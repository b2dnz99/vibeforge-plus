"""Add refresh_nonce to agents for drift refresh echo-back.

Revision ID: p6q7r8s9t0u1
Revises: o5p6q7r8s9t0
Create Date: 2026-04-12

VF-266 enhancement: random 4-byte nonce rotated on each /agentnotes
read. Agent must echo it via X-Refresh-Nonce header on next mutation.
Proves the agent actually parsed the contract response (not piped to
/dev/null or cached).
"""
from alembic import op
import sqlalchemy as sa

revision = "p6q7r8s9t0u1"
down_revision = "o5p6q7r8s9t0"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agents", sa.Column("refresh_nonce", sa.String(8), nullable=True))


def downgrade():
    op.drop_column("agents", "refresh_nonce")
