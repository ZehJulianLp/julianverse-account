import secrets
import smtplib
import ssl
from email.message import EmailMessage

from flask import current_app

from .extensions import db
from .models import MailAction, digest, now


def available():
    return bool(current_app.config["MAIL_HOST"] or current_app.testing)


def send_action(user, kind, email=None):
    if not available():
        raise RuntimeError("Der E-Mail-Versand ist noch nicht eingerichtet.")
    email = email or user.email
    token = secrets.token_urlsafe(32)
    action = MailAction(
        id=digest(token),
        user_id=user.id,
        kind=kind,
        email=email,
        expires_at=now() + (1800 if kind == "reset" else 86400),
    )
    url = current_app.config["BASE_URL"] + "/auth/mail/" + token
    subject = "Passwort zurücksetzen" if kind == "reset" else "E-Mail-Adresse bestätigen"
    message = EmailMessage()
    message["From"] = current_app.config["MAIL_FROM"]
    message["To"] = email
    message["Subject"] = "Julianverse Account · " + subject
    message.set_content(
        f"Hallo {user.display_name},\n\n{subject}:\n{url}\n\n"
        "Falls du diese Anfrage nicht gestellt hast, kannst du diese Nachricht ignorieren.\n"
    )
    if current_app.testing:
        current_app.extensions.setdefault("mail_outbox", []).append(
            {"to": email, "kind": kind, "url": url, "token": token}
        )
    else:
        cfg = current_app.config
        smtp_type = smtplib.SMTP_SSL if cfg["MAIL_USE_SSL"] else smtplib.SMTP
        options = {"timeout": 15}
        if cfg["MAIL_USE_SSL"]:
            options["context"] = ssl.create_default_context()
        with smtp_type(cfg["MAIL_HOST"], cfg["MAIL_PORT"], **options) as smtp:
            if cfg["MAIL_USE_STARTTLS"]:
                smtp.starttls(context=ssl.create_default_context())
            if cfg["MAIL_USERNAME"]:
                smtp.login(cfg["MAIL_USERNAME"], cfg["MAIL_PASSWORD"])
            smtp.send_message(message)
    db.session.add(action)
    db.session.commit()
