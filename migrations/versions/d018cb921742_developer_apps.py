"""User-owned apps, resource definitions and anonymous operational counters."""

import sqlalchemy as sa
from alembic import op

revision = "d018cb921742"
down_revision = "c49bde302b61"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "developer_app",
        sa.Column(
            "client_id",
            sa.String(48),
            sa.ForeignKey("client.client_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "owner_id", sa.String(36), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("website", sa.String(2048), nullable=False),
        sa.Column("icon", sa.String(24), nullable=False),
        sa.Column("visibility", sa.String(16), nullable=False),
        sa.Column("test_mode", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("deleted_at", sa.Integer(), nullable=True),
        sa.Column("read_count", sa.Integer(), nullable=False),
        sa.Column("write_count", sa.Integer(), nullable=False),
        sa.Column("last_used", sa.Integer(), nullable=True),
    )
    op.create_index("ix_developer_app_owner_id", "developer_app", ["owner_id"])
    op.create_table(
        "app_resource",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "client_id",
            sa.String(48),
            sa.ForeignKey("developer_app.client_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key", sa.String(60), nullable=False),
        sa.Column("label", sa.String(80), nullable=False),
        sa.Column("description", sa.String(240), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("client_id", "key", name="uq_app_resource_client_key"),
    )
    op.create_table(
        "app_error",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "client_id",
            sa.String(48),
            sa.ForeignKey("developer_app.client_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("operation", sa.String(24), nullable=False),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
    )
    op.create_index("ix_app_error_client_id", "app_error", ["client_id"])


def downgrade():
    op.drop_table("app_error")
    op.drop_table("app_resource")
    op.drop_table("developer_app")
