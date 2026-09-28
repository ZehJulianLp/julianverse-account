import json
from urllib.parse import urlsplit

from flask import Blueprint, abort, current_app, flash, g, redirect, request, url_for
from sqlalchemy.exc import IntegrityError
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from .extensions import db
from .models import DiscordIdentity, Passkey, User, now
from .security import fresh_required, make_challenge, rate_limit, safe_next, sign_in, take_challenge

bp = Blueprint("passkeys", __name__, url_prefix="/api/passkeys")


def rp():
    return urlsplit(current_app.config["BASE_URL"]).hostname


@bp.post("/register/options")
@fresh_required
def registration_options():
    rate_limit("passkey-register", 10, 300, identity=g.user.id)
    keys = db.session.scalars(db.select(Passkey).where(Passkey.user_id == g.user.id)).all()
    if len(keys) >= 10:
        abort(400, "Pro Konto sind höchstens zehn Passkeys vorgesehen.")
    options = generate_registration_options(
        rp_id=rp(),
        rp_name="Julianverse Account",
        user_id=g.user.id.encode(),
        user_name=g.user.username,
        user_display_name=g.user.display_name,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(k.credential_id)) for k in keys
        ],
    )
    data = json.loads(options_to_json(options))
    challenge = make_challenge("passkey-register", {"challenge": data["challenge"]}, g.user.id)
    return {"options": data, "challenge": challenge}


@bp.post("/register/verify")
@fresh_required
def registration_verify():
    data = request.get_json()
    payload, user = take_challenge(data.get("challenge", ""), "passkey-register")
    if not user or user.id != g.user.id:
        abort(400)
    try:
        verified = verify_registration_response(
            credential=data.get("credential", {}),
            expected_challenge=base64url_to_bytes(payload["challenge"]),
            expected_rp_id=rp(),
            expected_origin=current_app.config["BASE_URL"],
            require_user_verification=True,
        )
    except (WebAuthnException, ValueError, TypeError):
        abort(400, "Der Passkey konnte nicht geprüft werden.")
    name = str(data.get("name", "Mein Passkey")).strip()[:80] or "Mein Passkey"
    db.session.add(
        Passkey(
            user_id=user.id,
            credential_id=bytes_to_base64url(verified.credential_id),
            public_key=verified.credential_public_key,
            sign_count=verified.sign_count,
            name=name,
        )
    )
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        abort(409, "Dieser Passkey wurde bereits hinzugefügt.")
    return {"redirect": url_for("pages.security")}


@bp.post("/login/options")
def authentication_options():
    rate_limit("passkey-options", 20, 300)
    options = generate_authentication_options(
        rp_id=rp(), user_verification=UserVerificationRequirement.REQUIRED
    )
    data = json.loads(options_to_json(options))
    challenge = make_challenge(
        "passkey-login",
        {
            "challenge": data["challenge"],
            "next": safe_next((request.get_json(silent=True) or {}).get("next")),
        },
        g.user.id if g.user else None,
    )
    return {"options": data, "challenge": challenge}


@bp.post("/login/verify")
def authentication_verify():
    rate_limit("passkey-login", 10, 300)
    data = request.get_json()
    payload, expected_user = take_challenge(data.get("challenge", ""), "passkey-login")
    credential = data.get("credential", {})
    key = db.session.scalar(
        db.select(Passkey).where(Passkey.credential_id == credential.get("id", ""))
    )
    user = db.session.get(User, key.user_id) if key else None
    if not user or not user.enabled or (expected_user and expected_user.id != user.id):
        abort(400, "Die Anmeldung mit diesem Passkey ist fehlgeschlagen.")
    if current_app.config["REQUIRE_VERIFIED_EMAIL"] and not user.email_verified:
        abort(403, "Bitte bestätige zuerst deine E-Mail-Adresse.")
    try:
        user_handle = credential.get("response", {}).get("userHandle")
        if user_handle and base64url_to_bytes(user_handle) != user.id.encode():
            abort(400)
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(payload["challenge"]),
            expected_rp_id=rp(),
            expected_origin=current_app.config["BASE_URL"],
            credential_public_key=key.public_key,
            credential_current_sign_count=key.sign_count,
            require_user_verification=True,
        )
    except (WebAuthnException, ValueError, TypeError):
        abort(400, "Der Passkey konnte nicht geprüft werden.")
    changed = db.session.execute(
        db.update(Passkey)
        .where(Passkey.id == key.id, Passkey.sign_count == key.sign_count)
        .values(sign_count=verified.new_sign_count, last_used=now())
    )
    if changed.rowcount != 1:
        db.session.rollback()
        abort(409, "Bitte wiederhole die Anmeldung.")
    db.session.commit()
    if expected_user and g.user and g.user.id == user.id:
        g.login_session.authenticated_at = now()
        db.session.commit()
    else:
        sign_in(user, "passkey")
    return {"redirect": safe_next(payload["next"])}


@bp.post("/<identifier>/remove")
@fresh_required
def remove(identifier):
    key = db.session.get(Passkey, identifier)
    if not key or key.user_id != g.user.id:
        abort(404)
    count = db.session.scalar(
        db.select(db.func.count()).select_from(Passkey).where(Passkey.user_id == g.user.id)
    )
    discord = db.session.scalar(
        db.select(DiscordIdentity).where(DiscordIdentity.user_id == g.user.id)
    )
    if count <= 1 and not g.user.password_hash and not discord:
        abort(400, "Richte zuerst eine weitere Anmeldemethode ein.")
    db.session.delete(key)
    db.session.commit()
    flash("Passkey entfernt.", "success")
    return redirect(url_for("pages.security"))
