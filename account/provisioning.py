"""ownCloud account lifecycle. App content always remains in ownCloud."""

import json
import secrets
from urllib.parse import quote

import httpx
from flask import current_app

from .extensions import db
from .models import CloudConnection, User, now
from .security import decrypt, encrypt

QUOTA = "1 GB"  # ownCloud's quota unit: 1,073,741,824 bytes.
ERRORS = {
    "not_configured": "Die ownCloud-Einrichtung durch den Betreiber steht noch aus.",
    "unavailable": "ownCloud ist gerade nicht erreichbar. Ein weiterer Versuch folgt automatisch.",
    "permission": "ownCloud hat den Verwaltungszugriff abgelehnt.",
    "collision": "Der Cloud-Benutzername ist bereits anderweitig vergeben.",
    "invalid_response": "ownCloud hat eine unerwartete Antwort geliefert.",
    "quota": "Das Speicherlimit konnte noch nicht bestätigt werden.",
    "rejected": "ownCloud konnte die Änderung noch nicht ausführen.",
}


class ProvisionError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(ERRORS[code])


def configured():
    return bool(
        current_app.config["OWNCLOUD_PROVISION_USER"]
        and current_app.config["OWNCLOUD_PROVISION_PASSWORD"]
    )


def ocs(method, path, data=None, auth=None, allowed=(100,)):
    if auth is None:
        if not configured():
            raise ProvisionError("not_configured")
        auth = (
            current_app.config["OWNCLOUD_PROVISION_USER"],
            current_app.config["OWNCLOUD_PROVISION_PASSWORD"],
        )
    try:
        with httpx.Client(auth=auth, timeout=15, follow_redirects=False, trust_env=False) as client:
            with client.stream(
                method,
                current_app.config["OWNCLOUD_BASE_URL"] + "/ocs/v1.php/cloud/" + path,
                params={"format": "json"},
                headers={"OCS-APIRequest": "true", "Accept": "application/json"},
                data=data,
            ) as response:
                if response.status_code in (401, 403):
                    raise ProvisionError("permission")
                if response.status_code >= 500:
                    raise ProvisionError("unavailable")
                if not 200 <= response.status_code < 300:
                    raise ProvisionError("invalid_response")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 256 * 1024:
                        raise ProvisionError("invalid_response")
                payload = json.loads(body)["ocs"]
                code = int(payload["meta"]["statuscode"])
                if code in (401, 403, 997):
                    raise ProvisionError("permission")
                if code not in allowed:
                    raise ProvisionError("rejected")
                data = payload.get("data") or {}
                if not isinstance(data, dict):
                    raise ProvisionError("invalid_response")
                return code, data
    except httpx.HTTPError:
        raise ProvisionError("unavailable") from None
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ProvisionError("invalid_response") from None


def queue_new_cloud(user):
    """Called inside the account creation transaction; no network or uploads."""
    db.session.flush()
    if db.session.get(CloudConnection, user.id):
        return
    db.session.add(
        CloudConnection(
            user_id=user.id,
            username="jv_" + user.id.replace("-", ""),
            secret=encrypt(secrets.token_urlsafe(48)),
            managed=True,
            state="pending",
        )
    )


def request_retry(connection):
    connection.next_attempt = 0
    connection.last_error = None


def reconcile(user_id):
    """Claim a bounded lease so multiple workers cannot race provisioning."""
    lease = secrets.token_urlsafe(24)
    claimed = db.session.execute(
        db.update(CloudConnection)
        .where(
            CloudConnection.user_id == user_id,
            CloudConnection.managed.is_(True),
            CloudConnection.lease_until < now(),
        )
        .values(lease_until=now() + 300, lease_token=lease)
    )
    db.session.commit()
    if claimed.rowcount != 1:
        return False
    connection = db.session.get(CloudConnection, user_id)
    user = db.session.get(User, user_id)
    try:
        if not user.email_verified or (not user.enabled and connection.remote_enabled is None):
            connection.next_attempt = now() + 60
            return False
        path = "users/" + quote(connection.username, safe="")
        if connection.remote_enabled is None:
            code, _ = ocs(
                "POST",
                "users",
                {
                    "userid": connection.username,
                    "password": decrypt(connection.secret),
                    "groups[]": current_app.config["OWNCLOUD_PROVISION_GROUP"],
                },
                allowed=(100, 102),
            )
            if code == 102:
                # A lost HTTP reply may have created the user. Never reset a
                # pre-existing password; require the original random credential.
                try:
                    ocs("GET", path, auth=(connection.username, decrypt(connection.secret)))
                except ProvisionError:
                    raise ProvisionError("collision") from None
                _, groups = ocs("GET", path + "/groups")
                if current_app.config["OWNCLOUD_PROVISION_GROUP"] not in groups.get("groups", []):
                    raise ProvisionError("collision")
            connection.remote_enabled = True
            db.session.commit()
        # Enforce the quota before SSO or the sync API can expose a new account.
        if connection.state != "ready":
            for key, value in (
                ("quota", QUOTA),
                ("display", user.display_name),
                ("email", user.email),
            ):
                ocs("PUT", path, {"key": key, "value": value})
            _, data = ocs("GET", path)
            definition = str(data.get("quota", {}).get("definition", "")).replace(" ", "").upper()
            if definition not in ("1GB", "1073741824"):
                raise ProvisionError("quota")
        # Read the latest desired state, including a concurrent admin disable.
        db.session.refresh(user)
        desired = user.enabled
        ocs("PUT", path + ("/enable" if desired else "/disable"))
        connection.remote_enabled = desired
        connection.state = "ready"
        connection.last_error = None
        connection.attempts = 0
        connection.next_attempt = now() + 86400
        return True
    except ProvisionError as error:
        if connection.state != "ready":
            connection.state = "error"
        connection.last_error = error.code
        connection.attempts += 1
        connection.next_attempt = now() + min(3600, 30 * 2 ** min(connection.attempts, 7))
        return False
    finally:
        connection.lease_until = 0
        connection.lease_token = None
        db.session.commit()


def reconcile_due(limit=25):
    ids = db.session.scalars(
        db.select(CloudConnection.user_id)
        .join(User)
        .where(
            CloudConnection.managed.is_(True),
            CloudConnection.lease_until < now(),
            User.email_verified.is_(True),
            (CloudConnection.next_attempt <= now())
            | (
                CloudConnection.remote_enabled.is_not(None)
                & (CloudConnection.remote_enabled != User.enabled)
            ),
        )
        .order_by(CloudConnection.next_attempt)
        .limit(limit)
    ).all()
    return [(identifier, reconcile(identifier)) for identifier in ids]


def sso_username(user):
    connection = db.session.get(CloudConnection, user.id)
    if not user.enabled or not user.email_verified or not connection or connection.state != "ready":
        return None
    if connection.managed and (not connection.remote_enabled or connection.last_error):
        return None
    return connection.username
