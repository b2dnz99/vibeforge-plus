"""sessions.elevated_until for VF-264 Phase 2 SU elevation tier.

Revision ID: w3x4y5z6a7b8
Revises: v2w3x4y5z6a7
Create Date: 2026-04-20

Adds a nullable `elevated_until` timestamp column to `sessions`. When set and
in the future, the session is in SU admin-context (elevated) state. See
0-MD/proposed/SU-ELEVATION-TIER-PROPOSAL.md §3.1 and §3.3 for the helper
logic that consumes it (_require_sa Path B).

Nullable + no backfill — all existing sessions default to NULL meaning
"not elevated." No data migration needed.
"""
from alembic import op
import sqlalchemy as sa

revision = "w3x4y5z6a7b8"
down_revision = "v2w3x4y5z6a7"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "sessions",
        sa.Column("elevated_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_column("sessions", "elevated_until")
