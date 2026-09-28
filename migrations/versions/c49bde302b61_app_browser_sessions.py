"""Persistent browser sessions for the optional static app integrations."""

import sqlalchemy as sa
from alembic import op

revision = "c49bde302b61"
down_revision = "67fda9b5210e"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "app_browser_session",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "token_id",
            sa.Integer(),
            sa.ForeignKey("o_auth_token.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("origin", sa.String(255), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
    )
    op.create_index("ix_app_browser_session_expires_at", "app_browser_session", ["expires_at"])


def downgrade():
    op.drop_table("app_browser_session")
