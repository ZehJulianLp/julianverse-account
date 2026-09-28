import os
import re
import secrets
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

import click
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from .extensions import db
from .models import (
    AppBrowserSession,
    Challenge,
    Client,
    MailAction,
    OAuthToken,
    RateBucket,
    digest,
    now,
)


def init_app(app):
    @app.cli.command("init-secrets")
    def init_secrets():
        """Create a private .env and persistent RSA key without displaying secrets."""
        root = Path(app.root_path).parent
        env = root / ".env"
        if not env.exists():
            content = (root / ".env.example").read_text()
            content = content.replace(
                "ACCOUNT_SECRET_KEY=\n", f"ACCOUNT_SECRET_KEY={secrets.token_urlsafe(48)}\n"
            )
            content = content.replace(
                "ACCOUNT_ENCRYPTION_KEY=\n",
                f"ACCOUNT_ENCRYPTION_KEY={Fernet.generate_key().decode()}\n",
            )
            descriptor = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(content)
            click.echo(".env angelegt (0600). Vorhandene Einstellungen werden nie ersetzt.")
        else:
            click.echo(".env existiert bereits und bleibt unverändert.")
        keypath = Path(app.config["SIGNING_KEY_PATH"])
        if not keypath.exists():
            keypath.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
            descriptor = os.open(keypath, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(
                    key.private_bytes(
                        serialization.Encoding.PEM,
                        serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption(),
                    )
                )
            click.echo("OIDC-Signaturschlüssel angelegt (0600). Bitte zusammen mit .env sichern.")

    @app.cli.command("create-client")
    @click.argument("slug")
    @click.option("--name", required=True)
    @click.option("--redirect-uri", "redirects", multiple=True, required=True)
    @click.option("--scope", default="openid profile email")
    @click.option(
        "--public",
        "public_client",
        is_flag=True,
        help="Browser-App ohne Client-Secret; PKCE bleibt erforderlich.",
    )
    def create_client(slug, name, redirects, scope, public_client):
        """Register an exact allowlist of callback URLs. Secrets are shown once."""
        from .oidc import SCOPES

        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", slug):
            raise click.ClickException("Ungültiger App-Slug.")
        if not set(scope.split()) <= set(SCOPES) or "openid" not in scope.split():
            raise click.ClickException(
                "Zulässige Scopes: openid profile email sync; openid ist erforderlich."
            )
        if "owncloud" in scope.split() and (slug != "owncloud" or public_client):
            raise click.ClickException(
                "Der ownCloud-Scope ist dem vertraulichen ownCloud-Client vorbehalten."
            )
        for uri in redirects:
            parsed = urlsplit(uri)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.fragment
                or "*" in uri
            ):
                raise click.ClickException(
                    "Callbacks brauchen eine vollständige HTTPS-URL ohne Wildcards, Zugangsdaten oder Fragment."
                )
        if db.session.scalar(db.select(Client.id).where(Client.slug == slug)):
            raise click.ClickException("Dieser App-Slug ist bereits registriert.")
        secret = secrets.token_urlsafe(48) if not public_client else ""
        client = Client(
            slug=slug,
            client_id=secrets.token_urlsafe(24),
            client_secret=digest(secret) if secret else "",
            client_id_issued_at=now(),
        )
        client.set_client_metadata(
            dict(
                client_name=name,
                redirect_uris=list(redirects),
                scope=scope,
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="none" if public_client else "client_secret_basic",
            )
        )
        db.session.add(client)
        db.session.commit()
        click.echo(f"Client-ID: {client.client_id}")
        if secret:
            click.echo(f"Client-Secret (nur jetzt sichtbar): {secret}")

    @app.cli.command("list-clients")
    def list_clients():
        for client in db.session.scalars(db.select(Client).order_by(Client.slug)):
            click.echo(
                f"{client.slug}: {client.client_id} ({'aktiv' if client.enabled else 'gesperrt'})"
            )

    @app.cli.command("create-user")
    @click.argument("username")
    @click.option("--email", required=True)
    @click.option(
        "--existing-cloud", is_flag=True, help="Vorhandenes Cloud-Konto später verknüpfen."
    )
    @click.option(
        "--verified",
        is_flag=True,
        help="Der Betreiber hat die Inhaberschaft der E-Mail bereits geprüft.",
    )
    @click.password_option(confirmation_prompt=True)
    def create_user(username, email, verified, password, existing_cloud):
        """Create an initial account interactively; never ship a default password."""
        from email_validator import EmailNotValidError, validate_email

        from .models import User

        if (
            not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,31}", username)
            or not 12 <= len(password) <= 128
        ):
            raise click.ClickException("Benutzername: 3–32 Zeichen; Passwort: 12–128 Zeichen.")
        try:
            email = validate_email(email, check_deliverability=False).normalized.lower()
        except EmailNotValidError as error:
            raise click.ClickException(str(error)) from error
        if db.session.scalar(
            db.select(User.id).where((User.username == username) | (User.email == email))
        ):
            raise click.ClickException("Benutzername oder E-Mail bereits vergeben.")
        user = User(username=username, display_name=username, email=email, email_verified=verified)
        user.set_password(password)
        db.session.add(user)
        if not existing_cloud:
            from .provisioning import queue_new_cloud

            queue_new_cloud(user)
        db.session.commit()
        click.echo("Konto erstellt. Sync bleibt ausgeschaltet.")

    @app.cli.command("make-admin")
    @click.argument("username")
    def make_admin(username):
        """Bootstrap an administrator from an existing verified account."""
        from .models import AdminEvent, User

        user = db.session.scalar(db.select(User).where(User.username == username))
        if not user or not user.enabled or not user.email_verified:
            raise click.ClickException("Aktives Konto mit bestätigter E-Mail erforderlich.")
        if not user.is_admin:
            user.is_admin = True
            db.session.add(AdminEvent(target_id=user.id, action="cli-make-admin"))
            db.session.commit()
        click.echo("Adminrechte gesetzt.")

    @app.cli.command("reconcile-cloud")
    def reconcile_cloud():
        """Process queued ownCloud accounts; suitable for a systemd timer."""
        from .provisioning import reconcile_due

        results = reconcile_due()
        click.echo(
            f"Cloud-Aufträge: {sum(ok for _, ok in results)} erfolgreich, "
            f"{sum(not ok for _, ok in results)} noch ausstehend."
        )

    @app.cli.command("prepare-owncloud")
    def prepare_owncloud():
        """Write the private, idempotent setup bundle; never print credentials."""
        import json

        from .security import decrypt, encrypt

        target = Path(app.instance_path) / "owncloud-setup.json"
        client = db.session.scalar(db.select(Client).where(Client.slug == "owncloud"))
        if target.exists():
            bundle = json.loads(target.read_text())
            if not client or not client.check_client_secret(decrypt(bundle["oidc_secret"])):
                raise click.ClickException(
                    "Setup-Datei und registrierter ownCloud-Client passen nicht zusammen."
                )
            click.echo("Vorhandene private ownCloud-Einrichtung wird weiterverwendet.")
            return
        if client:
            raise click.ClickException(
                "ownCloud-Client existiert bereits; keine Zugangsdaten überschrieben."
            )
        secret = secrets.token_urlsafe(48)
        client = Client(
            slug="owncloud",
            client_id=secrets.token_urlsafe(24),
            client_secret=digest(secret),
            client_id_issued_at=now(),
        )
        client.set_client_metadata(
            dict(
                client_name="ownCloud",
                redirect_uris=[
                    app.config["OWNCLOUD_BASE_URL"] + "/index.php/apps/openidconnect/redirect"
                ],
                scope="openid profile email owncloud",
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="client_secret_basic",
            )
        )
        db.session.add(client)
        db.session.flush()
        bundle = dict(
            client_id=client.client_id,
            oidc_secret=encrypt(secret),
            provision_user="jv_account_provisioner",
            provision_password=encrypt(secrets.token_urlsafe(48)),
        )
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(bundle, stream)
        db.session.commit()
        click.echo("ownCloud-Client und private Setup-Datei vorbereitet.")

    @app.cli.command("disable-client")
    @click.argument("slug")
    def disable_client(slug):
        client = db.session.scalar(db.select(Client).where(Client.slug == slug))
        if not client:
            raise click.ClickException("App nicht gefunden.")
        client.enabled = False
        db.session.commit()
        click.echo("App gesperrt; ihre Tokens sind sofort ungültig.")

    @app.cli.command("cleanup")
    def cleanup():
        for model, condition in (
            (AppBrowserSession, AppBrowserSession.expires_at < now()),
            (OAuthToken, OAuthToken.refresh_expires_at < now()),
            (Challenge, Challenge.expires_at < now()),
            (MailAction, MailAction.expires_at < now()),
            (RateBucket, RateBucket.starts_at < now() - 86400),
        ):
            db.session.execute(db.delete(model).where(condition))
        db.session.commit()
        click.echo("Abgelaufene Anfragen und Ratenzähler entfernt.")

    @app.cli.command("backup-db")
    @click.argument("destination", type=click.Path(path_type=Path))
    def backup_db(destination):
        """Consistent SQLite backup, including data still in WAL. Does not include keys."""
        path = db.engine.url.database
        if not path or path == ":memory:":
            raise click.ClickException("Keine SQLite-Datei konfiguriert.")
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        try:
            with (
                sqlite3.connect(f"file:{path}?mode=ro", uri=True) as source,
                sqlite3.connect(destination) as target,
            ):
                source.backup(target)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        click.echo(
            "Konsistente Datenbanksicherung erstellt. .env und OIDC-Schlüssel separat sichern."
        )
