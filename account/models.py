import hashlib
import secrets
import time
import uuid

from authlib.integrations.sqla_oauth2 import OAuth2AuthorizationCodeMixin, OAuth2ClientMixin
from sqlalchemy import UniqueConstraint
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db


def now():
    return int(time.time())


def uid():
    return str(uuid.uuid4())


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class User(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=uid)
    username = db.Column(db.String(40), unique=True, nullable=False)
    email = db.Column(db.String(254), unique=True, nullable=False)
    display_name = db.Column(db.String(80), nullable=False)
    password_hash = db.Column(db.Text, nullable=True)
    email_verified = db.Column(db.Boolean, nullable=False, default=False)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    is_admin = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    locale = db.Column(db.String(5), nullable=False, default="de")
    theme = db.Column(db.String(10), nullable=False, default="system")
    created_at = db.Column(db.Integer, nullable=False, default=now)
    totp_secret = db.Column(db.Text, nullable=True)
    totp_last_counter = db.Column(db.Integer, nullable=False, default=-1)

    def set_password(self, value):
        self.password_hash = generate_password_hash(value, method="scrypt")

    def check_password(self, value):
        return bool(self.password_hash and check_password_hash(self.password_hash, value))

    def get_user_id(self):
        return self.id


class BrowserSession(db.Model):
    id = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    user = db.relationship(User)
    created_at = db.Column(db.Integer, nullable=False, default=now)
    expires_at = db.Column(db.Integer, nullable=False)
    last_seen = db.Column(db.Integer, nullable=False, default=now)
    authenticated_at = db.Column(db.Integer, nullable=False, default=now)
    method = db.Column(db.String(32), nullable=False, default="password")
    user_agent = db.Column(db.String(240), nullable=False, default="")
    revoked = db.Column(db.Boolean, nullable=False, default=False)


class DiscordIdentity(db.Model):
    id = db.Column(db.String(32), primary_key=True)
    user_id = db.Column(db.ForeignKey("user.id", ondelete="CASCADE"), unique=True, nullable=False)
    username = db.Column(db.String(100), nullable=False)
    linked_at = db.Column(db.Integer, nullable=False, default=now)


class RecoveryCode(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    code_hash = db.Column(db.String(64), nullable=False, unique=True)
    used_at = db.Column(db.Integer, nullable=True)


class Passkey(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=uid)
    user_id = db.Column(db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    credential_id = db.Column(db.Text, unique=True, nullable=False)
    public_key = db.Column(db.LargeBinary, nullable=False)
    sign_count = db.Column(db.Integer, nullable=False, default=0)
    name = db.Column(db.String(80), nullable=False)
    created_at = db.Column(db.Integer, nullable=False, default=now)
    last_used = db.Column(db.Integer, nullable=True)


class Challenge(db.Model):
    id = db.Column(db.String(64), primary_key=True, default=lambda: secrets.token_urlsafe(32))
    kind = db.Column(db.String(32), nullable=False)
    browser_hash = db.Column(db.String(64), nullable=False)
    user_id = db.Column(db.ForeignKey("user.id", ondelete="CASCADE"), nullable=True)
    payload = db.Column(db.Text, nullable=False)
    expires_at = db.Column(db.Integer, nullable=False)
    used = db.Column(db.Boolean, nullable=False, default=False)


class MailAction(db.Model):
    id = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    kind = db.Column(db.String(24), nullable=False)
    email = db.Column(db.String(254), nullable=False)
    expires_at = db.Column(db.Integer, nullable=False)
    used = db.Column(db.Boolean, nullable=False, default=False)


class RateBucket(db.Model):
    key = db.Column(db.String(64), primary_key=True)
    starts_at = db.Column(db.Integer, nullable=False)
    count = db.Column(db.Integer, nullable=False, default=0)


class CloudConnection(db.Model):
    user_id = db.Column(db.ForeignKey("user.id", ondelete="CASCADE"), primary_key=True)
    username = db.Column(db.String(254), nullable=False)
    secret = db.Column(db.Text, nullable=False)
    connected_at = db.Column(db.Integer, nullable=False, default=now)
    # The persistent SSO binding survives disabling sync. Existing cloud accounts
    # are only linked after proof of ownership, never by matching email addresses.
    username_key = db.Column(db.String(254), unique=True, nullable=False)
    managed = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    state = db.Column(db.String(16), nullable=False, default="ready", server_default="ready")
    remote_enabled = db.Column(db.Boolean, nullable=True)
    last_error = db.Column(db.String(32), nullable=True)
    attempts = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    next_attempt = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    lease_until = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    lease_token = db.Column(db.String(64), nullable=True)

    def __init__(self, **kwargs):
        if "username" in kwargs:
            kwargs.setdefault("username_key", kwargs["username"].casefold())
        super().__init__(**kwargs)


class AdminEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.ForeignKey("user.id", ondelete="SET NULL"), nullable=True)
    target_id = db.Column(db.ForeignKey("user.id", ondelete="SET NULL"), nullable=True)
    action = db.Column(db.String(40), nullable=False)
    created_at = db.Column(db.Integer, nullable=False, default=now)


class SyncPreference(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    app_slug = db.Column(db.String(40), nullable=False)
    resource = db.Column(db.String(60), nullable=False)
    enabled = db.Column(db.Boolean, nullable=False, default=False)
    last_sync = db.Column(db.Integer, nullable=True)
    __table_args__ = (UniqueConstraint("user_id", "app_slug", "resource"),)


class Client(db.Model, OAuth2ClientMixin):
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(40), unique=True, nullable=False)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    client_id = db.Column(db.String(48), unique=True, nullable=False)

    def get_allowed_scope(self, scope):
        allowed = super().get_allowed_scope(scope)
        if self.slug != "owncloud":
            allowed = " ".join(s for s in allowed.split() if s != "owncloud")
        return allowed

    def check_client_secret(self, client_secret):
        return bool(
            self.client_secret and secrets.compare_digest(self.client_secret, digest(client_secret))
        )

    def check_endpoint_auth_method(self, method, endpoint):
        return self.token_endpoint_auth_method == method


class Consent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    client_id = db.Column(db.ForeignKey("client.client_id", ondelete="CASCADE"), nullable=False)
    scopes = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.Integer, nullable=False, default=now)
    __table_args__ = (UniqueConstraint("user_id", "client_id"),)


class AuthorizationCode(db.Model, OAuth2AuthorizationCodeMixin):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    session_id = db.Column(db.ForeignKey("browser_session.id", ondelete="CASCADE"), nullable=False)
    created_at = db.Column(db.Integer, nullable=False, default=now)
    used_at = db.Column(db.Integer, nullable=True)

    def is_expired(self):
        return self.created_at + 180 <= now()


class OAuthToken(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.ForeignKey("client.client_id", ondelete="CASCADE"), nullable=False)
    user_id = db.Column(db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    session_id = db.Column(db.ForeignKey("browser_session.id", ondelete="CASCADE"), nullable=False)
    user = db.relationship(User)
    browser_session = db.relationship(BrowserSession)
    client = db.relationship(Client)
    code_id = db.Column(db.ForeignKey("authorization_code.id", ondelete="CASCADE"), nullable=False)
    family = db.Column(db.String(36), nullable=False, index=True, default=uid)
    access_hash = db.Column(db.String(64), unique=True, nullable=False)
    refresh_hash = db.Column(db.String(64), unique=True, nullable=True)
    scope = db.Column(db.Text, nullable=False)
    issued_at = db.Column(db.Integer, nullable=False, default=now)
    expires_in = db.Column(db.Integer, nullable=False, default=600)
    refresh_expires_at = db.Column(db.Integer, nullable=False)
    revoked = db.Column(db.Boolean, nullable=False, default=False)
    refresh_used = db.Column(db.Boolean, nullable=False, default=False)

    def check_client(self, client):
        return self.client_id == client.client_id

    def get_scope(self):
        return self.scope

    def is_expired(self):
        return self.issued_at + self.expires_in <= now()

    def is_revoked(self):
        if self.client and self.client.slug == "owncloud":
            from .provisioning import sso_username

            if not self.user or not sso_username(self.user):
                return True
        s = self.browser_session
        return (
            self.revoked
            or not self.user
            or not self.user.enabled
            or not self.client
            or not self.client.enabled
            or not s
            or s.revoked
            or s.expires_at <= now()
        )


class AppBrowserSession(db.Model):
    id = db.Column(db.String(64), primary_key=True)
    token_id = db.Column(db.ForeignKey("o_auth_token.id", ondelete="CASCADE"), nullable=False)
    token = db.relationship(OAuthToken)
    origin = db.Column(db.String(255), nullable=False)
    expires_at = db.Column(db.Integer, nullable=False, index=True)
