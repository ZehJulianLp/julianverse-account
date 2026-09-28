import functools
import json
import secrets
from urllib.parse import urlsplit

import pyotp
from cryptography.fernet import Fernet
from flask import abort, current_app, g, redirect, request, session, url_for
from sqlalchemy import update
from sqlalchemy.dialects.sqlite import insert

from .extensions import db
from .models import BrowserSession, Challenge, RateBucket, RecoveryCode, User, digest, now


def encrypt(value):
    return Fernet(current_app.config["ENCRYPTION_KEY"].encode()).encrypt(value.encode()).decode()


def decrypt(value):
    return Fernet(current_app.config["ENCRYPTION_KEY"].encode()).decrypt(value.encode()).decode()


def safe_next(value):
    if not value or any(c in value for c in ("\\", "\r", "\n")):
        return url_for("pages.dashboard")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or not value.startswith("/") or value.startswith("//"):
        return url_for("pages.dashboard")
    return value


def load_user():
    g.user = None
    g.login_session = None
    raw = session.get("sid")
    if raw:
        item = db.session.get(BrowserSession, digest(raw))
        if item and not item.revoked and item.expires_at > now() and item.user.enabled:
            g.user = item.user
            g.login_session = item
            if item.last_seen + 300 < now():
                item.last_seen = now()
                db.session.commit()
        else:
            session.clear()


def sign_in(user, method="password"):
    old = g.get("login_session")
    if old:
        old.revoked = True
    session.clear()
    raw = secrets.token_urlsafe(32)
    item = BrowserSession(
        id=digest(raw),
        user_id=user.id,
        expires_at=now() + 30 * 86400,
        user_agent=request.user_agent.string[:240],
        method=method,
    )
    db.session.add(item)
    db.session.commit()
    session["sid"] = raw
    session.permanent = True
    g.user, g.login_session = user, item


def sign_out():
    if g.get("login_session"):
        g.login_session.revoked = True
        db.session.commit()
    session.clear()


def revoke_sessions(user_id, keep_id=None):
    query = update(BrowserSession).where(BrowserSession.user_id == user_id)
    if keep_id:
        query = query.where(BrowserSession.id != keep_id)
    db.session.execute(query.values(revoked=True))


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not g.user:
            if request.path.startswith("/api/"):
                abort(401, "Bitte melde dich an.")
            return redirect(url_for("auth.login", next=safe_next(request.full_path)))
        return view(*args, **kwargs)

    return wrapped


def fresh_required(view):
    @login_required
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.login_session.authenticated_at + 600 < now():
            if request.path.startswith("/api/"):
                abort(403, "Bitte bestätige deine Anmeldung unter Sicherheit erneut.")
            target = request.full_path if request.method == "GET" else url_for("pages.security")
            return redirect(url_for("auth.reauthenticate", next=safe_next(target)))
        return view(*args, **kwargs)

    return wrapped


def rate_limit(bucket, limit=10, seconds=300, identity=None):
    """Atomic, shared between Gunicorn workers; identifiers are never stored in clear text."""
    if current_app.config.get("RATE_LIMIT_DISABLED"):
        return
    current = now()
    start = current - current % seconds
    identity = identity or request.remote_addr or "unknown"
    key = digest(f"{bucket}:{identity}:{start}")
    statement = insert(RateBucket).values(key=key, starts_at=start, count=1)
    statement = statement.on_conflict_do_update(
        index_elements=["key"], set_={"count": RateBucket.count + 1}
    ).returning(RateBucket.count)
    count = db.session.execute(statement).scalar_one()
    db.session.commit()
    if count > limit:
        abort(429, "Zu viele Versuche. Bitte warte einige Minuten.")


def browser_binding():
    if "browser_nonce" not in session:
        session["browser_nonce"] = secrets.token_urlsafe(32)
    return digest(session["browser_nonce"])


def make_challenge(kind, payload, user_id=None):
    item = Challenge(
        kind=kind,
        browser_hash=browser_binding(),
        user_id=user_id,
        payload=encrypt(json.dumps(payload)),
        expires_at=now() + 300,
    )
    db.session.add(item)
    db.session.commit()
    return item.id


def take_challenge(identifier, kind):
    item = db.session.get(Challenge, identifier)
    if (
        not item
        or item.kind != kind
        or item.used
        or item.expires_at <= now()
        or item.browser_hash != browser_binding()
    ):
        abort(400, "Die Anfrage ist abgelaufen. Bitte starte sie erneut.")
    result = db.session.execute(
        update(Challenge)
        .where(Challenge.id == item.id, Challenge.used.is_(False))
        .values(used=True)
    )
    if result.rowcount != 1:
        db.session.rollback()
        abort(400, "Diese Anfrage wurde bereits verwendet.")
    payload, user_id = json.loads(decrypt(item.payload)), item.user_id
    db.session.commit()
    return payload, db.session.get(User, user_id) if user_id else None


def verify_second_factor(user, code):
    code = (code or "").replace(" ", "").strip()
    if user.totp_secret and code.isdigit() and len(code) == 6:
        totp = pyotp.TOTP(decrypt(user.totp_secret))
        counter = now() // 30
        for candidate in (counter - 1, counter, counter + 1):
            if candidate > user.totp_last_counter and secrets.compare_digest(
                totp.at(candidate * 30), code
            ):
                result = db.session.execute(
                    update(User)
                    .where(User.id == user.id, User.totp_last_counter < candidate)
                    .values(totp_last_counter=candidate)
                )
                db.session.commit()
                return result.rowcount == 1
    item = db.session.scalar(
        db.select(RecoveryCode).where(
            RecoveryCode.user_id == user.id,
            RecoveryCode.code_hash == digest(code.lower()),
            RecoveryCode.used_at.is_(None),
        )
    )
    if item:
        result = db.session.execute(
            update(RecoveryCode)
            .where(RecoveryCode.id == item.id, RecoveryCode.used_at.is_(None))
            .values(used_at=now())
        )
        db.session.commit()
        return result.rowcount == 1
    return False
