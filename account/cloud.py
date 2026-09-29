import json
import re
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree

import httpx
from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from sqlalchemy.exc import IntegrityError

from .extensions import csrf, db
from .models import (
    AuthorizationCode,
    Client,
    CloudConnection,
    OAuthToken,
    SyncPreference,
    User,
    now,
)
from .security import decrypt, encrypt, fresh_required, login_required, rate_limit

bp = Blueprint("cloud", __name__)
CATALOG = {
    "startpage": {
        "name": "Startpage",
        "resources": {
            "settings": "Einstellungen",
            "bookmarks": "Lesezeichen",
            "tasks": "Aufgaben",
            "notes": "Notizen",
            "profiles": "Profile",
        },
    },
    "weather": {"name": "Wetter", "resources": {"settings": "Einstellungen", "locations": "Orte"}},
    "news": {
        "name": "Julianverse News",
        "resources": {
            "sources": "Quellen & Reihenfolge",
            "saved": "Leseliste",
            "read": "Gelesen-Status",
            "settings": "Ansicht & Leseeinstellungen",
        },
    },
    "searxng": {
        "name": "Julianverse Search",
        "resources": {
            "favorites": "Gespeicherte Suchen",
            "history": "Suchverlauf",
            "settings": "Sucheinstellungen",
        },
    },
}
MAX_BYTES = 512 * 1024
NEWS_MAX_BYTES = 8 * 1024 * 1024


def revoke_sync_access(user_id):
    # A changed ownCloud connection requires a new login/activation in each app.
    # Existing offline queues must not continue against another cloud account.
    db.session.execute(
        db.update(OAuthToken)
        .where(OAuthToken.user_id == user_id, OAuthToken.scope.like("%sync%"))
        .values(revoked=True)
    )
    db.session.execute(
        db.update(AuthorizationCode)
        .where(
            AuthorizationCode.user_id == user_id,
            AuthorizationCode.scope.like("%sync%"),
            AuthorizationCode.used_at.is_(None),
        )
        .values(used_at=now())
    )


def dav_url(connection, app_slug=None, resource=None):
    base = (
        current_app.config["OWNCLOUD_BASE_URL"]
        + "/remote.php/dav/files/"
        + quote(connection.username, safe="")
        + "/"
    )
    if app_slug:
        root = current_app.config["OWNCLOUD_SYNC_ROOT"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", root):
            raise RuntimeError("OWNCLOUD_SYNC_ROOT must be one safe directory name.")
        base += root + "/" + app_slug + "/"
    if resource:
        base += resource + ".json"
    return base


def dav(connection, method, url, *, max_bytes=MAX_BYTES, **kwargs):
    attempts = 2 if method.upper() in ("GET", "HEAD", "PROPFIND") else 1
    for attempt in range(attempts):
        try:
            with httpx.Client(
                auth=(connection.username, decrypt(connection.secret)),
                # Apache's gzip representation adds an ETag suffix that cannot
                # be used as the file's If-Match version on a later PUT.
                headers={"Accept-Encoding": "identity"},
                timeout=httpx.Timeout(6, connect=3),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                with client.stream(method, url, **kwargs) as response:
                    chunks, length = [], 0
                    for chunk in response.iter_bytes():
                        length += len(chunk)
                        if length > max_bytes:
                            abort(
                                502,
                                f"Die ownCloud-Datei überschreitet die unterstützte Größe von {max_bytes // 1024} KiB.",
                            )
                        chunks.append(chunk)
                    # iter_bytes() already decompresses the body. Reusing the wire
                    # encoding headers would make Response decode it a second time.
                    headers = response.headers.copy()
                    for name in ("content-encoding", "content-length", "transfer-encoding"):
                        headers.pop(name, None)
                    result = httpx.Response(
                        response.status_code, headers=headers, content=b"".join(chunks)
                    )
            if result.status_code in (502, 503, 504) and attempt + 1 < attempts:
                continue
            break
        except httpx.HTTPError:
            if attempt + 1 == attempts:
                abort(
                    502, "ownCloud ist gerade nicht erreichbar. Deine lokale Kopie bleibt erhalten."
                )
    if result.status_code in (401, 403):
        abort(
            502,
            "ownCloud hat den Zugriff abgelehnt. Bitte prüfe die Verbindung und das App-Passwort.",
        )
    if result.status_code == 507:
        abort(507, "Dein ownCloud-Speicher ist voll. Deine lokale Änderung bleibt erhalten.")
    if result.status_code >= 500 or 300 <= result.status_code < 400:
        abort(502, "ownCloud konnte die Anfrage nicht ausführen.")
    return result


def revoke_cloud_login(user_id):
    client_id = db.session.scalar(db.select(Client.client_id).where(Client.slug == "owncloud"))
    if client_id:
        db.session.execute(
            db.update(OAuthToken)
            .where(OAuthToken.user_id == user_id, OAuthToken.client_id == client_id)
            .values(revoked=True)
        )
        db.session.execute(
            db.update(AuthorizationCode)
            .where(
                AuthorizationCode.user_id == user_id,
                AuthorizationCode.client_id == client_id,
                AuthorizationCode.used_at.is_(None),
            )
            .values(used_at=now())
        )


def proven_username(username, password):
    connection = CloudConnection(username=username, secret=encrypt(password))
    result = dav(
        connection,
        "PROPFIND",
        current_app.config["OWNCLOUD_BASE_URL"] + "/remote.php/dav/",
        headers={"Depth": "0", "Content-Type": "application/xml"},
        content=b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>',
    )
    if result.status_code != 207:
        abort(502, "Der ownCloud-Zugang konnte nicht geprüft werden.")
    try:
        tree = ElementTree.fromstring(result.content)
        hrefs = [
            p.findtext("{DAV:}prop/{DAV:}current-user-principal/{DAV:}href")
            for p in tree.findall("{DAV:}response/{DAV:}propstat")
            if " 200 " in (p.findtext("{DAV:}status") or "")
        ]
        hrefs = {h for h in hrefs if h}
        if len(hrefs) != 1:
            raise ValueError
        path = urlsplit(hrefs.pop()).path
        prefix = (
            urlsplit(current_app.config["OWNCLOUD_BASE_URL"]).path
            + "/remote.php/dav/principals/users/"
        )
        if not path.startswith(prefix) or not path.endswith("/"):
            raise ValueError
        canonical = unquote(path[len(prefix) : -1])
        if not canonical or len(canonical) > 254 or any(c in canonical for c in "/\\\x00\r\n"):
            raise ValueError
        return canonical
    except (ElementTree.ParseError, ValueError):
        abort(502, "ownCloud hat die Identität des angemeldeten Benutzers nicht bestätigt.")


@bp.post("/connections/owncloud")
@fresh_required
def connect():
    rate_limit("cloud-connect", 5, 300, identity=g.user.id)
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    if (
        not username
        or len(username) > 254
        or any(c in username for c in "/\\\x00")
        or username in (".", "..")
        or not 1 <= len(password) <= 1024
    ):
        abort(400, "Bitte gib deinen ownCloud-Benutzernamen und ein App-Passwort ein.")
    if not g.user.email_verified:
        abort(403, "Bitte bestätige zuerst deine E-Mail-Adresse.")
    username = proven_username(username, password)
    # Serialize the final binding change against the provisioning worker.
    checked = db.session.execute(
        db.update(User).where(User.id == g.user.id, User.enabled.is_(True)).values(enabled=True)
    )
    if checked.rowcount != 1:
        abort(403)
    old = db.session.get(CloudConnection, g.user.id, populate_existing=True)
    if old and old.lease_until >= now():
        abort(409, "Die Cloud-Einrichtung läuft gerade. Bitte versuche es gleich erneut.")
    if old and old.managed and old.remote_enabled is not None and old.state != "ready":
        abort(409, "Die Cloud-Einrichtung muss zuerst abgeschlossen werden.")
    if (
        old
        and old.managed
        and old.remote_enabled is not None
        and old.username_key != username.casefold()
    ):
        abort(
            409,
            "Für dich wurde bereits ein Cloud-Konto angelegt. Bitte wende dich für einen Kontowechsel an den Betreiber, damit keine Dateien zurückbleiben.",
        )
    connection = CloudConnection(user_id=g.user.id, username=username, secret=encrypt(password))
    # Every reconnect disables sync, including when changing the ownCloud account.
    revoke_sync_access(g.user.id)
    revoke_cloud_login(g.user.id)
    db.session.execute(
        db.update(SyncPreference).where(SyncPreference.user_id == g.user.id).values(enabled=False)
    )
    if old:
        old.username, old.secret, old.connected_at = username, connection.secret, now()
        old.username_key = username.casefold()
        if old.remote_enabled is None:
            old.managed = False
        old.state = "ready"
        old.last_error = None
    else:
        db.session.add(connection)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        abort(409, "Dieses ownCloud-Konto ist bereits mit einem Julianverse-Konto verknüpft.")
    flash(
        "ownCloud verbunden. Wähle unter Cloud-Sync, welche Daten synchronisiert werden dürfen.",
        "success",
    )
    return redirect(url_for("cloud.settings"))


@bp.post("/connections/owncloud/disconnect")
@fresh_required
def disconnect():
    revoke_sync_access(g.user.id)
    connection = db.session.get(CloudConnection, g.user.id)
    if connection and not connection.managed:
        revoke_cloud_login(g.user.id)
        db.session.delete(connection)
    db.session.execute(
        db.update(SyncPreference).where(SyncPreference.user_id == g.user.id).values(enabled=False)
    )
    db.session.commit()
    flash(
        "Sync ausgeschaltet. Dein Cloud-Konto und vorhandene Dateien bleiben erhalten."
        if connection and connection.managed
        else "ownCloud getrennt. Vorhandene Dateien und lokale Daten bleiben erhalten.",
        "success",
    )
    return redirect(url_for("pages.connections"))


@bp.get("/sync")
@login_required
def settings():
    from .developer import catalog_for

    prefs = {
        (p.app_slug, p.resource): p
        for p in db.session.scalars(
            db.select(SyncPreference).where(SyncPreference.user_id == g.user.id)
        )
    }
    return render_template(
        "sync.html",
        catalog=catalog_for(g.user),
        preferences=prefs,
        cloud=db.session.get(CloudConnection, g.user.id),
    )


@bp.post("/sync/preferences")
@login_required
def preferences():
    from .developer import catalog_for

    connection = db.session.get(CloudConnection, g.user.id)
    if not connection or connection.state != "ready":
        abort(409, "Bitte verbinde zuerst ownCloud.")
    selected = set(request.form.getlist("resources"))
    catalog = catalog_for(g.user)
    allowed = {f"{app}/{res}" for app, entry in catalog.items() for res in entry["resources"]}
    if not selected <= allowed:
        abort(400)
    for app, entry in catalog.items():
        for resource in entry["resources"]:
            pref = db.session.scalar(
                db.select(SyncPreference).where(
                    SyncPreference.user_id == g.user.id,
                    SyncPreference.app_slug == app,
                    SyncPreference.resource == resource,
                )
            )
            if not pref:
                pref = SyncPreference(user_id=g.user.id, app_slug=app, resource=resource)
                db.session.add(pref)
            pref.enabled = f"{app}/{resource}" in selected
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        abort(409, "Die Einstellungen wurden gleichzeitig geändert. Bitte lade die Seite neu.")
    flash(
        "Sync-Auswahl gespeichert. Den Abgleich startest du ausdrücklich in der jeweiligen App.",
        "success",
    )
    return redirect(url_for("cloud.settings"))


def authorize_sync(app_slug, resource=None):
    from .developer import sync_resources
    from .oidc import bearer_user

    user, token = bearer_user("sync")
    if token.client.slug != app_slug:
        abort(403, "Dieses App-Token darf nur auf den eigenen App-Ordner zugreifen.")
    resources = sync_resources(token.client)
    if (not resources and not token.client.developer_app) or (
        resource and resource not in resources
    ):
        abort(404)
    if token.client.developer_app:
        g.developer_sync_app = token.client.developer_app
    g.sync_resources = resources
    connection = db.session.get(CloudConnection, user.id)
    if not connection or connection.state != "ready":
        abort(409, "ownCloud ist nicht verbunden.")
    return user, connection


@bp.post("/connections/owncloud/create")
@fresh_required
def create_cloud():
    from .provisioning import queue_new_cloud

    if not g.user.email_verified:
        abort(403, "Bitte bestätige zuerst deine E-Mail-Adresse.")
    if db.session.get(CloudConnection, g.user.id):
        abort(409, "Es besteht bereits eine Cloud-Verknüpfung oder ein Auftrag.")
    try:
        queue_new_cloud(g.user)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        abort(409, "Der Auftrag wurde bereits angelegt.")
    flash("Dein Cloud-Konto mit 1 GB wird eingerichtet. Sync bleibt ausgeschaltet.", "success")
    return redirect(url_for("pages.connections"))


@bp.route("/api/sync/<app_slug>", methods=["GET", "OPTIONS"])
@csrf.exempt
def status(app_slug):
    if request.method == "OPTIONS":
        return "", 204
    user, _ = authorize_sync(app_slug)
    prefs = db.session.scalars(
        db.select(SyncPreference).where(
            SyncPreference.user_id == user.id, SyncPreference.app_slug == app_slug
        )
    ).all()
    return {
        "app": app_slug,
        "subject": user.id,
        "resources": {
            r: next((p.enabled for p in prefs if p.resource == r), False) for r in g.sync_resources
        },
    }


@bp.route("/api/sync/<app_slug>/<resource>", methods=["GET", "PUT", "OPTIONS"])
@csrf.exempt
def sync(app_slug, resource):
    if request.method == "OPTIONS":
        return "", 204
    user, connection = authorize_sync(app_slug, resource)
    rate_limit("sync", 120, 60, identity=user.id)
    pref = db.session.scalar(
        db.select(SyncPreference).where(
            SyncPreference.user_id == user.id,
            SyncPreference.app_slug == app_slug,
            SyncPreference.resource == resource,
        )
    )
    if not pref or not pref.enabled:
        abort(403, "Sync für diese Daten ist ausgeschaltet.")
    max_bytes = NEWS_MAX_BYTES if app_slug == "news" else MAX_BYTES
    request.max_content_length = max_bytes
    url = dav_url(connection, app_slug, resource)
    if request.method == "GET":
        result = dav(connection, "GET", url, max_bytes=max_bytes)
        if result.status_code == 404:
            return {"error": "not_found"}, 404
        if result.status_code != 200:
            abort(502, "Die Datei konnte nicht gelesen werden.")
        try:
            payload = result.json()
        except ValueError:
            abort(409, "Die Datei enthält ungültiges JSON. Bitte prüfe sie direkt in ownCloud.")
        validate_document(payload)
        etag = result.headers.get("etag")
        if not etag or etag.startswith("W/"):
            abort(502, "ownCloud hat keine sichere Dateiversion (ETag) geliefert.")
        return jsonify(payload), 200, {"ETag": etag}
    payload = request.get_json()
    validate_document(payload)
    content = json.dumps(payload, ensure_ascii=False).encode()
    if len(content) > max_bytes:
        abort(413, "Diese Sync-Datei ist zu groß. Deine lokalen Daten bleiben erhalten.")
    match, create = request.headers.get("If-Match"), request.headers.get("If-None-Match")
    if (
        bool(match) == bool(create)
        or (create and create != "*")
        or (match and (not re.fullmatch(r'"[^"\r\n]+"', match) or match == '"*"'))
    ):
        abort(
            428,
            "Nutze If-Match mit der gelesenen Dateiversion oder If-None-Match: * für eine neue Datei.",
        )
    # Only an explicit write creates directories; connecting and enabling sync upload nothing.
    root_url = dav_url(connection) + current_app.config["OWNCLOUD_SYNC_ROOT"] + "/"
    for directory in (root_url, dav_url(connection, app_slug)):
        result = dav(connection, "MKCOL", directory)
        if result.status_code not in (201, 405):
            abort(502, "Der Sync-Ordner konnte nicht angelegt werden.")
    headers = {
        "Content-Type": "application/json",
        "If-Match" if match else "If-None-Match": match or create,
    }
    result = dav(connection, "PUT", url, headers=headers, content=content)
    if result.status_code == 412:
        return {
            "error": "conflict",
            "message": "Die Cloud-Datei wurde geändert. Beide Versionen bleiben erhalten; bitte löse den Konflikt in der App.",
        }, 412
    if result.status_code not in (200, 201, 204):
        abort(502, "Die Datei konnte nicht gespeichert werden.")
    pref.last_sync = now()
    db.session.commit()
    # Do not infer a PUT version from a later GET, which could see another writer's document.
    return {"saved": True}, 200, {"ETag": result.headers.get("etag", "")}


def validate_document(payload):
    if (
        not isinstance(payload, dict)
        or payload.get("schemaVersion") != 1
        or "data" not in payload
        or not isinstance(payload.get("deleted", False), bool)
    ):
        abort(
            409,
            "Erwartet wird ein JSON-Dokument mit schemaVersion: 1 und data. Löschungen verwenden deleted: true.",
        )
