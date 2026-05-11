"""agent_token_downloads for VF-303 server-streamed one-time token download.

Revision ID: x4y5z6a7b8c9
Revises: w3x4y5z6a7b8
Create Date: 2026-04-21

Adds a short-lived table that stores the plaintext agent token for at most
5 minutes with a single-use nonce. Used by the token-file GET endpoint so
the download comes from a known same-origin URL (sidesteps SmartScreen
reputation scan on client-side blob: URLs). Rows are consumed on download
or expire after 5 min; a periodic sweep can prune consumed/expired rows.

Intentional plaintext storage for a bounded window — documented in
threat-model.md (to be updated alongside).
"""
from alembic import op
import sqlalchemy as sa

revision = "x4y5z6a7b8c9"
down_revision = "w3x4y5z6a7b8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_token_downloads",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agents.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("nonce", sa.String(48), nullable=False, unique=True, index=True),
        sa.Column("token_plaintext", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_table("agent_token_downloads")
