import re
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from flask_migrate import upgrade

from account import create_app
from account.extensions import db
from account.models import Client, User


@pytest.fixture(scope="session")
def signing_key(tmp_path_factory):
    path = tmp_path_factory.mktemp("keys") / "oidc.pem"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return path


@pytest.fixture
def app(tmp_path, signing_key):
    app = create_app(
        dict(
            TESTING=True,
            SECRET_KEY="test-secret-" * 5,
            ENCRYPTION_KEY=Fernet.generate_key().decode(),
            SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'test.sqlite'}",
            BASE_URL="https://account.test",
            SERVER_NAME="account.test",
            PREFERRED_URL_SCHEME="https",
            SIGNING_KEY_PATH=str(signing_key),
            RATE_LIMIT_DISABLED=True,
            OWNCLOUD_BASE_URL="https://cloud.test",
            DISCORD_CLIENT_ID="",
            DISCORD_CLIENT_SECRET="",
        )
    )
    with app.app_context():
        upgrade(directory=str(Path(__file__).resolve().parents[1] / "migrations"))
        user = User(
            username="julian",
            email="julian@example.org",
            display_name="Julian",
            email_verified=True,
        )
        user.set_password("a-long-test-password")
        db.session.add(user)
        client = Client(slug="startpage", client_id="startpage-client", client_secret="")
        client.set_client_metadata(
            dict(
                client_name="Startpage",
                redirect_uris=["https://startpage.test/callback"],
                scope="openid profile email sync",
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="none",
            )
        )
        db.session.add(client)
        db.session.commit()
    yield app
    with app.app_context():
        db.session.remove()
        db.engine.dispose()


@pytest.fixture
def client(app):
    client = app.test_client()
    client.environ_base["HTTP_REFERER"] = "https://account.test/"
    return client


def csrf(client, path="/auth/login"):
    response = client.get(path)
    assert response.status_code == 200, response.data.decode()
    return re.search(r'<meta name="csrf-token" content="([^"]+)"', response.text).group(1)


@pytest.fixture
def logged_in(client):
    token = csrf(client)
    response = client.post(
        "/auth/login",
        data={"identifier": "julian", "password": "a-long-test-password", "csrf_token": token},
    )
    assert response.status_code == 302
    return client
