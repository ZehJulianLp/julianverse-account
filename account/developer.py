"""User-owned public OIDC clients. App contents stay in each user's ownCloud."""

import ipaddress
import re
import secrets
from urllib.parse import unquote, urlsplit, urlunsplit

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from .extensions import db
from .models import (
    AppError,
    AppResource,
    AuthorizationCode,
    Client,
    Consent,
    DeveloperApp,
    OAuthToken,
    SyncPreference,
    User,
    now,
)
from .security import fresh_required, login_required, rate_limit

bp = Blueprint("developer", __name__, url_prefix="/developer")
ICONS = {
    "puzzle": "🧩",
    "notes": "📝",
    "books": "📚",
    "check": "✓",
    "star": "★",
    "cloud": "☁",
    "plant": "🌱",
    "code": "⌘",
}


def owned(slug):
    item = db.session.scalar(
        db.select(DeveloperApp)
        .join(Client)
        .where(
            Client.slug == slug,
            DeveloperApp.owner_id == g.user.id,
            DeveloperApp.deleted_at.is_(None),
        )
    )
    if not item:
        abort(404)
    return item


def web_url(raw, test_mode=False, callback=False):
    raw = raw.strip()
    if not raw or len(raw) > 2048 or any(ord(c) < 33 for c in raw) or "\\" in raw:
        abort(400, "Bitte gib eine vollständige Web-Adresse ohne Leerzeichen ein.")
    try:
        parsed = urlsplit(raw)
        decoded_path = unquote(parsed.path)
        host = (parsed.hostname or "").encode("idna").decode().lower().rstrip(".")
        try:
            address = ipaddress.ip_address(host)
            host = address.compressed
        except ValueError:
            if len(host) > 253 or not all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in host.split(".")
            ):
                raise ValueError from None
        local = host in ("localhost", "127.0.0.1", "::1")
        port = parsed.port  # Reject invalid ports.
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or (callback and parsed.query)
            or parsed.scheme != "https"
            and not (test_mode and local and parsed.scheme == "http")
            or "*" in raw
            or host == urlsplit(current_app.config["BASE_URL"]).hostname
            or any(part in (".", "..") for part in decoded_path.split("/"))
            or "\\" in decoded_path
            or any(ord(c) < 32 for c in decoded_path)
            or (callback and (not parsed.path or parsed.path.endswith("/")))
        ):
            raise ValueError
    except (ValueError, UnicodeError):
        abort(
            400,
            "Apps brauchen HTTPS und exakte Callback-Adressen ohne Query oder Fragment. Im Testmodus ist HTTP auf localhost erlaubt. Die Account-Domain ist reserviert.",
        )
    authority = f"[{host}]" if ":" in host else host
    if port is not None and port != (443 if parsed.scheme == "https" else 80):
        authority += f":{port}"
    return urlunsplit((parsed.scheme, authority, parsed.path or "/", parsed.query, ""))


def app_form():
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    icon = request.form.get("icon", "puzzle")
    visibility = request.form.get("visibility", "private")
    test_mode = request.form.get("test_mode") == "on"
    if (
        not 1 <= len(name) <= 80
        or len(description) > 500
        or icon not in ICONS
        or visibility not in ("private", "unlisted")
    ):
        abort(400, "Bitte prüfe Namen, Beschreibung, Icon und Sichtbarkeit.")
    website = web_url(request.form.get("website", ""), test_mode)
    callbacks = list(
        dict.fromkeys(
            line.strip() for line in request.form.get("callbacks", "").splitlines() if line.strip()
        )
    )
    if not 1 <= len(callbacks) <= 5:
        abort(400, "Trage eine bis fünf Rücksprungadressen ein.")
    callbacks = [web_url(uri, test_mode, callback=True) for uri in callbacks]
    origin = urlsplit(website)
    if any(
        (urlsplit(uri).scheme, urlsplit(uri).netloc) != (origin.scheme, origin.netloc)
        for uri in callbacks
    ):
        abort(
            400,
            "App-Adresse und Rücksprungadressen müssen dieselbe Origin verwenden (Protokoll, Host und Port).",
        )
    return dict(
        name=name,
        description=description,
        icon=icon,
        visibility=visibility,
        test_mode=test_mode,
        website=website,
        callbacks=callbacks,
    )


def revoke_client(client):
    db.session.execute(
        db.update(OAuthToken).where(OAuthToken.client_id == client.client_id).values(revoked=True)
    )
    db.session.execute(
        db.update(AuthorizationCode)
        .where(AuthorizationCode.client_id == client.client_id, AuthorizationCode.used_at.is_(None))
        .values(used_at=now())
    )
    db.session.execute(db.delete(Consent).where(Consent.client_id == client.client_id))
    db.session.execute(
        db.update(SyncPreference)
        .where(SyncPreference.app_slug == client.slug)
        .values(enabled=False)
    )


def metadata(name, website, callbacks):
    return dict(
        client_name=name,
        client_uri=website,
        redirect_uris=callbacks,
        scope="openid profile sync",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
    )


@bp.get("/apps")
@login_required
def index():
    apps = db.session.scalars(
        db.select(DeveloperApp)
        .where(DeveloperApp.owner_id == g.user.id, DeveloperApp.deleted_at.is_(None))
        .order_by(DeveloperApp.created_at.desc())
    ).all()
    return render_template("developer_apps.html", apps=apps, icons=ICONS)


@bp.route("/apps/new", methods=["GET", "POST"])
@fresh_required
def create():
    if not g.user.email_verified:
        abort(403, "Bestätige zuerst deine E-Mail-Adresse, um eigene Apps anzulegen.")
    if request.method == "GET":
        return render_template("developer_form.html", item=None, icons=ICONS)
    rate_limit("create-app", 10, 3600, identity=g.user.id)
    values = app_form()
    # Serialize the limit check across workers.
    db.session.execute(db.update(User).where(User.id == g.user.id).values(enabled=User.enabled))
    count = db.session.scalar(
        db.select(db.func.count())
        .select_from(DeveloperApp)
        .where(DeveloperApp.owner_id == g.user.id, DeveloperApp.deleted_at.is_(None))
    )
    if count >= 20:
        abort(409, "Du kannst bis zu 20 eigene Apps verwalten.")
    client = Client(
        slug="app-" + secrets.token_hex(12),
        client_id=secrets.token_urlsafe(24),
        client_secret="",
        client_id_issued_at=now(),
    )
    client.set_client_metadata(
        metadata(values.pop("name"), values["website"], values.pop("callbacks"))
    )
    item = DeveloperApp(client=client, owner_id=g.user.id, **values)
    db.session.add(item)
    db.session.commit()
    flash("App angelegt. Ergänze jetzt ihre Datenarten und lade die Vorlage herunter.", "success")
    return redirect(url_for("developer.detail", slug=client.slug))


@bp.get("/apps/<slug>")
@login_required
def detail(slug):
    item = owned(slug)
    users = db.session.scalar(
        db.select(db.func.count()).select_from(Consent).where(Consent.client_id == item.client_id)
    )
    errors = db.session.scalars(
        db.select(AppError)
        .where(AppError.client_id == item.client_id)
        .order_by(AppError.id.desc())
        .limit(20)
    ).all()
    return render_template(
        "developer_detail.html", item=item, icons=ICONS, users=users, errors=errors
    )


@bp.route("/apps/<slug>/edit", methods=["GET", "POST"])
@fresh_required
def edit(slug):
    item = owned(slug)
    if request.method == "GET":
        return render_template("developer_form.html", item=item, icons=ICONS)
    values = app_form()
    client = item.client
    changed_access = (
        values["callbacks"] != client.redirect_uris
        or values["visibility"] != item.visibility
        or values["test_mode"] != item.test_mode
    )
    client.set_client_metadata(
        metadata(values.pop("name"), values["website"], values.pop("callbacks"))
    )
    for key, value in values.items():
        setattr(item, key, value)
    if changed_access:
        revoke_client(client)
    db.session.commit()
    flash(
        "App gespeichert. Geänderte Rücksprungadressen oder Zugriffsregeln erfordern eine neue Anmeldung und Freigabe."
        if changed_access
        else "App gespeichert.",
        "success",
    )
    return redirect(url_for("developer.detail", slug=slug))


@bp.post("/apps/<slug>/state")
@fresh_required
def state(slug):
    item = owned(slug)
    action = request.form.get("action")
    if action not in ("enable", "disable", "delete"):
        abort(400)
    if action == "delete" and request.form.get("confirmation") != item.client.client_name:
        abort(400, "Gib den Namen der App zur Bestätigung ein.")
    item.client.enabled = action == "enable"
    if action != "enable":
        revoke_client(item.client)
    if action == "delete":
        item.deleted_at = now()
    db.session.commit()
    flash(
        "App gelöscht. Die Nutzer behalten ihre eigenen Cloud-Dateien und können sie unter Apps & Zugriff verwalten."
        if action == "delete"
        else "App-Status gespeichert.",
        "success",
    )
    return redirect(
        url_for("developer.index") if action == "delete" else url_for("developer.detail", slug=slug)
    )


@bp.post("/apps/<slug>/resources")
@fresh_required
def resources(slug):
    item = owned(slug)
    key = request.form.get("key", "").strip()
    label = request.form.get("label", "").strip()
    description = request.form.get("description", "").strip()
    if (
        not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", key)
        or not 1 <= len(label) <= 80
        or len(description) > 240
    ):
        abort(
            400,
            "Datenarten brauchen einen Schlüssel aus Kleinbuchstaben, Ziffern und Bindestrichen, einen Titel und eine kurze Beschreibung.",
        )
    # Lock the app row before checking/upserting resource keys.
    db.session.execute(
        db.update(DeveloperApp)
        .where(DeveloperApp.client_id == item.client_id)
        .values(website=DeveloperApp.website)
    )
    resource = db.session.scalar(
        db.select(AppResource).where(
            AppResource.client_id == item.client_id, AppResource.key == key
        )
    )
    if not resource:
        if (
            db.session.scalar(
                db.select(db.func.count())
                .select_from(AppResource)
                .where(AppResource.client_id == item.client_id)
            )
            >= 10
        ):
            abort(409, "Eine App kann bis zu zehn Datenarten definieren.")
        resource = AppResource(client_id=item.client_id, key=key)
        db.session.add(resource)
    resource.label, resource.description = label, description
    resource.enabled = request.form.get("enabled") == "on"
    if not resource.enabled:
        db.session.execute(
            db.update(SyncPreference)
            .where(SyncPreference.app_slug == slug, SyncPreference.resource == key)
            .values(enabled=False)
        )
    db.session.commit()
    flash(
        "Datenart gespeichert. Neue und erneut aktivierte Datenarten brauchen eine ausdrückliche Freigabe durch jeden Nutzer.",
        "success",
    )
    return redirect(url_for("developer.detail", slug=slug))


@bp.get("/app/<slug>")
@login_required
def shared(slug):
    item = db.session.scalar(db.select(DeveloperApp).join(Client).where(Client.slug == slug))
    if not item or not item.client.allows_user(g.user):
        abort(404)
    return render_template("developer_shared.html", item=item, icons=ICONS)


@bp.get("/docs")
@login_required
def docs():
    return render_template("developer_docs.html")


@bp.get("/apps/<slug>/starter.zip")
@login_required
def starter(slug):
    from .sdk import starter_zip

    return starter_zip(owned(slug))


def catalog_for(user):
    from .cloud import CATALOG

    catalog = {slug: dict(entry) for slug, entry in CATALOG.items()}
    consented = db.select(Consent.client_id).where(Consent.user_id == user.id)
    apps = db.session.scalars(
        db.select(DeveloperApp).where(
            (DeveloperApp.owner_id == user.id) | DeveloperApp.client_id.in_(consented),
            DeveloperApp.deleted_at.is_(None),
        )
    ).all()
    for item in apps:
        if item.client.allows_user(user):
            catalog[item.client.slug] = {
                "name": item.client.client_name,
                "resources": {r.key: r.label for r in item.resources if r.enabled},
                "descriptions": {r.key: r.description for r in item.resources if r.enabled},
                "owner": item.owner.display_name,
            }
    return catalog


def sync_resources(client):
    from .cloud import CATALOG

    if client.developer_app:
        return {r.key: r.label for r in client.developer_app.resources if r.enabled}
    return CATALOG.get(client.slug, {}).get("resources", {})


def record_response(response):
    """Only count authenticated app requests; never retain bodies or user identifiers."""
    item = g.get("developer_sync_app")
    if item:
        if response.status_code < 300:
            column = (
                DeveloperApp.write_count if request.method == "PUT" else DeveloperApp.read_count
            )
            db.session.execute(
                db.update(DeveloperApp)
                .where(DeveloperApp.client_id == item.client_id)
                .values({column: column + 1, DeveloperApp.last_used: now()})
            )
        elif response.status_code >= 400:
            db.session.add(
                AppError(
                    client_id=item.client_id,
                    operation="sync-" + request.method.lower(),
                    status=response.status_code,
                )
            )
            db.session.flush()
            keep = (
                db.select(AppError.id)
                .where(AppError.client_id == item.client_id)
                .order_by(AppError.id.desc())
                .limit(100)
            )
            db.session.execute(
                db.delete(AppError).where(
                    AppError.client_id == item.client_id, AppError.id.not_in(keep)
                )
            )
        db.session.commit()
    return response
