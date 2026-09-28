import pyotp
from conftest import csrf

from account.extensions import db
from account.models import (
    BrowserSession,
    Challenge,
    CloudConnection,
    Passkey,
    User,
)


def test_csrf_login_and_redirect_safety(client):
    assert (
        client.post(
            "/auth/login", data={"identifier": "julian", "password": "a-long-test-password"}
        ).status_code
        == 400
    )
    response = client.post(
        "/auth/login",
        data={
            "identifier": "julian",
            "password": "a-long-test-password",
            "csrf_token": csrf(client),
            "next": "//evil.test",
        },
    )
    assert response.location == "/overview"
    assert "HttpOnly" in response.headers["Set-Cookie"]
    assert "Secure" in response.headers["Set-Cookie"]


def test_account_pages(logged_in):
    for route in (
        "/overview",
        "/profile",
        "/security",
        "/connections",
        "/apps",
        "/sync",
        "/privacy",
    ):
        response = logged_in.get(route)
        assert response.status_code == 200, (route, response.text)
        assert "Content-Security-Policy" in response.headers
        assert response.headers["Cache-Control"] == "no-store"


def test_registration_requires_email_and_creates_no_sync(app, client):
    response = client.post(
        "/auth/register",
        data={
            "csrf_token": csrf(client, "/auth/register"),
            "username": "new-user",
            "email": "new@example.org",
            "password": "new-long-password",
            "password_confirm": "new-long-password",
        },
    )
    assert response.status_code == 302
    message = app.extensions["mail_outbox"][-1]
    assert message["kind"] == "verify"
    with app.app_context():
        user = db.session.scalar(db.select(User).where(User.username == "new-user"))
        assert not user.email_verified
        assert not db.session.get(CloudConnection, user.id)
        assert not db.session.scalar(
            db.select(BrowserSession).where(BrowserSession.user_id == user.id)
        )
    link = message["url"].replace("https://account.test", "")
    assert client.post(link, data={"csrf_token": csrf(client, link)}).status_code == 302
    assert client.get(link).status_code == 400
    with app.app_context():
        assert db.session.scalar(db.select(User).where(User.username == "new-user")).email_verified


def test_password_reset_revokes_sessions_and_single_use(app, logged_in):
    logged_in.post(
        "/auth/recover",
        data={"csrf_token": csrf(logged_in, "/auth/recover"), "email": "julian@example.org"},
    )
    link = app.extensions["mail_outbox"][-1]["url"].replace("https://account.test", "")
    response = logged_in.post(
        link,
        data={
            "csrf_token": csrf(logged_in, link),
            "password": "replacement-password",
            "password_confirm": "replacement-password",
        },
    )
    assert response.status_code == 302
    assert logged_in.get("/overview").status_code == 302
    assert logged_in.get(link).status_code == 400
    with app.app_context():
        assert all(s.revoked for s in db.session.scalars(db.select(BrowserSession)))


def test_email_change_is_confirmed_before_update(app, logged_in):
    response = logged_in.post(
        "/profile/email",
        data={"csrf_token": csrf(logged_in, "/profile"), "email": "changed@example.org"},
    )
    assert response.status_code == 302
    with app.app_context():
        assert db.session.scalar(db.select(User)).email == "julian@example.org"
    link = app.extensions["mail_outbox"][-1]["url"].replace("https://account.test", "")
    assert logged_in.post(link, data={"csrf_token": csrf(logged_in, link)}).status_code == 302
    with app.app_context():
        user = db.session.scalar(db.select(User))
        assert user.email == "changed@example.org" and user.email_verified


def test_totp_setup_login_and_replay(app, logged_in):
    import re

    response = logged_in.post(
        "/security/totp/start", data={"csrf_token": csrf(logged_in, "/security")}
    )
    assert response.status_code == 200
    secret = re.search(r'<code class="secret">([^<]+)', response.text).group(1)
    challenge = re.search(r'name="challenge" value="([^"]+)"', response.text).group(1)
    token = re.search(r'<meta name="csrf-token" content="([^"]+)"', response.text).group(1)
    response = logged_in.post(
        "/security/totp/confirm",
        data={"csrf_token": token, "challenge": challenge, "code": pyotp.TOTP(secret).now()},
    )
    assert response.status_code == 200
    recovery = re.search(r'<pre class="recovery-codes">([a-f0-9]+)', response.text).group(1)
    logged_in.post("/auth/logout", data={"csrf_token": csrf(logged_in, "/security")})
    response = logged_in.post(
        "/auth/login",
        data={
            "csrf_token": csrf(logged_in),
            "identifier": "julian",
            "password": "a-long-test-password",
        },
    )
    assert response.location == "/auth/second-factor"
    assert logged_in.get("/overview").status_code == 302
    token = csrf(logged_in, "/auth/second-factor")
    # The setup code has already been consumed.
    response = logged_in.post(
        "/auth/second-factor", data={"csrf_token": token, "code": pyotp.TOTP(secret).now()}
    )
    assert response.status_code == 200 and "ungültig" in response.text
    assert (
        logged_in.post(
            "/auth/second-factor", data={"csrf_token": token, "code": recovery}
        ).status_code
        == 302
    )
    assert logged_in.get("/overview").status_code == 200


def test_passkey_challenge_browser_bound_and_single_use(app, logged_in):
    token = csrf(logged_in, "/security")
    response = logged_in.post(
        "/api/passkeys/register/options", json={}, headers={"X-CSRFToken": token}
    )
    assert response.status_code == 200
    data = response.json
    assert data["options"]["authenticatorSelection"]["userVerification"] == "required"
    assert data["options"]["rp"]["id"] == "account.test"
    # Invalid credentials consume their server challenge without creating a passkey.
    response = logged_in.post(
        "/api/passkeys/register/verify",
        json={"challenge": data["challenge"], "credential": {}},
        headers={"X-CSRFToken": token},
    )
    assert response.status_code == 400
    with app.app_context():
        assert db.session.get(Challenge, data["challenge"]).used
        assert not db.session.scalar(db.select(Passkey))


def test_export_and_delete_cascade(app, logged_in):
    token = csrf(logged_in, "/privacy")
    export = logged_in.post("/privacy/export", data={"csrf_token": token})
    assert export.status_code == 200
    assert export.json["profile"]["username"] == "julian"
    assert "password_hash" not in export.text
    assert (
        logged_in.post(
            "/privacy/delete", data={"csrf_token": token, "confirmation": "wrong"}
        ).status_code
        == 400
    )
    assert (
        logged_in.post(
            "/privacy/delete", data={"csrf_token": token, "confirmation": "julian"}
        ).status_code
        == 302
    )
    with app.app_context():
        assert not db.session.scalar(db.select(User))
        assert not db.session.scalar(db.select(BrowserSession))


def test_rate_limit_atomic_and_shared(app, client):
    app.config["RATE_LIMIT_DISABLED"] = False
    token = csrf(client)
    for _ in range(10):
        assert (
            client.post(
                "/auth/login",
                data={"csrf_token": token, "identifier": "julian", "password": "wrong"},
            ).status_code
            == 400
        )
    assert (
        client.post(
            "/auth/login", data={"csrf_token": token, "identifier": "julian", "password": "wrong"}
        ).status_code
        == 429
    )
