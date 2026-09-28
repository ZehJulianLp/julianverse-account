import httpx
import respx
from conftest import csrf
from test_oidc import issue

from account.extensions import db
from account.models import CloudConnection, SyncPreference, User
from account.security import encrypt


def setup_cloud(app, enabled=False):
    with app.app_context():
        user = db.session.scalar(db.select(User))
        db.session.add(
            CloudConnection(
                user_id=user.id, username="cloud-user", secret=encrypt("private-app-password")
            )
        )
        db.session.add(
            SyncPreference(user_id=user.id, app_slug="startpage", resource="notes", enabled=enabled)
        )
        db.session.commit()


def auth_headers(client):
    token, _ = issue(client)
    return {"Authorization": "Bearer " + token["access_token"]}


@respx.mock
def test_connect_validates_credentials_but_does_not_upload(app, logged_in):
    root = respx.request("PROPFIND", "https://cloud.test/remote.php/dav/files/cloud-user/").respond(
        207
    )
    response = logged_in.post(
        "/connections/owncloud",
        data={
            "csrf_token": csrf(logged_in, "/connections"),
            "username": "cloud-user",
            "password": "private-app-password",
        },
    )
    assert response.status_code == 302
    assert root.call_count == 1 and len(respx.calls) == 1
    assert root.calls[0].request.headers["Depth"] == "0"
    with app.app_context():
        connection = db.session.scalar(db.select(CloudConnection))
        assert connection.secret != "private-app-password"
        assert not db.session.scalar(
            db.select(SyncPreference).where(SyncPreference.enabled.is_(True))
        )


@respx.mock
def test_sync_off_by_default_and_app_isolation(app, logged_in):
    setup_cloud(app)
    headers = auth_headers(logged_in)
    assert logged_in.get("/api/sync/startpage/notes", headers=headers).status_code == 403
    assert logged_in.get("/api/sync/weather/settings", headers=headers).status_code == 403
    assert logged_in.get("/api/sync/startpage/passwords", headers=headers).status_code == 404
    assert len(respx.calls) == 0


@respx.mock
def test_cloud_is_source_of_truth_and_conditional_writes(app, logged_in):
    setup_cloud(app, enabled=True)
    headers = auth_headers(logged_in)
    remote = "https://cloud.test/remote.php/dav/files/cloud-user/Julianverse/startpage/notes.json"
    respx.get(remote).respond(
        200,
        headers={"ETag": '"version-1"'},
        json={"schemaVersion": 1, "data": {"text": "Changed directly in ownCloud"}},
    )
    response = logged_in.get("/api/sync/startpage/notes", headers=headers)
    assert response.json["data"]["text"] == "Changed directly in ownCloud"
    assert response.headers["ETag"] == '"version-1"'
    document = {"schemaVersion": 1, "data": {"text": "local edit"}}
    assert (
        logged_in.put("/api/sync/startpage/notes", json=document, headers=headers).status_code
        == 428
    )
    for directory in ("Julianverse/", "Julianverse/startpage/"):
        respx.request(
            "MKCOL", "https://cloud.test/remote.php/dav/files/cloud-user/" + directory
        ).respond(405)
    write = respx.put(remote).respond(412)
    response = logged_in.put(
        "/api/sync/startpage/notes", json=document, headers={**headers, "If-Match": '"version-1"'}
    )
    assert response.status_code == 412 and response.json["error"] == "conflict"
    assert write.calls[0].request.headers["If-Match"] == '"version-1"'
    write.respond(204, headers={"ETag": '"version-3"'})
    response = logged_in.put(
        "/api/sync/startpage/notes", json=document, headers={**headers, "If-Match": '"version-2"'}
    )
    assert response.status_code == 200 and response.headers["ETag"] == '"version-3"'
    with app.app_context():
        assert db.session.scalar(db.select(SyncPreference)).last_sync
        # Content never lands in SQLite, including its schema.
        assert "data" not in SyncPreference.__table__.columns


@respx.mock
def test_remote_failure_is_not_treated_as_missing_data(app, logged_in):
    setup_cloud(app, enabled=True)
    headers = auth_headers(logged_in)
    remote = respx.get(
        "https://cloud.test/remote.php/dav/files/cloud-user/Julianverse/startpage/notes.json"
    )
    for status in (401, 403, 500, 503, 302):
        remote.respond(status, headers={"Location": "https://evil.test/"})
        assert logged_in.get("/api/sync/startpage/notes", headers=headers).status_code == 502
    remote.mock(side_effect=httpx.ConnectError("offline"))
    assert logged_in.get("/api/sync/startpage/notes", headers=headers).status_code == 502
    remote.respond(404)
    assert logged_in.get("/api/sync/startpage/notes", headers=headers).json["error"] == "not_found"


@respx.mock
def test_disconnect_disables_sync_keeps_remote_files(app, logged_in):
    setup_cloud(app, enabled=True)
    headers = auth_headers(logged_in)
    response = logged_in.post(
        "/connections/owncloud/disconnect", data={"csrf_token": csrf(logged_in, "/connections")}
    )
    assert response.status_code == 302
    assert len(respx.calls) == 0
    assert logged_in.get("/api/sync/startpage", headers=headers).status_code == 401
    with app.app_context():
        assert not db.session.scalar(db.select(CloudConnection))
        assert not db.session.scalar(db.select(SyncPreference)).enabled
