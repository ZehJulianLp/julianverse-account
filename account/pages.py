import io
import json
import secrets

import pyotp
import qrcode
import qrcode.image.svg
from flask import (
    Blueprint,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from sqlalchemy import delete

from . import mail
from .auth import normalized_email, validate_password
from .extensions import db
from .models import (
    AuthorizationCode,
    BrowserSession,
    Client,
    CloudConnection,
    Consent,
    DiscordIdentity,
    OAuthToken,
    Passkey,
    RecoveryCode,
    SyncPreference,
    User,
    digest,
    now,
)
from .security import (
    encrypt,
    fresh_required,
    login_required,
    make_challenge,
    rate_limit,
    revoke_sessions,
    take_challenge,
)

bp = Blueprint("pages", __name__)


@bp.get("/")
def index():
    return redirect(url_for("pages.dashboard" if g.user else "auth.login"))


@bp.get("/overview")
@login_required
def dashboard():
    return render_template(
        "dashboard.html",
        cloud=db.session.get(CloudConnection, g.user.id),
        sync_count=db.session.scalar(
            db.select(db.func.count())
            .select_from(SyncPreference)
            .where(SyncPreference.user_id == g.user.id, SyncPreference.enabled.is_(True))
        ),
        session_count=db.session.scalar(
            db.select(db.func.count())
            .select_from(BrowserSession)
            .where(
                BrowserSession.user_id == g.user.id,
                BrowserSession.revoked.is_(False),
                BrowserSession.expires_at > now(),
            )
        ),
        passkey_count=db.session.scalar(
            db.select(db.func.count()).select_from(Passkey).where(Passkey.user_id == g.user.id)
        ),
    )


@bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        name = request.form.get("display_name", "").strip()
        theme = request.form.get("theme", "system")
        if not 1 <= len(name) <= 80 or theme not in ("system", "light", "dark"):
            abort(400, "Bitte prüfe Namen und Darstellung.")
        g.user.display_name, g.user.theme = name, theme
        db.session.commit()
        flash("Dein Profil wurde gespeichert.", "success")
        return redirect(url_for("pages.profile"))
    return render_template("profile.html")


@bp.post("/profile/email")
@fresh_required
def change_email():
    rate_limit("change-email", 3, 1800, identity=g.user.id)
    email = normalized_email(request.form.get("email", ""))
    if db.session.scalar(db.select(User.id).where(User.email == email)):
        abort(400, "Diese Adresse kann nicht verwendet werden.")
    try:
        mail.send_action(g.user, "email-change", email=email)
    except (OSError, RuntimeError):
        abort(
            503, "Die Bestätigung konnte nicht versendet werden. Bitte versuche es später erneut."
        )
    flash("Bitte bestätige deine neue E-Mail-Adresse über den zugesendeten Link.", "success")
    return redirect(url_for("pages.profile"))


@bp.get("/security")
@login_required
def security():
    return render_template(
        "security.html",
        passkeys=db.session.scalars(db.select(Passkey).where(Passkey.user_id == g.user.id)).all(),
        sessions=db.session.scalars(
            db.select(BrowserSession)
            .where(
                BrowserSession.user_id == g.user.id,
                BrowserSession.revoked.is_(False),
                BrowserSession.expires_at > now(),
            )
            .order_by(BrowserSession.last_seen.desc())
        ).all(),
        current_session=g.login_session.id,
    )


@bp.post("/security/password")
@fresh_required
def password():
    value = validate_password(request.form.get("password", ""))
    if value != request.form.get("password_confirm"):
        abort(400, "Die Passwörter stimmen nicht überein.")
    g.user.set_password(value)
    revoke_sessions(g.user.id, g.login_session.id)
    db.session.commit()
    flash("Passwort gespeichert. Andere Sitzungen wurden abgemeldet.", "success")
    return redirect(url_for("pages.security"))


@bp.post("/security/sessions/<identifier>/revoke")
@fresh_required
def revoke_session(identifier):
    item = db.session.get(BrowserSession, identifier)
    if not item or item.user_id != g.user.id:
        abort(404)
    item.revoked = True
    db.session.commit()
    if item.id == g.login_session.id:
        session.clear()
        return redirect(url_for("auth.login"))
    flash("Sitzung und zugehörige App-Zugänge wurden widerrufen.", "success")
    return redirect(url_for("pages.security"))


@bp.post("/security/totp/start")
@fresh_required
def totp_start():
    if g.user.totp_secret:
        abort(409, "Ein Authenticator ist bereits eingerichtet.")
    secret = pyotp.random_base32()
    challenge = make_challenge("totp-setup", {"secret": secret}, g.user.id)
    uri = pyotp.TOTP(secret).provisioning_uri(g.user.email, issuer_name="Julianverse Account")
    # Inline SVG is generated locally by qrcode; no third-party QR service receives the secret.
    svg = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage).to_string(
        encoding="unicode"
    )
    return render_template("totp_setup.html", secret=secret, qr=svg, challenge=challenge)


@bp.post("/security/totp/confirm")
@fresh_required
def totp_confirm():
    rate_limit("totp-setup", 8, 300, identity=g.user.id)
    payload, user = take_challenge(request.form.get("challenge", ""), "totp-setup")
    if not user or user.id != g.user.id or user.totp_secret:
        abort(400)
    if not pyotp.TOTP(payload["secret"]).verify(request.form.get("code", ""), valid_window=0):
        abort(400, "Der Code war ungültig. Bitte starte die Einrichtung erneut.")
    user.totp_secret = encrypt(payload["secret"])
    user.totp_last_counter = now() // 30
    revoke_sessions(user.id, g.login_session.id)
    codes = [secrets.token_hex(8) for _ in range(10)]
    db.session.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user.id))
    for code in codes:
        db.session.add(RecoveryCode(user_id=user.id, code_hash=digest(code)))
    db.session.commit()
    return render_template("recovery_codes.html", codes=codes)


@bp.post("/security/totp/disable")
@fresh_required
def totp_disable():
    # Require the current factor even when the browser session is still fresh.
    from .security import verify_second_factor

    rate_limit("totp-disable", 8, 300, identity=g.user.id)
    if not verify_second_factor(g.user, request.form.get("code")):
        abort(400, "Bitte gib einen aktuellen Authenticator- oder Wiederherstellungscode ein.")
    g.user.totp_secret = None
    g.user.totp_last_counter = -1
    db.session.execute(delete(RecoveryCode).where(RecoveryCode.user_id == g.user.id))
    revoke_sessions(g.user.id, g.login_session.id)
    db.session.commit()
    flash("Authenticator entfernt.", "success")
    return redirect(url_for("pages.security"))


@bp.get("/connections")
@login_required
def connections():
    return render_template(
        "connections.html",
        discord=db.session.scalar(
            db.select(DiscordIdentity).where(DiscordIdentity.user_id == g.user.id)
        ),
        cloud=db.session.get(CloudConnection, g.user.id),
    )


@bp.get("/apps")
@login_required
def applications():
    rows = db.session.execute(
        db.select(Consent, Client)
        .join(Client, Client.client_id == Consent.client_id)
        .where(Consent.user_id == g.user.id)
    ).all()
    return render_template("applications.html", applications=rows)


@bp.post("/apps/<client_id>/revoke")
@fresh_required
def revoke_app(client_id):
    consent = db.session.scalar(
        db.select(Consent).where(Consent.user_id == g.user.id, Consent.client_id == client_id)
    )
    if not consent:
        abort(404)
    db.session.delete(consent)
    db.session.execute(
        db.update(AuthorizationCode)
        .where(
            AuthorizationCode.user_id == g.user.id,
            AuthorizationCode.client_id == client_id,
            AuthorizationCode.used_at.is_(None),
        )
        .values(used_at=now())
    )
    db.session.execute(
        db.update(OAuthToken)
        .where(OAuthToken.user_id == g.user.id, OAuthToken.client_id == client_id)
        .values(revoked=True)
    )
    client = db.session.scalar(db.select(Client).where(Client.client_id == client_id))
    db.session.execute(
        db.update(SyncPreference)
        .where(SyncPreference.user_id == g.user.id, SyncPreference.app_slug == client.slug)
        .values(enabled=False)
    )
    db.session.commit()
    flash("App-Zugriff widerrufen und Sync für diese App deaktiviert.", "success")
    return redirect(url_for("pages.applications"))


@bp.get("/privacy")
@login_required
def privacy():
    return render_template("privacy.html")


@bp.post("/privacy/export")
@fresh_required
def export_account():
    user = g.user
    identity = db.session.scalar(
        db.select(DiscordIdentity).where(DiscordIdentity.user_id == user.id)
    )
    data = {
        "format": "julianverse-account-export",
        "version": 1,
        "profile": {
            key: getattr(user, key)
            for key in (
                "id",
                "username",
                "display_name",
                "email",
                "email_verified",
                "theme",
                "locale",
                "created_at",
            )
        },
        "discord": {"id": identity.id, "username": identity.username} if identity else None,
        "sync": [
            {
                "app": p.app_slug,
                "resource": p.resource,
                "enabled": p.enabled,
                "last_sync": p.last_sync,
            }
            for p in db.session.scalars(
                db.select(SyncPreference).where(SyncPreference.user_id == user.id)
            )
        ],
        "applications": [
            {"client_id": c.client_id, "scopes": c.scopes}
            for c in db.session.scalars(db.select(Consent).where(Consent.user_id == user.id))
        ],
        "note": "Synchronisierte App-Inhalte liegen in deinem ownCloud-Ordner Julianverse. Lade sie dort direkt herunter.",
    }
    return send_file(
        io.BytesIO(json.dumps(data, ensure_ascii=False, indent=2).encode()),
        mimetype="application/json",
        as_attachment=True,
        download_name="julianverse-account.json",
    )


@bp.post("/privacy/delete")
@fresh_required
def delete_account():
    if request.form.get("confirmation") != g.user.username:
        abort(400, "Gib deinen Benutzernamen zur Bestätigung ein.")
    db.session.delete(g.user)
    db.session.commit()
    session.clear()
    flash(
        "Dein Julianverse-Konto wurde gelöscht. Deine Dateien in ownCloud und lokale App-Daten bleiben erhalten.",
        "success",
    )
    return redirect(url_for("auth.login"))
