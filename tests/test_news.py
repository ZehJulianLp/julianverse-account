"""Small API/adapter checks only: no browser processes or live user data."""

import json
import runpy
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import respx
from test_oidc import authorize, issue

from account.extensions import db
from account.models import Client, CloudConnection, SyncPreference, User
from account.security import encrypt

ROOT = Path(__file__).resolve().parents[1]


def setup_news(app, logged_in):
    with app.app_context():
        client = Client(slug="news", client_id="news-client", client_secret="")
        client.set_client_metadata(
            dict(
                client_name="Julianverse News",
                redirect_uris=["https://julianverse.test/news/account-callback.html"],
                scope="openid profile sync",
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="none",
            )
        )
        db.session.add(client)
        user = db.session.scalar(db.select(User))
        db.session.add(
            CloudConnection(user_id=user.id, username="news-user", secret=encrypt("test-only"))
        )
        db.session.commit()
    response, verifier = authorize(
        logged_in,
        client_id="news-client",
        redirect_uri="https://julianverse.test/news/account-callback.html",
        scope="openid profile sync",
    )
    code = parse_qs(urlsplit(response.location).query)["code"][0]
    response = logged_in.post(
        "/oauth/token",
        data=dict(
            grant_type="authorization_code",
            client_id="news-client",
            redirect_uri="https://julianverse.test/news/account-callback.html",
            code=code,
            code_verifier=verifier,
        ),
    )
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json["access_token"]}


def test_news_public_login_is_persistent_but_never_enables_sync(app, logged_in):
    headers = setup_news(app, logged_in)
    status = logged_in.get("/api/sync/news", headers=headers)
    assert status.status_code == 200
    assert status.json["resources"] == dict(sources=False, saved=False, read=False, settings=False)
    assert logged_in.get("/api/sync/news/saved", headers=headers).status_code == 403
    assert logged_in.get("/api/sync/weather/settings", headers=headers).status_code == 403
    assert logged_in.get("/api/sync/news/arbitrary", headers=headers).status_code == 404
    origin = {"Origin": "https://julianverse.test", "X-Julianverse-Session": "1"}
    response = logged_in.post(
        "/oauth/browser/news", json={"action": "remember"}, headers={**headers, **origin}
    )
    assert response.status_code == 200
    for flag in ("Secure", "HttpOnly", "SameSite=Lax", "Path=/oauth/browser/news"):
        assert flag in response.headers["Set-Cookie"]
    assert (
        logged_in.post("/oauth/browser/news", json={"action": "token"}, headers=origin).status_code
        == 200
    )
    assert (
        logged_in.post(
            "/oauth/browser/news",
            json={"action": "token"},
            headers={**origin, "Origin": "https://evil.test"},
        ).status_code
        == 403
    )
    assert (
        logged_in.post("/oauth/browser/news", json={"action": "logout"}, headers=origin).status_code
        == 200
    )
    assert (
        logged_in.post("/oauth/browser/news", json={"action": "token"}, headers=origin).status_code
        == 401
    )


@respx.mock
def test_large_news_documents_use_owncloud_etags_and_keep_other_apps_limited(app, logged_in):
    headers = setup_news(app, logged_in)
    with app.app_context():
        user = db.session.scalar(db.select(User))
        db.session.add(
            SyncPreference(user_id=user.id, app_slug="news", resource="saved", enabled=True)
        )
        db.session.add(
            SyncPreference(user_id=user.id, app_slug="startpage", resource="notes", enabled=True)
        )
        db.session.commit()
    base = "https://cloud.test/remote.php/dav/files/news-user/Julianverse/"
    for suffix in ("", "news/"):
        respx.request("MKCOL", base + suffix).respond(405)
    document = {
        "schemaVersion": 1,
        "data": {
            "saved": {
                f"https://example.org/{i}": {"title": f"Article {i}", "summary": "x" * 3000}
                for i in range(500)
            }
        },
    }
    assert len(json.dumps(document)) > 1024 * 1024
    target = respx.put(base + "news/saved.json").respond(201, headers={"ETag": '"news-v1"'})
    response = logged_in.put(
        "/api/sync/news/saved", json=document, headers={**headers, "If-None-Match": "*"}
    )
    assert response.status_code == 200, response.json
    assert json.loads(target.calls[0].request.content) == document
    respx.get(base + "news/saved.json").respond(200, json=document, headers={"ETag": '"news-v1"'})
    response = logged_in.get("/api/sync/news/saved", headers=headers)
    assert response.status_code == 200 and response.json == document
    target.respond(412)
    assert (
        logged_in.put(
            "/api/sync/news/saved", json=document, headers={**headers, "If-Match": '"news-v1"'}
        ).status_code
        == 412
    )
    tokens, _ = issue(logged_in)
    start_headers = {"Authorization": "Bearer " + tokens["access_token"], "If-None-Match": "*"}
    assert (
        logged_in.put("/api/sync/startpage/notes", json=document, headers=start_headers).status_code
        == 413
    )
    respx.get(base + "news/saved.json").respond(200, content=b"x" * (8 * 1024 * 1024 + 1))
    assert logged_in.get("/api/sync/news/saved", headers=headers).status_code == 502


def test_news_proxy_patch_is_repeatable_and_changes_only_news_routes():
    helpers = runpy.run_path(str(ROOT / "scripts/setup-news-proxy.py"))
    account = (ROOT / "deploy/account.nginx.conf").read_text()
    assert helpers["patch_account"](account) == account
    assert "client_max_body_size 1m;" in account and "client_max_body_size 8m;" in account
    original = "http {\n server { listen 443 ssl; server_name julianverse.de; root /srv/http; }\n server { listen 443 ssl; server_name other.test; }\n}"
    patched = helpers["patch_main"](original)
    assert helpers["patch_main"](patched) == patched
    assert "server_name other.test;" in patched
    assert "location = /news/account-callback.html" in patched
    assert "access_log off;" in patched and 'Cache-Control "no-store"' in patched
