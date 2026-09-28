import base64
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from conftest import csrf
from test_oidc import authorize, issue

from account.extensions import db
from account.models import (
    AdminEvent,
    BrowserSession,
    Client,
    CloudConnection,
    SyncPreference,
    User,
    digest,
    now,
)
from account.provisioning import queue_new_cloud, reconcile, reconcile_due, sso_username
from account.security import encrypt


def ocs_reply(data=None, code=100):
    return httpx.Response(200, json={"ocs": {"meta": {"statuscode": code}, "data": data or {}}})


def queue(app, verified=True):
    app.config.update(
        OWNCLOUD_PROVISION_USER="provisioner", OWNCLOUD_PROVISION_PASSWORD="service-secret"
    )
    user = db.session.scalar(db.select(User))
    user.email_verified = verified
    queue_new_cloud(user)
    db.session.commit()
    return user, db.session.get(CloudConnection, user.id)


@respx.mock
def test_provision_after_verification_quota_and_retry(app):
    with app.app_context():
        user, cloud = queue(app, verified=False)
        assert not reconcile(user.id)
        assert len(respx.calls) == 0
        user.email_verified = True
        db.session.commit()
        root = "https://cloud.test/ocs/v1.php/cloud/"
        create = respx.post(root + "users").mock(return_value=ocs_reply())
        edit = respx.put(root + "users/" + cloud.username).mock(return_value=ocs_reply(code=997))
        assert not reconcile(user.id)
        assert cloud.last_error == "permission" and cloud.state == "error"
        assert sso_username(user) is None
        edit.mock(return_value=ocs_reply())
        respx.get(root + "users/" + cloud.username).mock(
            return_value=ocs_reply({"quota": {"definition": "1 GB"}})
        )
        enabled = respx.put(root + "users/" + cloud.username + "/enable").mock(
            return_value=ocs_reply()
        )
        assert reconcile(user.id)
        assert create.call_count == 1  # retry never overwrites/recreates the user
        assert enabled.called and cloud.state == "ready" and cloud.remote_enabled
        assert sso_username(user) == cloud.username
        assert any(
            parse_qs(c.request.content.decode()).get("value") == ["1 GB"] for c in edit.calls
        )
        assert not db.session.scalar(db.select(SyncPreference))
        assert not any(c.request.method in ("MKCOL", "PROPFIND") for c in respx.calls)
        assert reconcile_due() == []


@respx.mock
def test_collision_never_resets_existing_cloud_password(app):
    with app.app_context():
        user, cloud = queue(app)
        base = "https://cloud.test/ocs/v1.php/cloud/"
        respx.post(base + "users").mock(return_value=ocs_reply(code=102))
        respx.get(base + "users/" + cloud.username).respond(401)
        assert not reconcile(user.id)
        assert cloud.last_error == "collision"
        assert cloud.remote_enabled is None
        assert all(call.request.method != "PUT" for call in respx.calls)


@respx.mock
def test_lost_create_reply_recovers_only_our_owned_group_account(app):
    with app.app_context():
        user, cloud = queue(app)
        base = "https://cloud.test/ocs/v1.php/cloud/"
        respx.post(base + "users").mock(return_value=ocs_reply(code=102))
        path = base + "users/" + cloud.username
        respx.get(path).mock(return_value=ocs_reply({"quota": {"definition": "1 GB"}}))
        respx.get(path + "/groups").mock(
            return_value=ocs_reply({"groups": ["julianverse-account"]})
        )
        respx.put(path).mock(return_value=ocs_reply())
        respx.put(path + "/enable").mock(return_value=ocs_reply())
        assert reconcile(user.id)
        assert cloud.state == "ready"


@respx.mock
def test_lease_and_quota_gate_prevent_premature_access(app, logged_in):
    with app.app_context():
        user, cloud = queue(app)
        cloud.lease_until = now() + 100
        db.session.commit()
        assert not reconcile(user.id) and not respx.calls
        cloud.lease_until = 0
        db.session.commit()
        base = "https://cloud.test/ocs/v1.php/cloud/"
        respx.post(base + "users").mock(return_value=ocs_reply())
        respx.put(base + "users/" + cloud.username).mock(return_value=ocs_reply())
        respx.get(base + "users/" + cloud.username).mock(
            return_value=ocs_reply({"quota": {"definition": "none"}})
        )
        assert not reconcile(user.id) and cloud.last_error == "quota"
    assert (
        logged_in.post(
            "/sync/preferences",
            data={"csrf_token": csrf(logged_in, "/sync"), "resources": "startpage/notes"},
        ).status_code
        == 409
    )


def admin_and_target(app):
    with app.app_context():
        admin = db.session.scalar(db.select(User))
        admin.is_admin = True
        other = User(
            username="second-user",
            email="second@example.org",
            display_name="Second",
            email_verified=True,
        )
        db.session.add(other)
        db.session.flush()
        db.session.add(
            BrowserSession(id="target-session", user_id=other.id, expires_at=now() + 600)
        )
        db.session.commit()
        return admin.id, other.id


def test_admin_permissions_csrf_roles_and_revocation(app, logged_in):
    assert logged_in.get("/admin/").status_code == 403
    admin, other = admin_and_target(app)
    page = logged_in.get("/admin/")
    assert page.status_code == 200 and "Second" in page.text
    assert logged_in.post(f"/admin/users/{other}", data={"action": "disable"}).status_code == 400
    token = csrf(logged_in, "/admin/")
    for action in ("disable", "remove-admin"):
        assert (
            logged_in.post(
                f"/admin/users/{admin}", data={"csrf_token": token, "action": action}
            ).status_code
            == 409
        )
    assert (
        logged_in.post(
            f"/admin/users/{other}", data={"csrf_token": token, "action": "make-admin"}
        ).status_code
        == 302
    )
    assert (
        logged_in.post(
            f"/admin/users/{other}", data={"csrf_token": token, "action": "disable"}
        ).status_code
        == 302
    )
    with app.app_context():
        assert not db.session.get(User, other).enabled
        assert db.session.get(BrowserSession, "target-session").revoked
        assert db.session.scalar(db.select(AdminEvent)).action == "make-admin"
    assert (
        logged_in.post(
            "/privacy/delete", data={"csrf_token": token, "confirmation": "julian"}
        ).status_code
        == 409
    )


def test_admin_write_requires_recent_login(app, logged_in):
    _, other = admin_and_target(app)
    with app.app_context():
        session = db.session.scalar(
            db.select(BrowserSession).where(BrowserSession.id != "target-session")
        )
        session.authenticated_at = now() - 601
        db.session.commit()
    response = logged_in.post(
        f"/admin/users/{other}",
        data={"csrf_token": csrf(logged_in, "/admin/"), "action": "disable"},
    )
    assert response.status_code == 302 and "/auth/confirm" in response.location
    with app.app_context():
        assert db.session.get(User, other).enabled


@respx.mock
def test_admin_disable_reconciles_managed_cloud(app, logged_in):
    _, other = admin_and_target(app)
    with app.app_context():
        db.session.add(
            CloudConnection(
                user_id=other,
                username="jv-managed",
                secret=encrypt("private"),
                managed=True,
                state="ready",
                remote_enabled=True,
            )
        )
        db.session.commit()
    logged_in.post(
        f"/admin/users/{other}",
        data={"csrf_token": csrf(logged_in, "/admin/"), "action": "disable"},
    )
    app.config.update(OWNCLOUD_PROVISION_USER="service", OWNCLOUD_PROVISION_PASSWORD="secret")
    disable = respx.put("https://cloud.test/ocs/v1.php/cloud/users/jv-managed/disable").mock(
        return_value=ocs_reply()
    )
    with app.app_context():
        assert reconcile_due() == [(other, True)]
        assert disable.called
        assert not db.session.get(CloudConnection, other).remote_enabled


def cloud_client(app):
    with app.app_context():
        user = db.session.scalar(db.select(User))
        db.session.add(
            CloudConnection(user_id=user.id, username="legacy-julian", secret=encrypt("private"))
        )
        c = Client(slug="owncloud", client_id="cloud-client", client_secret=digest("cloud-secret"))
        c.set_client_metadata(
            dict(
                client_name="ownCloud",
                redirect_uris=["https://cloud.test/callback"],
                scope="openid profile email owncloud",
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="client_secret_basic",
            )
        )
        db.session.add(c)
        db.session.commit()
    return {"Authorization": "Basic " + base64.b64encode(b"cloud-client:cloud-secret").decode()}


def test_owncloud_oidc_claim_introspection_isolation_and_unlink(app, logged_in):
    headers = cloud_client(app)
    response, verifier = authorize(
        logged_in,
        client_id="cloud-client",
        redirect_uri="https://cloud.test/callback",
        scope="openid profile email owncloud",
    )
    code = parse_qs(urlsplit(response.location).query)["code"][0]
    response = logged_in.post(
        "/oauth/token",
        headers=headers,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": "https://cloud.test/callback",
        },
    )
    assert response.status_code == 200, response.text
    token = response.json
    userinfo = logged_in.get(
        "/oauth/userinfo", headers={"Authorization": "Bearer " + token["access_token"]}
    )
    assert userinfo.json["owncloud_username"] == "legacy-julian"
    active = logged_in.post(
        "/oauth/introspect", headers=headers, data={"token": token["access_token"]}
    )
    assert (
        active.status_code == 200
        and active.json["active"]
        and active.json["aud"] == ["cloud-client"]
    )
    assert not logged_in.post(
        "/oauth/introspect", headers=headers, data={"token": token["refresh_token"]}
    ).json["active"]
    other_token, _ = issue(logged_in)
    assert not logged_in.post(
        "/oauth/introspect", headers=headers, data={"token": other_token["access_token"]}
    ).json["active"]
    other_info = logged_in.get(
        "/oauth/userinfo", headers={"Authorization": "Bearer " + other_token["access_token"]}
    )
    assert "owncloud_username" not in other_info.json
    assert (
        logged_in.post("/oauth/introspect", data={"token": token["access_token"]}).status_code
        == 401
    )
    logged_in.post(
        "/connections/owncloud/disconnect", data={"csrf_token": csrf(logged_in, "/connections")}
    )
    assert not logged_in.post(
        "/oauth/introspect", headers=headers, data={"token": token["access_token"]}
    ).json["active"]


@respx.mock
def test_existing_cloud_signup_skips_creation(app, client):
    response = client.post(
        "/auth/register",
        data={
            "csrf_token": csrf(client, "/auth/register"),
            "username": "old-cloud-user",
            "email": "old@example.org",
            "password": "long-test-password",
            "password_confirm": "long-test-password",
            "existing_cloud": "yes",
        },
    )
    assert response.status_code == 302
    with app.app_context():
        user = db.session.scalar(db.select(User).where(User.username == "old-cloud-user"))
        assert not db.session.get(CloudConnection, user.id)
    assert not respx.calls


@pytest.mark.parametrize("canonical", ["other-cloud", "Other-Cloud"])
@respx.mock
def test_migration_prevents_duplicate_bindings_and_uses_authenticated_identity(
    app, logged_in, canonical
):
    with app.app_context():
        other = User(username="second-user", email="second@example.org", display_name="Second")
        db.session.add(other)
        db.session.flush()
        db.session.add(
            CloudConnection(user_id=other.id, username="other-cloud", secret=encrypt("password"))
        )
        db.session.commit()
    respx.request("PROPFIND", "https://cloud.test/remote.php/dav/").respond(
        207,
        text=f"""<d:multistatus xmlns:d="DAV:"><d:response><d:propstat><d:prop><d:current-user-principal><d:href>/remote.php/dav/principals/users/{canonical}/</d:href></d:current-user-principal></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>""",
    )
    response = logged_in.post(
        "/connections/owncloud",
        data={
            "csrf_token": csrf(logged_in, "/connections"),
            "username": "email-alias@example.org",
            "password": "password",
        },
    )
    assert response.status_code == 409
    with app.app_context():
        assert len(db.session.scalars(db.select(CloudConnection)).all()) == 1
    assert len(respx.calls) == 1  # migration never writes quota, groups, or files
