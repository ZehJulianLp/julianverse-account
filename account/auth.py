import re
import secrets

from authlib.integrations.flask_client import OAuth
from email_validator import EmailNotValidError, validate_email
from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from . import mail
from .extensions import db
from .models import Challenge, DiscordIdentity, MailAction, Passkey, User, digest, now
from .security import (
    browser_binding,
    fresh_required,
    login_required,
    make_challenge,
    rate_limit,
    revoke_sessions,
    safe_next,
    sign_in,
    sign_out,
    take_challenge,
    verify_second_factor,
)

bp = Blueprint("auth", __name__, url_prefix="/auth")
DUMMY_HASH = generate_password_hash("unusable-placeholder-password", method="scrypt")


def normalized_email(value):
    try:
        return validate_email(value, check_deliverability=False).normalized.lower()
    except EmailNotValidError:
        abort(400, "Bitte gib eine gültige E-Mail-Adresse an.")


def validate_password(value):
    if not 12 <= len(value) <= 128:
        abort(400, "Das Passwort muss zwischen 12 und 128 Zeichen lang sein.")
    return value


def start_authenticated_login(user, method, target):
    if not user.enabled:
        abort(403)
    if current_app.config["REQUIRE_VERIFIED_EMAIL"] and not user.email_verified:
        flash("Bitte bestätige zuerst deine E-Mail-Adresse.", "info")
        return redirect(url_for("auth.resend"))
    if user.totp_secret:
        session["pending_login"] = make_challenge(
            "login-mfa", {"method": method, "next": safe_next(target)}, user.id
        )
        return redirect(url_for("auth.second_factor"))
    sign_in(user, method)
    return redirect(safe_next(target))


@bp.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("pages.dashboard"))
    if not current_app.config["REGISTRATION_OPEN"]:
        abort(403, "Die Registrierung ist momentan geschlossen.")
    if request.method == "POST":
        rate_limit("register", 5, 3600)
        if current_app.config["REQUIRE_VERIFIED_EMAIL"] and not mail.available():
            abort(
                503, "Die Registrierung wird verfügbar, sobald der E-Mail-Versand eingerichtet ist."
            )
        username = request.form.get("username", "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,31}", username):
            abort(400, "Benutzernamen brauchen 3–32 Zeichen: a–z, 0–9, _ oder -.")
        email = normalized_email(request.form.get("email", ""))
        password = validate_password(request.form.get("password", ""))
        if password != request.form.get("password_confirm"):
            abort(400, "Die Passwörter stimmen nicht überein.")
        user = User(username=username, display_name=username, email=email)
        user.set_password(password)
        db.session.add(user)
        try:
            if request.form.get("existing_cloud") != "yes":
                from .provisioning import queue_new_cloud

                queue_new_cloud(user)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash(
                "Diese Angaben sind bereits vergeben. Melde dich an oder nutze die Wiederherstellung.",
                "error",
            )
            return render_template("register.html"), 400
        if mail.available():
            try:
                mail.send_action(user, "verify")
            except (OSError, RuntimeError):
                current_app.logger.warning("Verification email could not be sent.")
                flash(
                    "Dein Konto wurde erstellt. Bitte fordere die Bestätigung später erneut an.",
                    "info",
                )
                return redirect(url_for("auth.resend"))
        flash(
            "Dein Konto wurde erstellt. Bitte bestätige deine E-Mail-Adresse."
            if current_app.config["REQUIRE_VERIFIED_EMAIL"]
            else "Dein Konto wurde erstellt. Du kannst dich jetzt anmelden.",
            "success",
        )
        return redirect(url_for("auth.login"))
    return render_template("register.html")


@bp.route("/login", methods=["GET", "POST"])
def login():
    target = safe_next(request.values.get("next"))
    if g.user:
        return redirect(target)
    if request.method == "POST":
        rate_limit("login")
        identifier = request.form.get("identifier", "").strip().lower()
        rate_limit("login-user", 20, 900, identity=identifier)
        user = db.session.scalar(
            db.select(User).where((User.username == identifier) | (User.email == identifier))
        )
        password = request.form.get("password", "")
        valid = check_password_hash(
            user.password_hash if user and user.password_hash else DUMMY_HASH, password
        )
        if not user or not user.enabled or not valid:
            flash("Die Anmeldung ist fehlgeschlagen. Bitte prüfe deine Angaben.", "error")
            return render_template("login.html", next=target), 400
        return start_authenticated_login(user, "password", target)
    return render_template("login.html", next=target)


@bp.route("/second-factor", methods=["GET", "POST"])
def second_factor():
    item = db.session.get(Challenge, session.get("pending_login", ""))
    if not item or item.used or item.expires_at <= now() or item.browser_hash != browser_binding():
        return redirect(url_for("auth.login"))
    user = db.session.get(User, item.user_id)
    if not user or not user.enabled:
        abort(403)
    if request.method == "POST":
        rate_limit("mfa", 8, 300)
        if verify_second_factor(user, request.form.get("code")):
            payload, user = take_challenge(item.id, "login-mfa")
            if payload.get("reauth_session"):
                if (
                    not g.login_session
                    or g.login_session.id != payload["reauth_session"]
                    or g.user.id != user.id
                ):
                    abort(403, "Bitte beginne die Bestätigung erneut.")
                g.login_session.authenticated_at = now()
                db.session.commit()
                session.pop("pending_login", None)
            else:
                sign_in(user, payload["method"] + "+totp")
            return redirect(safe_next(payload["next"]))
        flash("Der Code ist ungültig oder wurde bereits verwendet.", "error")
    return render_template("second_factor.html")


@bp.post("/logout")
@login_required
def logout():
    sign_out()
    flash("Du bist abgemeldet. Lokale App-Daten bleiben auf diesem Gerät.", "success")
    return redirect(url_for("auth.login"))


@bp.route("/confirm", methods=["GET", "POST"])
@login_required
def reauthenticate():
    target = safe_next(request.values.get("next"))
    if request.method == "POST":
        rate_limit("reauth", 8, 300)
        if g.user.check_password(request.form.get("password", "")) and (
            not g.user.totp_secret or verify_second_factor(g.user, request.form.get("code"))
        ):
            g.login_session.authenticated_at = now()
            db.session.commit()
            return redirect(target)
        flash("Die Bestätigung ist fehlgeschlagen.", "error")
    return render_template("reauthenticate.html", next=target)


def mail_request(kind):
    if request.method == "POST":
        rate_limit("mail-" + kind, 4, 900)
        if not mail.available():
            abort(503, "Der E-Mail-Versand ist noch nicht eingerichtet.")
        address = normalized_email(request.form.get("email", ""))
        rate_limit("mail-user-" + kind, 3, 1800, identity=address)
        user = db.session.scalar(
            db.select(User).where(User.email == address, User.enabled.is_(True))
        )
        if user and (kind == "reset" or not user.email_verified):
            try:
                mail.send_action(user, kind)
            except (OSError, RuntimeError):
                current_app.logger.warning("Account email could not be sent.")
        flash("Falls ein passendes Konto vorhanden ist, erhältst du eine E-Mail.", "success")
        return redirect(url_for("auth.login"))
    return render_template("mail_request.html", kind=kind)


@bp.route("/recover", methods=["GET", "POST"])
def recover():
    return mail_request("reset")


@bp.route("/resend", methods=["GET", "POST"])
def resend():
    return mail_request("verify")


@bp.route("/mail/<token>", methods=["GET", "POST"])
def mail_action(token):
    action = db.session.get(MailAction, digest(token))
    if not action or action.used or action.expires_at <= now():
        abort(400, "Dieser Link ist abgelaufen oder wurde bereits verwendet.")
    user = db.session.get(User, action.user_id)
    if not user or not user.enabled:
        abort(400)
    if request.method == "POST":
        rate_limit("mail-confirm", 12, 300)
        password = None
        if action.kind == "reset":
            password = validate_password(request.form.get("password", ""))
            if password != request.form.get("password_confirm"):
                abort(400, "Die Passwörter stimmen nicht überein.")
        result = db.session.execute(
            update(MailAction)
            .where(MailAction.id == action.id, MailAction.used.is_(False))
            .values(used=True)
        )
        if result.rowcount != 1:
            db.session.rollback()
            abort(400)
        if action.kind in ("verify", "email-change"):
            # Verification cannot silently overwrite a later email change.
            if action.kind == "verify" and user.email != action.email:
                db.session.rollback()
                abort(400, "Dieser Link gehört zu einer früheren E-Mail-Adresse.")
            user.email = action.email
            user.email_verified = True
        elif action.kind == "reset":
            user.set_password(password)
            revoke_sessions(user.id)
            db.session.execute(
                update(MailAction)
                .where(
                    MailAction.user_id == user.id,
                    MailAction.kind == "reset",
                )
                .values(used=True)
            )
        else:
            abort(400)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            abort(400, "Diese E-Mail-Adresse kann nicht übernommen werden.")
        flash("Gespeichert. Du kannst dich jetzt anmelden.", "success")
        return redirect(url_for("auth.login"))
    return render_template("mail_action.html", kind=action.kind)


def init_discord(app):
    oauth = OAuth(app)
    oauth.register(
        name="discord",
        client_id=app.config["DISCORD_CLIENT_ID"],
        client_secret=app.config["DISCORD_CLIENT_SECRET"],
        authorize_url="https://discord.com/oauth2/authorize",
        access_token_url="https://discord.com/api/oauth2/token",
        api_base_url="https://discord.com/api/v10/",
        client_kwargs={
            "scope": "identify email",
            "token_endpoint_auth_method": "client_secret_post",
        },
    )
    app.extensions["discord_oauth"] = oauth


def discord_redirect(mode, target=None):
    if (
        not current_app.config["DISCORD_CLIENT_ID"]
        or not current_app.config["DISCORD_CLIENT_SECRET"]
    ):
        abort(503, "Discord ist noch nicht eingerichtet.")
    rate_limit("discord-start", 20, 300)
    session["discord_context"] = {
        "mode": mode,
        "user_id": g.user.id if g.user else None,
        "next": safe_next(target),
        "started_at": now(),
    }
    return current_app.extensions["discord_oauth"].discord.authorize_redirect(
        current_app.config["BASE_URL"] + "/auth/discord/callback"
    )


@bp.get("/discord")
def discord_login():
    if g.user:
        return redirect(url_for("pages.connections"))
    return discord_redirect("login", request.args.get("next"))


@bp.post("/discord/link")
@fresh_required
def discord_link():
    return discord_redirect("link", url_for("pages.connections"))


@bp.get("/discord/reauthenticate")
@login_required
def discord_reauthenticate():
    return discord_redirect("reauth", request.args.get("next"))


@bp.get("/discord/callback")
def discord_callback():
    rate_limit("discord-callback", 20, 300)
    context = session.pop("discord_context", None)
    if not context or context["started_at"] + 600 < now():
        abort(400, "Die Discord-Anmeldung ist abgelaufen.")
    try:
        client = current_app.extensions["discord_oauth"].discord
        token = client.authorize_access_token()
        response = client.get("users/@me", token=token)
        response.raise_for_status()
        profile = response.json()
    except Exception:
        # OAuth errors and responses may contain tokens. Do not log them.
        abort(400, "Die Discord-Anmeldung konnte nicht abgeschlossen werden.")
    discord_id = str(profile.get("id", ""))
    if not re.fullmatch(r"\d{5,32}", discord_id):
        abort(400, "Discord hat kein gültiges Profil geliefert.")
    identity = db.session.get(DiscordIdentity, discord_id)
    mode = context["mode"]
    if mode in ("link", "reauth"):
        if not g.user or g.user.id != context["user_id"]:
            abort(403, "Bitte beginne die Verknüpfung erneut in deinem Konto.")
        if mode == "reauth":
            if not identity or identity.user_id != g.user.id:
                abort(403, "Dieses Discord-Konto ist nicht für die Bestätigung verfügbar.")
            if g.user.totp_secret:
                session["pending_login"] = make_challenge(
                    "login-mfa",
                    {
                        "method": "discord",
                        "next": safe_next(context["next"]),
                        "reauth_session": g.login_session.id,
                    },
                    g.user.id,
                )
                return redirect(url_for("auth.second_factor"))
            g.login_session.authenticated_at = now()
            db.session.commit()
            return redirect(safe_next(context["next"]))
        if identity and identity.user_id != g.user.id:
            abort(409, "Dieses Discord-Konto ist bereits mit einem anderen Konto verbunden.")
        if g.login_session.authenticated_at + 600 < now():
            abort(403, "Bitte bestätige deine Anmeldung erneut, bevor du Discord verknüpfst.")
        existing = db.session.scalar(
            db.select(DiscordIdentity).where(DiscordIdentity.user_id == g.user.id)
        )
        if existing and existing.id != discord_id:
            abort(409, "Löse zunächst deine bisherige Discord-Verbindung.")
        if not identity:
            identity = DiscordIdentity(
                id=discord_id, user_id=g.user.id, username=profile.get("username", "Discord")[:100]
            )
            db.session.add(identity)
        db.session.commit()
        flash("Dein Discord-Konto ist verbunden.", "success")
        return redirect(url_for("pages.connections"))
    if identity:
        return start_authenticated_login(
            db.session.get(User, identity.user_id), "discord", context["next"]
        )
    if not current_app.config["REGISTRATION_OPEN"]:
        abort(403, "Die Registrierung ist momentan geschlossen.")
    if not profile.get("verified") or not profile.get("email"):
        abort(400, "Für die Registrierung muss deine Discord-E-Mail-Adresse bestätigt sein.")
    email = normalized_email(profile["email"])
    if db.session.scalar(db.select(User.id).where(User.email == email)):
        flash(
            "Zu dieser E-Mail gibt es bereits ein Konto. Melde dich dort an und verbinde Discord unter Verbindungen.",
            "info",
        )
        return redirect(url_for("auth.login"))
    username = re.sub(r"[^a-z0-9_-]", "", profile.get("username", "").lower())[:24]
    if len(username) < 3:
        username = "discord"
    if db.session.scalar(db.select(User.id).where(User.username == username)):
        username += "-" + secrets.token_hex(3)
    user = User(
        username=username,
        display_name=(profile.get("global_name") or username)[:80],
        email=email,
        email_verified=True,
    )
    db.session.add(user)
    db.session.flush()
    from .provisioning import queue_new_cloud

    queue_new_cloud(user)
    db.session.add(
        DiscordIdentity(
            id=discord_id, user_id=user.id, username=profile.get("username", username)[:100]
        )
    )
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        abort(409, "Die Verbindung wurde bereits angelegt. Bitte melde dich erneut an.")
    return start_authenticated_login(user, "discord", context["next"])


@bp.post("/discord/unlink")
@fresh_required
def discord_unlink():
    passkey = db.session.scalar(db.select(Passkey.id).where(Passkey.user_id == g.user.id))
    if not g.user.password_hash and not passkey:
        abort(400, "Lege zuerst ein Passwort oder einen Passkey als weitere Anmeldemöglichkeit an.")
    identity = db.session.scalar(
        db.select(DiscordIdentity).where(DiscordIdentity.user_id == g.user.id)
    )
    if identity:
        db.session.delete(identity)
        db.session.commit()
    flash("Die Discord-Verbindung wurde gelöst.", "success")
    return redirect(url_for("pages.connections"))
