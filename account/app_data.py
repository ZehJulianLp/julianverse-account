"""The signed-in user's app files remain manageable after access is revoked."""

from flask import (
    Blueprint,
    abort,
    flash,
    g,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)

from .cloud import CATALOG, MAX_BYTES, NEWS_MAX_BYTES, dav, dav_url
from .extensions import db
from .models import Client, CloudConnection, Consent, OAuthToken, SyncPreference
from .security import fresh_required, login_required

bp = Blueprint("app_data", __name__, url_prefix="/apps")


def associated_clients(user_id):
    return (
        db.select(Client)
        .where(
            Client.client_id.in_(db.select(Consent.client_id).where(Consent.user_id == user_id))
            | Client.client_id.in_(
                db.select(OAuthToken.client_id).where(OAuthToken.user_id == user_id)
            )
            | Client.slug.in_(
                db.select(SyncPreference.app_slug).where(SyncPreference.user_id == user_id)
            )
        )
        .order_by(Client.slug)
    )


def own_data(client_id):
    client = db.session.scalar(associated_clients(g.user.id).where(Client.client_id == client_id))
    if not client:
        abort(404)
    resources = (
        {r.key: r.label for r in client.developer_app.resources}
        if client.developer_app
        else CATALOG.get(client.slug, {}).get("resources", {})
    )
    return client, resources


@bp.get("/<client_id>/data")
@login_required
def index(client_id):
    client, resources = own_data(client_id)
    prefs = {
        p.resource: p
        for p in db.session.scalars(
            db.select(SyncPreference).where(
                SyncPreference.user_id == g.user.id, SyncPreference.app_slug == client.slug
            )
        )
    }
    return render_template("app_data.html", client=client, resources=resources, preferences=prefs)


@bp.route("/<client_id>/data/<resource>", methods=["GET", "POST"])
@fresh_required
def file(client_id, resource):
    client, resources = own_data(client_id)
    if resource not in resources:
        abort(404)
    connection = db.session.get(CloudConnection, g.user.id)
    if not connection or connection.state != "ready":
        abort(409, "Bitte verbinde zuerst deine ownCloud.")
    if request.method == "POST":
        if request.form.get("confirmation") != resource:
            abort(400, "Gib den Schlüssel der Datenart ein, um die Cloud-Datei zu löschen.")
        db.session.execute(
            db.update(SyncPreference)
            .where(
                SyncPreference.user_id == g.user.id,
                SyncPreference.app_slug == client.slug,
                SyncPreference.resource == resource,
            )
            .values(enabled=False)
        )
        db.session.commit()
    result = dav(
        connection,
        "GET",
        dav_url(connection, client.slug, resource),
        max_bytes=NEWS_MAX_BYTES if client.slug == "news" else MAX_BYTES,
    )
    if request.method == "GET":
        if result.status_code == 404:
            abort(404, "Für diese Datenart ist noch keine Cloud-Datei vorhanden.")
        if result.status_code != 200:
            abort(502, "Die Datei konnte nicht geladen werden.")
        response = make_response(result.content)
        response.headers["Content-Type"] = "application/octet-stream"
        response.headers["Content-Disposition"] = (
            f'attachment; filename="{client.slug}-{resource}.json"'
        )
        return response
    if result.status_code != 404:
        etag = result.headers.get("etag")
        if result.status_code != 200 or not etag or etag.startswith("W/"):
            abort(
                502,
                "Die Dateiversion konnte nicht sicher bestimmt werden. Die Datei wurde nicht gelöscht.",
            )
        deleted = dav(
            connection,
            "DELETE",
            dav_url(connection, client.slug, resource),
            headers={"If-Match": etag},
        )
        if deleted.status_code == 412:
            abort(
                409,
                "Die Cloud-Datei wurde inzwischen geändert. Prüfe sie und wiederhole das Löschen bei Bedarf.",
            )
        if deleted.status_code not in (200, 204, 404):
            abort(502, "Die Cloud-Datei konnte nicht gelöscht werden.")
    flash(
        "Cloud-Datei gelöscht und Sync für diese Datenart ausgeschaltet. Deine lokale Kopie bleibt erhalten.",
        "success",
    )
    return redirect(url_for("app_data.index", client_id=client_id))
