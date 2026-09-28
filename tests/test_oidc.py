import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlencode, urlsplit

from conftest import csrf
from joserfc import jwt
from joserfc.jwk import KeySet

from account.extensions import db
from account.models import OAuthToken, digest


def authorize(client, **changes):
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    params = dict(
        client_id="startpage-client",
        redirect_uri="https://startpage.test/callback",
        response_type="code",
        scope="openid profile email sync",
        state=secrets.token_urlsafe(24),
        nonce=secrets.token_urlsafe(24),
        code_challenge=challenge,
        code_challenge_method="S256",
    )
    params.update(changes)
    path = "/oauth/authorize?" + urlencode(params)
    response = client.get(path)
    if response.status_code == 200:
        response = client.post(path, data={"csrf_token": csrf(client, path), "decision": "allow"})
    return response, verifier


def issue(client):
    response, verifier = authorize(client)
    assert response.status_code == 302, response.text
    code = parse_qs(urlsplit(response.location).query)["code"][0]
    data = dict(
        grant_type="authorization_code",
        code=code,
        client_id="startpage-client",
        redirect_uri="https://startpage.test/callback",
        code_verifier=verifier,
    )
    response = client.post("/oauth/token", data=data)
    assert response.status_code == 200, response.text
    return response.json, data


def test_oidc_pkce_signed_identity_and_hashed_storage(app, logged_in):
    token, _ = issue(logged_in)
    keys = KeySet.import_key_set(logged_in.get("/oauth/jwks").json)
    decoded = jwt.decode(token["id_token"], keys, algorithms=["RS256"])
    assert decoded.claims["iss"] == "https://account.test"
    assert decoded.claims["aud"] == ["startpage-client"]
    info = logged_in.get(
        "/oauth/userinfo", headers={"Authorization": "Bearer " + token["access_token"]}
    )
    assert info.status_code == 200 and info.json["sub"] == decoded.claims["sub"]
    assert info.json["email"] == "julian@example.org"
    with app.app_context():
        stored = db.session.scalar(db.select(OAuthToken))
        assert stored.access_hash == digest(token["access_token"])
        assert stored.refresh_hash == digest(token["refresh_token"])


def test_redirect_allowlist_and_pkce_required(logged_in):
    response, _ = authorize(logged_in, redirect_uri="https://evil.test/callback")
    assert response.status_code == 400 and not response.location
    response, _ = authorize(logged_in, code_challenge_method="plain")
    assert response.status_code == 400
    response, _ = authorize(logged_in, code_challenge="")
    assert response.status_code == 400


def test_wrong_verifier_and_code_replay(logged_in):
    response, verifier = authorize(logged_in)
    code = parse_qs(urlsplit(response.location).query)["code"][0]
    data = dict(
        grant_type="authorization_code",
        code=code,
        client_id="startpage-client",
        redirect_uri="https://startpage.test/callback",
        code_verifier="x" * 64,
    )
    assert logged_in.post("/oauth/token", data=data).status_code == 400
    data["code_verifier"] = verifier
    response = logged_in.post("/oauth/token", data=data)
    assert response.status_code == 200
    token = response.json["access_token"]
    assert logged_in.post("/oauth/token", data=data).status_code == 400
    assert (
        logged_in.get("/oauth/userinfo", headers={"Authorization": "Bearer " + token}).status_code
        == 401
    )


def test_refresh_rotation_replay_revokes_family(logged_in):
    token, _ = issue(logged_in)
    refresh = dict(
        grant_type="refresh_token",
        client_id="startpage-client",
        refresh_token=token["refresh_token"],
    )
    rotated = logged_in.post("/oauth/token", data=refresh)
    assert rotated.status_code == 200, rotated.text
    assert rotated.json["refresh_token"] != token["refresh_token"]
    assert logged_in.post("/oauth/token", data=refresh).status_code == 400
    assert (
        logged_in.get(
            "/oauth/userinfo", headers={"Authorization": "Bearer " + rotated.json["access_token"]}
        ).status_code
        == 401
    )


def test_logout_revokes_app_access(logged_in):
    token, _ = issue(logged_in)
    logged_in.post("/auth/logout", data={"csrf_token": csrf(logged_in, "/overview")})
    assert (
        logged_in.get(
            "/oauth/userinfo", headers={"Authorization": "Bearer " + token["access_token"]}
        ).status_code
        == 401
    )


def test_token_cors_exact_origins(logged_in):
    for path in ("/.well-known/openid-configuration", "/oauth/jwks"):
        assert (
            logged_in.get(path, headers={"Origin": "https://startpage.test"}).headers[
                "Access-Control-Allow-Origin"
            ]
            == "*"
        )
    for origin, allowed in (
        ("https://startpage.test", True),
        ("https://startpage.test.evil.org", False),
    ):
        response = logged_in.options("/oauth/token", headers={"Origin": origin})
        assert (response.headers.get("Access-Control-Allow-Origin") == origin) == allowed
        assert "Access-Control-Allow-Credentials" not in response.headers


def test_silent_auth_returns_errors_to_validated_callback(client, logged_in):
    response, _ = authorize(logged_in, prompt="none")
    assert response.status_code == 302
    assert parse_qs(urlsplit(response.location).query)["error"] == ["consent_required"]
    logged_in.post("/auth/logout", data={"csrf_token": csrf(logged_in, "/overview")})
    response, _ = authorize(client, prompt="none")
    assert response.status_code == 302
    assert parse_qs(urlsplit(response.location).query)["error"] == ["login_required"]
    response, _ = authorize(client, prompt="none", redirect_uri="https://evil.test")
    assert response.status_code == 400 and not response.location


def test_revoking_app_invalidates_unredeemed_code(logged_in):
    response, verifier = authorize(logged_in)
    code = parse_qs(urlsplit(response.location).query)["code"][0]
    assert (
        logged_in.post(
            "/apps/startpage-client/revoke", data={"csrf_token": csrf(logged_in, "/apps")}
        ).status_code
        == 302
    )
    # Even a later new consent must not reactivate the old authorization code.
    authorize(logged_in)
    response = logged_in.post(
        "/oauth/token",
        data=dict(
            grant_type="authorization_code",
            code=code,
            client_id="startpage-client",
            redirect_uri="https://startpage.test/callback",
            code_verifier=verifier,
        ),
    )
    assert response.status_code == 400
