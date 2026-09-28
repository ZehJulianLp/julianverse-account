import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from flask import Flask, g, jsonify, render_template, request
from flask_wtf.csrf import CSRFError
from sqlalchemy import event
from sqlalchemy.engine import Engine
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from .extensions import csrf, db, migrate


def boolean(name, default=False):
    return os.environ.get(name, str(default)).lower() in ("1", "true", "yes")


@event.listens_for(Engine, "connect")
def configure_sqlite(connection, _):
    if isinstance(connection, sqlite3.Connection):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


def create_app(config=None):
    app = Flask(__name__, instance_relative_config=True)
    root = Path(__file__).resolve().parent.parent
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("ACCOUNT_SECRET_KEY"),
        ENCRYPTION_KEY=os.environ.get("ACCOUNT_ENCRYPTION_KEY"),
        BASE_URL=os.environ.get("ACCOUNT_BASE_URL", "http://localhost:8096").rstrip("/"),
        ENVIRONMENT=os.environ.get("ACCOUNT_ENV", "development"),
        SQLALCHEMY_DATABASE_URI=os.environ.get("ACCOUNT_DATABASE_URL", "sqlite:///account.sqlite3"),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SIGNING_KEY_PATH=str(
            root / os.environ.get("ACCOUNT_SIGNING_KEY", "instance/oidc-private.pem")
        ),
        SESSION_COOKIE_NAME="julianverse_account",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_REFRESH_EACH_REQUEST=False,
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        MAX_CONTENT_LENGTH=1024 * 1024,
        WTF_CSRF_TIME_LIMIT=7200,
        REQUIRE_VERIFIED_EMAIL=boolean("ACCOUNT_REQUIRE_VERIFIED_EMAIL", True),
        REGISTRATION_OPEN=boolean("ACCOUNT_REGISTRATION_OPEN", True),
        OWNCLOUD_BASE_URL=os.environ.get(
            "OWNCLOUD_BASE_URL", "https://cloud.julianverse.de"
        ).rstrip("/"),
        OWNCLOUD_SYNC_ROOT=os.environ.get("OWNCLOUD_SYNC_ROOT", "Julianverse"),
        DISCORD_CLIENT_ID=os.environ.get("DISCORD_CLIENT_ID", ""),
        DISCORD_CLIENT_SECRET=os.environ.get("DISCORD_CLIENT_SECRET", ""),
        MAIL_HOST=os.environ.get("MAIL_HOST", ""),
        MAIL_PORT=int(os.environ.get("MAIL_PORT", "465")),
        MAIL_USE_SSL=boolean("MAIL_USE_SSL", True),
        MAIL_USE_STARTTLS=boolean("MAIL_USE_STARTTLS"),
        MAIL_USERNAME=os.environ.get("MAIL_USERNAME", ""),
        MAIL_PASSWORD=os.environ.get("MAIL_PASSWORD", ""),
        MAIL_FROM=os.environ.get("MAIL_FROM", "Julianverse Account <account@julianverse.de>"),
    )
    if config:
        app.config.update(config)
    Path(app.instance_path).mkdir(mode=0o700, parents=True, exist_ok=True)
    app.config["SESSION_COOKIE_SECURE"] = app.config["BASE_URL"].startswith("https://")
    if app.config["ENVIRONMENT"] == "production":
        if not app.config["BASE_URL"].startswith("https://"):
            raise RuntimeError("ACCOUNT_BASE_URL must use HTTPS in production.")
        if not app.config["SECRET_KEY"] or len(app.config["SECRET_KEY"]) < 32:
            raise RuntimeError("Set ACCOUNT_SECRET_KEY to a stable random secret.")
        if not app.config["ENCRYPTION_KEY"]:
            raise RuntimeError("Set ACCOUNT_ENCRYPTION_KEY; it protects ownCloud credentials.")
        Fernet(app.config["ENCRYPTION_KEY"].encode())
        if not Path(app.config["SIGNING_KEY_PATH"]).is_file():
            raise RuntimeError("Create the persistent OIDC signing key with init-secrets first.")
        if not app.config["OWNCLOUD_BASE_URL"].startswith("https://"):
            raise RuntimeError("ownCloud must use HTTPS in production.")
        if app.config["MAIL_HOST"] and not (
            app.config["MAIL_USE_SSL"] or app.config["MAIL_USE_STARTTLS"]
        ):
            raise RuntimeError("Configure TLS for outgoing email.")
    if boolean("ACCOUNT_TRUST_PROXY"):
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=0)
    app.config["TRUSTED_HOSTS"] = [
        urlsplit(app.config["BASE_URL"]).hostname,
        "localhost",
        "127.0.0.1",
    ]
    db.init_app(app)
    migrate.init_app(app, db, render_as_batch=True)
    csrf.init_app(app)

    from . import auth, cli, cloud, oidc, pages, passkeys
    from .security import load_user

    app.before_request(load_user)
    for blueprint in (auth.bp, pages.bp, cloud.bp, oidc.bp, passkeys.bp):
        app.register_blueprint(blueprint)
    auth.init_discord(app)
    oidc.init_server(app)
    cli.init_app(app)

    @app.context_processor
    def context():
        return {
            "current_user": g.get("user"),
            "base_url": app.config["BASE_URL"],
            "discord_enabled": bool(app.config["DISCORD_CLIENT_ID"]),
            "cloud_url": app.config["OWNCLOUD_BASE_URL"],
            "year": datetime.now(timezone.utc).year,
        }

    @app.template_filter("date")
    def date_filter(value):
        return (
            datetime.fromtimestamp(value, timezone.utc).strftime("%d.%m.%Y, %H:%M UTC")
            if value
            else "Noch nicht"
        )

    @app.errorhandler(HTTPException)
    def http_error(error):
        if request.path.startswith(("/api/", "/oauth/")) and request.endpoint != "oidc.authorize":
            return jsonify(error=error.name, message=error.description), error.code
        return render_template("error.html", error=error), error.code

    @app.errorhandler(CSRFError)
    def csrf_error(error):
        error.description = "Die Formularsitzung ist abgelaufen. Bitte lade die Seite neu."
        return http_error(error)

    @app.after_request
    def headers(response):
        from .models import Client

        # Browsers also apply form-action to redirects after a form submission.
        # Consent and Discord linking must be able to return to registered apps.
        form_origins = {"'self'", "https://discord.com"}
        for client in db.session.scalars(db.select(Client).where(Client.enabled.is_(True))):
            for uri in client.redirect_uris:
                parsed = urlsplit(uri)
                form_origins.add(f"{parsed.scheme}://{parsed.netloc}")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action " + " ".join(sorted(form_origins))
        )
        if app.config["SESSION_COOKIE_SECURE"]:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        if not request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/healthz")
    def health():
        db.session.execute(db.text('SELECT id FROM "user" LIMIT 1'))
        return {"status": "ok"}

    return app
