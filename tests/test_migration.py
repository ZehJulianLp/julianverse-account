from pathlib import Path

from flask_migrate import downgrade, upgrade
from sqlalchemy import inspect
from test_oidc import issue

from account.extensions import db
from account.models import CloudConnection, DiscordIdentity, User
from account.security import encrypt


def test_populated_database_migration_preserves_identity_and_cloud(app, logged_in):
    issue(logged_in)  # Also populate sessions, grants, consent and OAuth tokens.
    migrations = str(Path(__file__).resolve().parents[1] / "migrations")
    with app.app_context():
        user = db.session.scalar(db.select(User))
        db.session.add(
            CloudConnection(
                user_id=user.id, username="existing-cloud", secret=encrypt("private-password")
            )
        )
        db.session.add(
            DiscordIdentity(id="1234567890000", user_id=user.id, username="existing-discord")
        )
        db.session.commit()
        tables = [
            name
            for name in inspect(db.engine).get_table_names()
            if name not in ("alembic_version", "admin_event")
        ]
        before = {
            name: db.session.execute(db.text('SELECT COUNT(*) FROM "' + name + '"')).scalar()
            for name in tables
        }
        db.session.remove()
        downgrade(directory=migrations, revision="9f8c816cc317")
        assert inspect(db.engine).has_table("cloud_connection")
        upgrade(directory=migrations)
        after = {
            name: db.session.execute(db.text('SELECT COUNT(*) FROM "' + name + '"')).scalar()
            for name in tables
        }
        assert after == before
        cloud = db.session.scalar(db.select(CloudConnection))
        assert cloud.username == "existing-cloud" and cloud.username_key == "existing-cloud"
        assert not cloud.managed
    # The same browser cookie must still work after migrating existing data.
    assert logged_in.get("/connections").status_code == 200
