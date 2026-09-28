from account.extensions import db
from account.models import DiscordIdentity, User, now


def mock_callback(app, monkeypatch, identity):
    oauth = app.extensions["discord_oauth"].discord
    monkeypatch.setattr(oauth, "authorize_access_token", lambda: {"access_token": "transient"})

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return identity

    monkeypatch.setattr(oauth, "get", lambda *args, **kwargs: Response())


def test_discord_email_does_not_silently_merge(app, client, monkeypatch):
    mock_callback(
        app,
        monkeypatch,
        {
            "id": "123456789123456789",
            "username": "discord-name",
            "email": "julian@example.org",
            "verified": True,
        },
    )
    with client.session_transaction() as session:
        session["discord_context"] = {
            "mode": "login",
            "user_id": None,
            "next": "/overview",
            "started_at": now(),
        }
    response = client.get("/auth/discord/callback")
    assert response.status_code in (302, 409)
    with app.app_context():
        assert not db.session.scalar(db.select(DiscordIdentity))
        assert len(db.session.scalars(db.select(User)).all()) == 1
    assert client.get("/overview").status_code == 302


def test_discord_callback_without_flow_is_rejected(client):
    assert client.get("/auth/discord/callback?code=unsolicited&state=invalid").status_code == 400


def test_discord_verifies_new_account_email(app, client, monkeypatch):
    mock_callback(
        app,
        monkeypatch,
        {
            "id": "123456789123456789",
            "username": "new-discord",
            "email": "new@example.org",
            "verified": False,
        },
    )
    with client.session_transaction() as session:
        session["discord_context"] = {
            "mode": "login",
            "user_id": None,
            "next": "/overview",
            "started_at": now(),
        }
    response = client.get("/auth/discord/callback")
    assert response.status_code == 400
    with app.app_context():
        assert not db.session.scalar(db.select(DiscordIdentity))


def test_discord_only_account_can_reauthenticate_with_totp(app, logged_in, monkeypatch):
    import pyotp
    from conftest import csrf

    from account.models import BrowserSession
    from account.security import encrypt

    secret = pyotp.random_base32()
    with app.app_context():
        user = db.session.scalar(db.select(User))
        user.password_hash = None
        user.totp_secret = encrypt(secret)
        identity = DiscordIdentity(
            id="123456789123456789", user_id=user.id, username="julian-discord"
        )
        db.session.add(identity)
        browser = db.session.scalar(db.select(BrowserSession))
        browser.authenticated_at = now() - 900
        user_id, session_id = user.id, browser.id
        db.session.commit()
    mock_callback(app, monkeypatch, {"id": "123456789123456789", "username": "julian-discord"})
    with logged_in.session_transaction() as session:
        session["discord_context"] = {
            "mode": "reauth",
            "user_id": user_id,
            "next": "/security",
            "started_at": now(),
        }
    assert logged_in.get("/auth/discord/callback").location == "/auth/second-factor"
    response = logged_in.post(
        "/auth/second-factor",
        data={
            "csrf_token": csrf(logged_in, "/auth/second-factor"),
            "code": pyotp.TOTP(secret).now(),
        },
    )
    assert response.location == "/security"
    with app.app_context():
        assert db.session.get(BrowserSession, session_id).authenticated_at >= now() - 2
        assert not db.session.get(BrowserSession, session_id).revoked
