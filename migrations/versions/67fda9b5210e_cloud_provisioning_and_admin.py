"""ownCloud provisioning, unique cloud ownership, and administrators"""

from alembic import op
import sqlalchemy as sa

revision = "67fda9b5210e"
down_revision = "9f8c816cc317"
branch_labels = None
depends_on = None


def upgrade():
    # SQLite supports ADD COLUMN here. Rebuilding the parent table would fire
    # ON DELETE CASCADE on sessions, cloud bindings and other identity records.
    op.add_column(
        "user", sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    with op.batch_alter_table("cloud_connection") as batch:
        batch.add_column(sa.Column("username_key", sa.String(254), nullable=True))
        batch.add_column(
            sa.Column("managed", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(sa.Column("state", sa.String(16), nullable=False, server_default="ready"))
        batch.add_column(sa.Column("remote_enabled", sa.Boolean(), nullable=True))
        batch.add_column(sa.Column("last_error", sa.String(32), nullable=True))
        batch.add_column(sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(
            sa.Column("next_attempt", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("lease_until", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("lease_token", sa.String(64), nullable=True))
    connection = op.get_bind()
    seen = set()
    for row in connection.execute(sa.text("SELECT user_id, username FROM cloud_connection")):
        key = row.username.casefold()
        if key in seen:
            raise RuntimeError(
                "Mehrere Konten verwenden denselben ownCloud-Zugang. Vor der Migration auflösen."
            )
        seen.add(key)
        connection.execute(
            sa.text("UPDATE cloud_connection SET username_key=:key WHERE user_id=:id"),
            {"key": key, "id": row.user_id},
        )
    with op.batch_alter_table("cloud_connection") as batch:
        batch.alter_column("username_key", existing_type=sa.String(254), nullable=False)
        batch.create_unique_constraint("uq_cloud_connection_username_key", ["username_key"])
    op.create_table(
        "admin_event",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("user.id", ondelete="SET NULL")),
        sa.Column("target_id", sa.String(36), sa.ForeignKey("user.id", ondelete="SET NULL")),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
    )


def downgrade():
    op.drop_table("admin_event")
    with op.batch_alter_table("cloud_connection") as batch:
        batch.drop_constraint("uq_cloud_connection_username_key", type_="unique")
        for name in (
            "username_key",
            "managed",
            "state",
            "remote_enabled",
            "last_error",
            "attempts",
            "next_attempt",
            "lease_until",
            "lease_token",
        ):
            batch.drop_column(name)
    # Native DROP COLUMN (SQLite >= 3.35) also keeps the parent table intact.
    op.drop_column("user", "is_admin")
