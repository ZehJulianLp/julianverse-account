import io
import json
import posixpath
import re
import zipfile
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
import respx
from conftest import csrf
from test_oidc import authorize

from account.extensions import db
from account.models import (
    AppError,
    AppResource,
    Client,
    CloudConnection,
    DeveloperApp,
    SyncPreference,
    User,
)
from account.security import encrypt


def create_app_record(app, browser, **changes):
    data = dict(
        name="Notizgarten",
        description="Private Notizen",
        icon="plant",
        website="https://garden.example/",
        callbacks="https://garden.example/account-callback.html",
        visibility="private",
        test_mode="on",
    )
    data.update(changes)
    data["csrf_token"] = csrf(browser, "/developer/apps/new")
    response = browser.post("/developer/apps/new", data=data)
    assert response.status_code == 302, response.text
    slug = response.location.rsplit("/", 1)[1]
    with app.app_context():
        item = db.session.scalar(db.select(Client).where(Client.slug == slug))
        return slug, item.client_id


def add_resource(browser, slug, key="notes", enabled="on"):
    path = f"/developer/apps/{slug}"
    return browser.post(
        path + "/resources",
        data=dict(
            csrf_token=csrf(browser, path),
            key=key,
            label="Meine " + key,
            description="Persönliche Inhalte",
            enabled=enabled,
        ),
    )


def app_token(browser, identifier):
    response, verifier = authorize(
        browser,
        client_id=identifier,
        redirect_uri="https://garden.example/account-callback.html",
        scope="openid profile sync",
    )
    assert response.status_code == 302, response.text
    code = parse_qs(urlsplit(response.location).query)["code"][0]
    result = browser.post(
        "/oauth/token",
        data=dict(
            grant_type="authorization_code",
            client_id=identifier,
            redirect_uri="https://garden.example/account-callback.html",
            code=code,
            code_verifier=verifier,
        ),
    )
    assert result.status_code == 200, result.text
    return {"Authorization": "Bearer " + result.json["access_token"]}


def other_user(app):
    with app.app_context():
        user = User(
            username="other",
            display_name="Andere Person",
            email="other@example.org",
            email_verified=True,
        )
        user.set_password("a-long-test-password")
        db.session.add(user)
        db.session.commit()
    browser = app.test_client()
    browser.environ_base["HTTP_REFERER"] = "https://account.test/"
    response = browser.post(
        "/auth/login",
        data=dict(
            csrf_token=csrf(browser, "/auth/login"),
            identifier="other",
            password="a-long-test-password",
        ),
    )
    assert response.status_code == 302
    return browser


def share(browser, slug):
    path = f"/developer/apps/{slug}/edit"
    result = browser.post(
        path,
        data=dict(
            csrf_token=csrf(browser, path),
            name="Notizgarten",
            description="Notizen",
            website="https://garden.example/",
            callbacks="https://garden.example/account-callback.html",
            icon="plant",
            visibility="unlisted",
        ),
    )
    assert result.status_code == 302, result.text


def connect_cloud(app, username, cloud_name):
    with app.app_context():
        user = db.session.scalar(db.select(User).where(User.username == username))
        db.session.add(
            CloudConnection(user_id=user.id, username=cloud_name, secret=encrypt("test-only"))
        )
        db.session.commit()
        return user.id


def grant(browser, slug, *resources):
    result = browser.post(
        "/sync/preferences",
        data={
            "csrf_token": csrf(browser, "/sync"),
            "resources": [f"{slug}/{r}" for r in resources],
        },
    )
    assert result.status_code == 302, result.text


def test_owner_registry_resources_and_starter_are_private_by_default(app, logged_in):
    slug, identifier = create_app_record(app, logged_in)
    assert slug.startswith("app-") and len(slug) == 28
    assert add_resource(logged_in, slug).status_code == 302
    assert add_resource(logged_in, slug, "settings").status_code == 302
    page = logged_in.get(f"/developer/apps/{slug}")
    assert "Testmodus" in page.text and identifier in page.text
    assert logged_in.get("/developer/docs").status_code == 200
    assert "Meine Apps" in logged_in.get("/overview").text
    with app.app_context():
        item = db.session.get(DeveloperApp, identifier)
        assert item.test_mode and item.visibility == "private"
        assert item.client.token_endpoint_auth_method == "none"
        assert item.client.client_secret == ""
        assert item.client.scope == "openid profile sync"
        assert not db.session.scalar(db.select(SyncPreference))
    package = logged_in.get(f"/developer/apps/{slug}/starter.zip")
    assert package.status_code == 200
    with zipfile.ZipFile(io.BytesIO(package.data)) as archive:
        names = archive.namelist()
        assert "index.html" in names and "account-callback.html" in names
        config = json.loads(
            archive.read("account/config.mjs").decode().split(" = ", 1)[1].rstrip(";\n")
        )
        assert config["clientId"] == identifier
        assert config["resources"][0]["key"] == "notes"
        assert "client_secret" not in config
        assert all(not n.startswith("/") and ".." not in n.split("/") for n in names)
        for name in names:
            if name.endswith(".mjs"):
                for spec in re.findall(
                    r'(?:from\s+|import\s*\()["\'](\.[^"\']+)["\']', archive.read(name).decode()
                ):
                    assert (
                        posixpath.normpath(posixpath.join(posixpath.dirname(name), spec)) in names
                    ), (name, spec)
    visitor = other_user(app)
    for suffix in ("", "/edit", "/starter.zip"):
        assert visitor.get(f"/developer/apps/{slug}{suffix}").status_code == 404
    assert visitor.get(f"/developer/app/{slug}").status_code == 404
    assert (
        visitor.post(
            f"/developer/apps/{slug}/state",
            data=dict(
                csrf_token=csrf(visitor, "/overview"), action="delete", confirmation="Notizgarten"
            ),
        ).status_code
        == 404
    )
    assert (
        logged_in.post(
            f"/developer/apps/{slug}/resources", data=dict(key="stolen", label="No CSRF")
        ).status_code
        == 400
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"website": "javascript:alert(1)"},
        {"website": "https://x.example/../escape/"},
        {"callbacks": "https://garden.example/%2e%2e/escape.html"},
        {"callbacks": "https://other.example/callback.html"},
        {"callbacks": "https://garden.example/callback.html?redirect=evil"},
        {"callbacks": "https://garden.example/*"},
        {"callbacks": "https://garden.example/"},
        {"website": "http://garden.example/"},
        {"visibility": "public"},
        {"website": "https://account.test/", "callbacks": "https://account.test/callback.html"},
    ],
)
def test_registration_rejects_unsafe_or_unimplemented_configuration(app, logged_in, changes):
    data = dict(
        name="Example",
        website="https://garden.example/",
        callbacks="https://garden.example/callback.html",
        test_mode="on",
        visibility="private",
    )
    data.update(changes)
    data["csrf_token"] = csrf(logged_in, "/developer/apps/new")
    assert logged_in.post("/developer/apps/new", data=data).status_code == 400
    with app.app_context():
        assert not db.session.scalar(db.select(DeveloperApp))


def test_private_test_mode_pkce_and_changing_access_revoke_existing_tokens(app, logged_in):
    slug, identifier = create_app_record(app, logged_in)
    add_resource(logged_in, slug)
    owner_token = app_token(logged_in, identifier)
    visitor = other_user(app)
    response, _ = authorize(
        visitor,
        client_id=identifier,
        redirect_uri="https://garden.example/account-callback.html",
        scope="openid profile sync",
    )
    assert response.status_code == 403
    share(logged_in, slug)
    assert logged_in.get("/oauth/userinfo", headers=owner_token).status_code == 401
    assert visitor.get(f"/developer/app/{slug}").status_code == 200
    token = app_token(visitor, identifier)
    assert "email" not in visitor.get("/oauth/userinfo", headers=token).json
    origin = {"Origin": "https://garden.example", "X-Julianverse-Session": "1"}
    remembered = visitor.post(
        f"/oauth/browser/{slug}", json={"action": "remember"}, headers={**token, **origin}
    )
    assert remembered.status_code == 200
    assert "SameSite=None" in remembered.headers["Set-Cookie"]
    assert "HttpOnly" in remembered.headers["Set-Cookie"]
    assert (
        visitor.post(f"/oauth/browser/{slug}", json={"action": "token"}, headers=origin).status_code
        == 200
    )
    assert (
        visitor.post(
            f"/oauth/browser/{slug}",
            json={"action": "token"},
            headers={**origin, "Origin": "https://evil.test"},
        ).status_code
        == 403
    )
    result = logged_in.post(
        f"/developer/apps/{slug}/state",
        data=dict(csrf_token=csrf(logged_in, f"/developer/apps/{slug}"), action="disable"),
    )
    assert result.status_code == 302
    assert visitor.get("/oauth/userinfo", headers=token).status_code == 401
    assert (
        visitor.post(f"/oauth/browser/{slug}", json={"action": "token"}, headers=origin).status_code
        == 403
    )


@respx.mock
def test_sync_is_separately_granted_namespaced_and_user_data_survives_app_deletion(app, logged_in):
    slug, identifier = create_app_record(app, logged_in)
    add_resource(logged_in, slug)
    add_resource(logged_in, slug, "settings")
    share(logged_in, slug)
    visitor = other_user(app)
    user_id = connect_cloud(app, "other", "other-cloud")
    token = app_token(visitor, identifier)
    assert visitor.get(f"/api/sync/{slug}", headers=token).json["resources"] == {
        "notes": False,
        "settings": False,
    }
    assert visitor.get(f"/api/sync/{slug}/notes", headers=token).status_code == 403
    assert visitor.get("/api/sync/startpage/notes", headers=token).status_code == 403
    assert visitor.get(f"/api/sync/{slug}/unknown", headers=token).status_code == 404
    grant(visitor, slug, "notes")
    assert visitor.get(f"/api/sync/{slug}/settings", headers=token).status_code == 403
    base = "https://cloud.test/remote.php/dav/files/other-cloud/Julianverse/"
    respx.request("MKCOL", base).respond(405)
    respx.request("MKCOL", base + slug + "/").respond(201)
    payload = {"schemaVersion": 1, "data": {"value": {"private": "belongs to other user"}}}
    target = base + slug + "/notes.json"
    upload = respx.put(target).respond(201, headers={"ETag": '"v1"'})
    result = visitor.put(
        f"/api/sync/{slug}/notes", json=payload, headers={**token, "If-None-Match": "*"}
    )
    assert result.status_code == 200 and json.loads(upload.calls[0].request.content) == payload
    respx.get(target).respond(200, json=payload, headers={"ETag": '"v1"'})
    assert visitor.get(f"/api/sync/{slug}/notes", headers=token).json == payload
    assert logged_in.get(f"/apps/{identifier}/data/notes").status_code == 404
    with app.app_context():
        item = db.session.get(DeveloperApp, identifier)
        assert item.write_count == 1 and item.read_count >= 1
        errors = db.session.scalars(db.select(AppError)).all()
        assert errors and all(e.status == 403 for e in errors)
    page = logged_in.get(f"/developer/apps/{slug}")
    assert "belongs to other user" not in page.text and "other@example.org" not in page.text
    result = logged_in.post(
        f"/developer/apps/{slug}/state",
        data=dict(
            csrf_token=csrf(logged_in, f"/developer/apps/{slug}"),
            action="delete",
            confirmation="Notizgarten",
        ),
    )
    assert result.status_code == 302
    assert visitor.get(f"/api/sync/{slug}/notes", headers=token).status_code == 401
    assert logged_in.get(f"/developer/apps/{slug}").status_code == 404
    assert visitor.get(f"/developer/app/{slug}").status_code == 404
    assert visitor.get("/apps").status_code == 200
    assert (
        visitor.get(f"/apps/{identifier}/data/notes").json is None
    )  # download uses attachment MIME
    assert json.loads(visitor.get(f"/apps/{identifier}/data/notes").data) == payload
    removal = respx.delete(target).respond(204)
    path = f"/apps/{identifier}/data"
    assert (
        visitor.post(
            path + "/notes", data=dict(csrf_token=csrf(visitor, path), confirmation="wrong")
        ).status_code
        == 400
    )
    result = visitor.post(
        path + "/notes", data=dict(csrf_token=csrf(visitor, path), confirmation="notes")
    )
    assert result.status_code == 302 and removal.calls[0].request.headers["If-Match"] == '"v1"'
    with app.app_context():
        assert not db.session.scalar(
            db.select(SyncPreference).where(
                SyncPreference.user_id == user_id,
                SyncPreference.app_slug == slug,
                SyncPreference.resource == "notes",
            )
        ).enabled


def test_new_and_reenabled_resources_never_inherit_permission(app, logged_in):
    slug, identifier = create_app_record(app, logged_in)
    add_resource(logged_in, slug)
    connect_cloud(app, "julian", "own-cloud")
    headers = app_token(logged_in, identifier)
    grant(logged_in, slug, "notes")
    add_resource(logged_in, slug, "settings")
    assert logged_in.get(f"/api/sync/{slug}", headers=headers).json["resources"] == {
        "notes": True,
        "settings": False,
    }
    add_resource(logged_in, slug, enabled="")
    assert logged_in.get(f"/api/sync/{slug}/notes", headers=headers).status_code == 404
    add_resource(logged_in, slug)
    assert logged_in.get(f"/api/sync/{slug}", headers=headers).json["resources"]["notes"] is False
    with app.app_context():
        assert (
            db.session.scalar(
                db.select(db.func.count())
                .select_from(AppResource)
                .where(AppResource.client_id == identifier)
            )
            == 2
        )


def test_test_mode_unlisted_apps_still_reject_other_users_and_owner_suspension(app, logged_in):
    slug, identifier = create_app_record(app, logged_in, visibility="unlisted")
    headers = app_token(logged_in, identifier)
    visitor = other_user(app)
    response, _ = authorize(
        visitor,
        client_id=identifier,
        redirect_uri="https://garden.example/account-callback.html",
        scope="openid profile sync",
    )
    assert response.status_code == 403
    assert visitor.get(f"/developer/app/{slug}").status_code == 404
    with app.app_context():
        owner = db.session.scalar(db.select(User).where(User.username == "julian"))
        owner.enabled = False
        db.session.commit()
    assert visitor.get("/oauth/userinfo", headers=headers).status_code == 401


def test_disabled_owner_app_never_becomes_a_builtin_after_owner_deletion(app, logged_in):
    slug, identifier = create_app_record(app, logged_in)
    add_resource(logged_in, slug)
    share(logged_in, slug)
    visitor = other_user(app)
    token = app_token(visitor, identifier)
    with app.app_context():
        owner = db.session.scalar(db.select(User).where(User.username == "julian"))
        db.session.delete(owner)
        db.session.commit()
        db.session.expire_all()
        assert db.session.get(DeveloperApp, identifier).owner_id is None
    assert visitor.get("/oauth/userinfo", headers=token).status_code == 401
    assert visitor.get(f"/developer/app/{slug}").status_code == 404


def test_starter_preserves_nested_paths_and_escaped_untrusted_text(app, logged_in):
    slug, _ = create_app_record(
        app,
        logged_in,
        name='<script>alert("test")</script>',
        website="https://garden.example/tools/notes/",
        callbacks="https://garden.example/login/callback.html",
    )
    detail = logged_in.get(f"/developer/apps/{slug}")
    assert '<script>alert("test")</script>' not in detail.text
    package = logged_in.get(f"/developer/apps/{slug}/starter.zip")
    with zipfile.ZipFile(io.BytesIO(package.data)) as archive:
        assert "tools/notes/index.html" in archive.namelist()
        assert (
            'src="../tools/notes/account/callback.mjs"'
            in archive.read("login/callback.html").decode()
        )
        assert "tools/notes/account/config.mjs" in archive.namelist()
        assert (
            '<script>alert("test")</script>' not in archive.read("tools/notes/index.html").decode()
        )


def test_https_normalization_and_localhost_only_in_test_mode(app, logged_in):
    slug, _ = create_app_record(
        app,
        logged_in,
        website="https://GARDEN.EXAMPLE:443/",
        callbacks="https://garden.example/account-callback.html",
    )
    with app.app_context():
        client = db.session.scalar(db.select(Client).where(Client.slug == slug))
        assert client.developer_app.website == "https://garden.example/"
    local, _ = create_app_record(
        app,
        logged_in,
        website="http://localhost:8000/",
        callbacks="http://localhost:8000/account-callback.html",
    )
    path = f"/developer/apps/{local}/edit"
    result = logged_in.post(
        path,
        data=dict(
            csrf_token=csrf(logged_in, path),
            name="Local",
            website="http://localhost:8000/",
            callbacks="http://localhost:8000/account-callback.html",
            visibility="unlisted",
        ),
    )
    assert result.status_code == 400


@respx.mock
def test_user_revocation_and_cloud_delete_conflict_keep_file_and_namespace(app, logged_in):
    slug, identifier = create_app_record(app, logged_in)
    add_resource(logged_in, slug)
    connect_cloud(app, "julian", "own-cloud")
    token = app_token(logged_in, identifier)
    grant(logged_in, slug, "notes")
    response = logged_in.post(
        f"/apps/{identifier}/revoke", data=dict(csrf_token=csrf(logged_in, "/apps"))
    )
    assert response.status_code == 302
    assert logged_in.get("/oauth/userinfo", headers=token).status_code == 401
    data_page = f"/apps/{identifier}/data"
    assert logged_in.get(data_page).status_code == 200
    target = f"https://cloud.test/remote.php/dav/files/own-cloud/Julianverse/{slug}/notes.json"
    respx.get(target).respond(200, json={"schemaVersion": 1, "data": {}}, headers={"ETag": '"old"'})
    deletion = respx.delete(target).respond(412)
    result = logged_in.post(
        data_page + "/notes", data=dict(csrf_token=csrf(logged_in, data_page), confirmation="notes")
    )
    assert result.status_code == 409 and deletion.call_count == 1


def test_custom_app_origins_only_enter_csp_for_the_current_login_flow(app, logged_in):
    _, identifier = create_app_record(app, logged_in)
    create_app_record(
        app,
        logged_in,
        website="https://second.example/",
        callbacks="https://second.example/callback.html",
    )
    headers = logged_in.get("/overview").headers["Content-Security-Policy"]
    assert "garden.example" not in headers and "second.example" not in headers
    target = "/oauth/authorize?" + urlencode({"client_id": identifier})
    path = "/auth/login?" + urlencode({"next": target})
    response = logged_in.get(path)
    assert "https://garden.example" in response.headers["Content-Security-Policy"]
    assert "second.example" not in response.headers["Content-Security-Policy"]
    response, _ = authorize(
        logged_in,
        client_id=identifier,
        redirect_uri="https://garden.example/account-callback.html",
        scope="openid profile sync",
    )
    assert "https://garden.example" in response.headers["Content-Security-Policy"]
    assert "second.example" not in response.headers["Content-Security-Policy"]
