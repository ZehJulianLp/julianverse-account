from pathlib import Path
from urllib.parse import urlsplit

from authlib.integrations.flask_oauth2 import AuthorizationServer
from authlib.oauth2.rfc6749 import InvalidGrantError, InvalidRequestError, OAuth2Error
from authlib.oauth2.rfc6749.grants import AuthorizationCodeGrant, RefreshTokenGrant
from authlib.oauth2.rfc7009 import RevocationEndpoint
from authlib.oauth2.rfc7636 import CodeChallenge
from authlib.oidc.core import UserInfo
from authlib.oidc.core.errors import ConsentRequiredError, LoginRequiredError
from authlib.oidc.core.grants import OpenIDCode
from flask import (
    Blueprint,
    abort,
    current_app,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from joserfc.jwk import RSAKey

from .extensions import csrf, db
from .models import (
    AuthorizationCode,
    BrowserSession,
    Client,
    Consent,
    OAuthToken,
    digest,
    now,
)
from .security import rate_limit, safe_next

bp = Blueprint("oidc", __name__)
SCOPES = {
    "openid": "Deine eindeutige Konto-ID",
    "profile": "Anzeigename und Benutzername",
    "email": "E-Mail-Adresse und Bestätigungsstatus",
    "sync": "Freigegebene Sync-Dateien dieser App in ownCloud",
}


def query_client(identifier):
    return db.session.scalar(
        db.select(Client).where(Client.client_id == identifier, Client.enabled.is_(True))
    )


def revoke_family(family):
    db.session.execute(
        db.update(OAuthToken).where(OAuthToken.family == family).values(revoked=True)
    )
    db.session.commit()


def save_token(token, oauth_request):
    code = oauth_request.authorization_code
    previous = oauth_request.refresh_token
    if previous:
        changed = db.session.execute(
            db.update(OAuthToken)
            .where(
                OAuthToken.id == previous.id,
                OAuthToken.refresh_used.is_(False),
                OAuthToken.revoked.is_(False),
            )
            .values(refresh_used=True, revoked=True)
        )
        if changed.rowcount != 1:
            db.session.rollback()
            revoke_family(previous.family)
            raise InvalidGrantError("Refresh token already used.")
        params = dict(
            code_id=previous.code_id,
            session_id=previous.session_id,
            family=previous.family,
            refresh_expires_at=previous.refresh_expires_at,
        )
    else:
        changed = db.session.execute(
            db.update(AuthorizationCode)
            .where(AuthorizationCode.id == code.id, AuthorizationCode.used_at.is_(None))
            .values(used_at=now())
        )
        if changed.rowcount != 1:
            db.session.rollback()
            db.session.execute(
                db.update(OAuthToken).where(OAuthToken.code_id == code.id).values(revoked=True)
            )
            db.session.commit()
            raise InvalidGrantError("Authorization code already used.")
        params = dict(
            code_id=code.id, session_id=code.session_id, refresh_expires_at=now() + 30 * 86400
        )
    db.session.add(
        OAuthToken(
            client_id=oauth_request.client.client_id,
            user_id=oauth_request.user.id,
            access_hash=digest(token["access_token"]),
            refresh_hash=digest(token["refresh_token"]) if token.get("refresh_token") else None,
            scope=token.get("scope", ""),
            expires_in=token["expires_in"],
            **params,
        )
    )
    db.session.commit()


class PKCE(CodeChallenge):
    def validate_code_challenge(self, grant, redirect_uri):
        data = grant.request.payload.data
        if not data.get("code_challenge") or data.get("code_challenge_method") != "S256":
            raise InvalidRequestError("PKCE with S256 is required for every client.")
        return super().validate_code_challenge(grant, redirect_uri)


class CodeGrant(AuthorizationCodeGrant):
    TOKEN_ENDPOINT_AUTH_METHODS = ["client_secret_basic", "client_secret_post", "none"]

    def save_authorization_code(self, code, request):
        data = request.payload.data
        db.session.add(
            AuthorizationCode(
                code=digest(code),
                client_id=request.client.client_id,
                redirect_uri=request.payload.redirect_uri,
                scope=request.scope,
                user_id=request.user.id,
                session_id=g.login_session.id,
                nonce=data.get("nonce"),
                auth_time=g.login_session.authenticated_at,
                code_challenge=data.get("code_challenge"),
                code_challenge_method="S256",
            )
        )
        db.session.commit()

    def query_authorization_code(self, code, client):
        item = db.session.scalar(
            db.select(AuthorizationCode).where(
                AuthorizationCode.code == digest(code),
                AuthorizationCode.client_id == client.client_id,
            )
        )
        if item and item.used_at:
            db.session.execute(
                db.update(OAuthToken).where(OAuthToken.code_id == item.id).values(revoked=True)
            )
            db.session.commit()
            return None
        return item if item and not item.is_expired() else None

    def delete_authorization_code(self, authorization_code):
        # Keep consumed codes to detect replay; consumption is atomic with token storage.
        pass

    def authenticate_user(self, authorization_code):
        browser = db.session.get(BrowserSession, authorization_code.session_id)
        consent = db.session.scalar(
            db.select(Consent).where(
                Consent.user_id == authorization_code.user_id,
                Consent.client_id == authorization_code.client_id,
            )
        )
        if (
            browser
            and not browser.revoked
            and browser.expires_at > now()
            and browser.user.enabled
            and consent
        ):
            return browser.user


class RefreshGrant(RefreshTokenGrant):
    TOKEN_ENDPOINT_AUTH_METHODS = CodeGrant.TOKEN_ENDPOINT_AUTH_METHODS
    INCLUDE_NEW_REFRESH_TOKEN = True

    def authenticate_refresh_token(self, refresh_token):
        token = db.session.scalar(
            db.select(OAuthToken).where(OAuthToken.refresh_hash == digest(refresh_token))
        )
        if not token or token.client_id != self.request.client.client_id:
            return None
        if token.refresh_used:
            revoke_family(token.family)
            return None
        if token.refresh_expires_at > now() and not token.is_revoked():
            return token

    def authenticate_user(self, token):
        return token.user

    def revoke_old_credential(self, token):
        pass  # Atomically consumed in save_token.


def user_info(user, scope):
    result = UserInfo(sub=user.id)
    scopes = set(scope.split())
    if "profile" in scopes:
        result.update(name=user.display_name, preferred_username=user.username, locale=user.locale)
    if "email" in scopes:
        result.update(email=user.email, email_verified=user.email_verified)
    return result


def signing_key():
    key = current_app.extensions.get("oidc_signing_key")
    if not key:
        path = Path(current_app.config["SIGNING_KEY_PATH"])
        if not path.exists():
            abort(503, "Der OIDC-Signaturschlüssel fehlt. Bitte init-secrets ausführen.")
        key = RSAKey.import_key(path.read_bytes())
        key.ensure_kid()
        current_app.extensions["oidc_signing_key"] = key
    return key


class OpenID(OpenIDCode):
    def resolve_client_private_key(self, client):
        return signing_key()

    def get_client_algorithm(self, client):
        return "RS256"

    def get_encode_header(self, client):
        return {"alg": "RS256", "kid": signing_key().kid}

    def get_client_claims(self, client):
        return {
            "iss": current_app.config["BASE_URL"],
            "aud": [client.client_id],
            "exp": now() + 600,
        }

    def exists_nonce(self, nonce, request):
        return bool(
            db.session.scalar(
                db.select(AuthorizationCode.id).where(
                    AuthorizationCode.client_id == request.payload.client_id,
                    AuthorizationCode.nonce == nonce,
                )
            )
        )

    def generate_user_info(self, user, scope):
        return user_info(user, scope)


class Revoke(RevocationEndpoint):
    CLIENT_AUTH_METHODS = CodeGrant.TOKEN_ENDPOINT_AUTH_METHODS

    def query_token(self, value, hint):
        hashed = digest(value)
        return db.session.scalar(
            db.select(OAuthToken).where(
                (OAuthToken.access_hash == hashed) | (OAuthToken.refresh_hash == hashed)
            )
        )

    def revoke_token(self, token, request):
        revoke_family(token.family)


def server():
    return current_app.extensions["authorization_server"]


def init_server(app):
    app.config.update(
        OAUTH2_REFRESH_TOKEN_GENERATOR=True,
        OAUTH2_SCOPES_SUPPORTED=list(SCOPES),
        OAUTH2_TOKEN_EXPIRES_IN={"authorization_code": 600, "refresh_token": 600},
    )
    authorization = AuthorizationServer(app, query_client, save_token)
    authorization.register_grant(CodeGrant, [PKCE(), OpenID(require_nonce=True)])
    authorization.register_grant(RefreshGrant)
    authorization.register_endpoint(Revoke)
    app.extensions["authorization_server"] = authorization

    @app.after_request
    def cors(response):
        if request.path in ("/.well-known/openid-configuration", "/oauth/jwks"):
            response.headers["Access-Control-Allow-Origin"] = "*"
            return response
        if request.path.startswith("/api/sync/") or request.path in (
            "/oauth/token",
            "/oauth/userinfo",
            "/oauth/revoke",
        ):
            origin = request.headers.get("Origin")
            if origin:
                clients = db.session.scalars(db.select(Client).where(Client.enabled.is_(True)))
                origins = {
                    f"{p.scheme}://{p.netloc}"
                    for client in clients
                    for uri in client.redirect_uris
                    for p in [urlsplit(uri)]
                }
                if origin in origins:
                    response.headers["Access-Control-Allow-Origin"] = origin
                    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, OPTIONS"
                    response.headers["Access-Control-Allow-Headers"] = (
                        "Authorization, Content-Type, If-Match, If-None-Match"
                    )
                    response.headers["Access-Control-Expose-Headers"] = "ETag"
                    response.vary.add("Origin")
        return response


@bp.get("/.well-known/openid-configuration")
def discovery():
    base = current_app.config["BASE_URL"]
    return dict(
        issuer=base,
        authorization_endpoint=base + "/oauth/authorize",
        token_endpoint=base + "/oauth/token",
        userinfo_endpoint=base + "/oauth/userinfo",
        jwks_uri=base + "/oauth/jwks",
        revocation_endpoint=base + "/oauth/revoke",
        response_types_supported=["code"],
        grant_types_supported=["authorization_code", "refresh_token"],
        subject_types_supported=["public"],
        id_token_signing_alg_values_supported=["RS256"],
        token_endpoint_auth_methods_supported=CodeGrant.TOKEN_ENDPOINT_AUTH_METHODS,
        scopes_supported=list(SCOPES),
        code_challenge_methods_supported=["S256"],
        claims_supported=[
            "sub",
            "iss",
            "aud",
            "exp",
            "iat",
            "auth_time",
            "nonce",
            "name",
            "preferred_username",
            "email",
            "email_verified",
            "locale",
        ],
    )


@bp.get("/oauth/jwks")
def jwks():
    return {"keys": [signing_key().as_dict(private=False)]}


@bp.route("/oauth/authorize", methods=["GET", "POST"])
def authorize():
    rate_limit("authorize", 60, 300)
    try:
        grant = server().get_consent_grant(end_user=g.user)
    except OAuth2Error as error:
        # These OIDC errors are raised only after the registered redirect was validated.
        if isinstance(error, (LoginRequiredError, ConsentRequiredError)):
            return server().handle_error_response(server().create_oauth2_request(None), error)
        # Other invalid authorization requests are rendered locally.
        return render_template("oauth_error.html", error=error), 400
    if not g.user:
        return redirect(url_for("auth.login", next=safe_next(request.full_path)))
    if current_app.config["REQUIRE_VERIFIED_EMAIL"] and not g.user.email_verified:
        abort(403, "Bitte bestätige zuerst deine E-Mail-Adresse.")
    scope = grant.request.scope
    consent = db.session.scalar(
        db.select(Consent).where(
            Consent.user_id == g.user.id, Consent.client_id == grant.request.client.client_id
        )
    )
    prompts = set(request.values.get("prompt", "").split())
    max_age = request.values.get("max_age")
    if max_age is not None and (not max_age.isdigit() or len(max_age) > 9):
        abort(400, "Ungültiges max_age.")
    auth_key = digest(request.query_string.decode())
    confirmed = session.get("oidc_confirmed", {})
    recently_confirmed = confirmed.get(
        "key"
    ) == auth_key and g.login_session.authenticated_at >= confirmed.get("at", now() + 1)
    must_confirm = (
        max_age is not None
        and now() - g.login_session.authenticated_at > int(max_age)
        and not recently_confirmed
    ) or (
        "login" in prompts
        and (
            confirmed.get("key") != auth_key
            or g.login_session.authenticated_at < confirmed.get("at", 0)
        )
    )
    if must_confirm:
        if "none" in prompts:
            return prompt_error(grant, LoginRequiredError)
        session["oidc_confirmed"] = {"key": auth_key, "at": now()}
        return redirect(url_for("auth.reauthenticate", next=safe_next(request.full_path)))
    has_consent = consent and set(scope.split()) <= set(consent.scopes.split())
    if "none" in prompts and not has_consent:
        return prompt_error(grant, ConsentRequiredError)
    if request.method == "POST":
        if request.form.get("decision") != "allow":
            return server().create_authorization_response(grant_user=None, grant=grant)
        if not consent:
            consent = Consent(
                user_id=g.user.id, client_id=grant.request.client.client_id, scopes=scope
            )
            db.session.add(consent)
        else:
            consent.scopes = " ".join(sorted(set(consent.scopes.split()) | set(scope.split())))
        db.session.commit()
        session.pop("oidc_confirmed", None)
        return server().create_authorization_response(grant_user=g.user, grant=grant)
    if has_consent and "consent" not in prompts:
        session.pop("oidc_confirmed", None)
        return server().create_authorization_response(grant_user=g.user, grant=grant)
    return render_template(
        "consent.html",
        client=grant.request.client,
        scopes=[SCOPES.get(s, s) for s in scope.split()],
    )


def prompt_error(grant, error_type):
    error = error_type(
        redirect_uri=grant.request.payload.redirect_uri
        or grant.request.client.get_default_redirect_uri(),
        state=grant.request.payload.state,
    )
    return server().handle_error_response(grant.request, error)


@bp.route("/oauth/token", methods=["POST", "OPTIONS"])
@csrf.exempt
def token():
    if request.method == "OPTIONS":
        return "", 204
    rate_limit("token", 60, 300)
    return server().create_token_response()


def bearer_user(required_scope):
    scheme, _, raw = request.headers.get("Authorization", "").partition(" ")
    token = (
        db.session.scalar(db.select(OAuthToken).where(OAuthToken.access_hash == digest(raw)))
        if raw and scheme.lower() == "bearer"
        else None
    )
    if not token or token.is_expired() or token.is_revoked():
        abort(401, "Das App-Token ist ungültig oder abgelaufen.")
    if required_scope not in token.scope.split():
        abort(403, "Dem App-Token fehlt die erforderliche Freigabe.")
    return token.user, token


@bp.route("/oauth/userinfo", methods=["GET", "POST", "OPTIONS"])
@csrf.exempt
def userinfo():
    if request.method == "OPTIONS":
        return "", 204
    user, token = bearer_user("openid")
    return dict(user_info(user, token.scope))


@bp.route("/oauth/revoke", methods=["POST", "OPTIONS"])
@csrf.exempt
def revoke():
    if request.method == "OPTIONS":
        return "", 204
    rate_limit("revoke", 60, 300)
    return server().create_endpoint_response("revocation")
