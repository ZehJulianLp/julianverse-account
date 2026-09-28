import pytest
from test_oidc import issue

from account.extensions import db
from account.models import AppBrowserSession, BrowserSession, OAuthToken, User, digest, now

PATH = "/oauth/browser/startpage"
HEADERS = {"Origin": "https://startpage.test", "X-Julianverse-Session": "1"}


def remember(client):
    tokens, _ = issue(client)
    result = client.post(
        PATH,
        json={"action": "remember"},
        headers={
            **HEADERS,
            "Authorization": "Bearer " + tokens["access_token"],
        },
    )
    assert result.status_code == 200
    return result, tokens


def test_remember_cookie_resume_and_two_tabs(app, logged_in):
    result, original = remember(logged_in)
    cookie = result.headers["Set-Cookie"]
    for flag in ("Secure", "HttpOnly", "SameSite=Lax", "Max-Age=", "Path=" + PATH):
        assert flag in cookie
    assert "Domain=" not in cookie
    assert "refresh_token" not in result.json
    with app.app_context():
        seed = db.session.scalar(
            db.select(OAuthToken).where(OAuthToken.access_hash == digest(original["access_token"]))
        )
        assert seed.refresh_hash is None
        seed.issued_at = now() - 1000
        db.session.commit()
    second = logged_in.post(PATH, json={"action": "token"}, headers=HEADERS)
    assert second.status_code == 200
    for access in (result.json["access_token"], second.json["access_token"]):
        assert (
            logged_in.get(
                "/oauth/userinfo", headers={"Authorization": "Bearer " + access}
            ).status_code
            == 200
        )
    assert logged_in.post(PATH, json={"action": "logout"}, headers=HEADERS).status_code == 200
    assert logged_in.post(PATH, json={"action": "token"}, headers=HEADERS).status_code == 401
    assert (
        logged_in.get(
            "/oauth/userinfo", headers={"Authorization": "Bearer " + second.json["access_token"]}
        ).status_code
        == 401
    )


@pytest.mark.parametrize("change", ["expired", "revoked", "disabled", "family"])
def test_cookie_never_bypasses_expiry_or_revocation(app, logged_in, change):
    remember(logged_in)
    with app.app_context():
        if change == "expired":
            db.session.scalar(db.select(AppBrowserSession)).expires_at = now() - 1
        elif change == "revoked":
            db.session.scalar(db.select(BrowserSession)).revoked = True
        elif change == "disabled":
            db.session.scalar(db.select(User)).enabled = False
        else:
            db.session.scalar(db.select(AppBrowserSession)).token.revoked = True
        db.session.commit()
    assert logged_in.post(PATH, json={"action": "token"}, headers=HEADERS).status_code == 401


def test_browser_session_origin_csrf_and_client_binding(app, logged_in):
    result, _ = remember(logged_in)
    assert result.headers["Access-Control-Allow-Credentials"] == "true"
    assert result.headers["Access-Control-Allow-Origin"] == HEADERS["Origin"]
    for headers in ({}, {"Origin": HEADERS["Origin"]}, {**HEADERS, "Origin": "https://evil.test"}):
        blocked = logged_in.post(PATH, json={"action": "logout"}, headers=headers)
        assert blocked.status_code == 403
    assert logged_in.post(PATH, data={"action": "logout"}, headers=HEADERS).status_code == 403
    assert logged_in.post(PATH, json={"action": "token"}, headers=HEADERS).status_code == 200
    assert logged_in.options(PATH, headers={"Origin": "https://evil.test"}).status_code == 403
    assert (
        logged_in.post(
            "/oauth/browser/weather", json={"action": "token"}, headers=HEADERS
        ).status_code
        == 403
    )
