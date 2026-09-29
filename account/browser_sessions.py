"""Remember public app logins without persisting bearer tokens in JavaScript storage."""

import secrets
from urllib.parse import urlsplit

from flask import Blueprint, abort, jsonify, request
from werkzeug.exceptions import HTTPException

from .extensions import csrf, db
from .models import AppBrowserSession, Client, OAuthToken, digest, now
from .oidc import bearer_user, user_info
from .security import rate_limit

bp = Blueprint("browser_sessions", __name__)


def allowed_client(slug):
    client = db.session.scalar(
        db.select(Client).where(Client.slug == slug, Client.enabled.is_(True))
    )
    if (
        not client
        or (slug not in ("startpage", "weather", "searxng", "news") and not client.developer_app)
        or client.token_endpoint_auth_method != "none"
    ):
        abort(403)
    origins = {f"{p.scheme}://{p.netloc}" for uri in client.redirect_uris for p in [urlsplit(uri)]}
    if request.headers.get("Origin") not in origins:
        abort(403)
    return client


@bp.after_request
def cors(response):
    try:
        allowed_client(request.view_args["slug"])
    except HTTPException:
        return response
    response.headers["Access-Control-Allow-Origin"] = request.headers["Origin"]
    response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = (
        "Authorization, Content-Type, X-Julianverse-Session"
    )
    response.vary.add("Origin")
    return response


def revoke(record):
    db.session.execute(
        db.update(OAuthToken).where(OAuthToken.family == record.token.family).values(revoked=True)
    )
    db.session.delete(record)


@bp.route("/oauth/browser/<slug>", methods=["POST", "OPTIONS"])
@csrf.exempt
def browser_session(slug):
    client = allowed_client(slug)
    same_site = "None" if client.developer_app else "Lax"
    if request.method == "OPTIONS":
        return "", 204
    # Exact registered Origin plus a non-simple header prevent cross-site and form CSRF.
    if request.headers.get("X-Julianverse-Session") != "1" or not request.is_json:
        abort(403)
    payload = request.get_json()
    if not isinstance(payload, dict):
        abort(400)
    action = payload.get("action")
    if action not in ("remember", "token", "logout"):
        abort(400)
    cookie = f"__Secure-jv-app-{slug}"
    raw = request.cookies.get(cookie, "")
    record = db.session.get(AppBrowserSession, digest(raw)) if raw else None
    if record and (
        record.origin != request.headers["Origin"] or record.token.client_id != client.client_id
    ):
        abort(403)
    if action == "logout":
        if record:
            revoke(record)
            db.session.commit()
        response = jsonify(ok=True)
        response.delete_cookie(
            cookie, path=request.path, secure=True, httponly=True, samesite=same_site
        )
        return response
    if action == "remember":
        _, token = bearer_user("openid")
        if token.client_id != client.client_id or not token.refresh_hash or token.refresh_used:
            abort(403)
        rate_limit("app-session", 120, 300, identity=token.user_id)
        consumed = db.session.execute(
            db.update(OAuthToken)
            .where(
                OAuthToken.id == token.id,
                OAuthToken.revoked.is_(False),
                OAuthToken.refresh_hash.is_not(None),
                OAuthToken.refresh_used.is_(False),
            )
            .values(refresh_hash=None)
        )
        if consumed.rowcount != 1:
            abort(401)
        if record:
            revoke(record)
        raw = secrets.token_urlsafe(48)
        record = AppBrowserSession(
            id=digest(raw),
            token=token,
            origin=request.headers["Origin"],
            expires_at=min(token.refresh_expires_at, token.browser_session.expires_at),
        )
        # This login now uses the HttpOnly cookie; the PKCE refresh credential is retired.
        db.session.add(record)
    elif not record or record.expires_at <= now() or record.token.is_revoked():
        abort(401, "Bitte erneut anmelden. Deine lokalen Daten bleiben erhalten.")
    else:
        rate_limit("app-session", 120, 300, identity=record.token.user_id)
    seed = record.token
    # Serialize issuance against app logout/family revocation in another worker.
    checked = db.session.execute(
        db.update(OAuthToken)
        .where(
            OAuthToken.id == seed.id,
            OAuthToken.revoked.is_(False),
        )
        .values(revoked=False)
    )
    if checked.rowcount != 1:
        abort(401)
    # Remove expired access-only tokens, preserving the seed of every remembered session.
    db.session.execute(
        db.delete(OAuthToken).where(
            OAuthToken.user_id == seed.user_id,
            OAuthToken.refresh_hash.is_(None),
            OAuthToken.issued_at + OAuthToken.expires_in < now(),
            OAuthToken.id.not_in(db.select(AppBrowserSession.token_id)),
        )
    )
    access = secrets.token_urlsafe(48)
    lifetime = min(600, record.expires_at - now())
    if lifetime <= 0:
        abort(401)
    # Do not rotate/revoke earlier access tokens: open tabs share this cookie.
    db.session.add(
        OAuthToken(
            client_id=seed.client_id,
            user_id=seed.user_id,
            session_id=seed.session_id,
            code_id=seed.code_id,
            family=seed.family,
            access_hash=digest(access),
            scope=seed.scope,
            expires_in=lifetime,
            refresh_expires_at=record.expires_at,
        )
    )
    response = jsonify(
        access_token=access, expires_in=lifetime, user=dict(user_info(seed.user, seed.scope))
    )
    db.session.commit()
    if action == "remember":
        response.set_cookie(
            cookie,
            raw,
            max_age=record.expires_at - now(),
            path=request.path,
            secure=True,
            httponly=True,
            samesite=same_site,
        )
    return response
