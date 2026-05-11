"""Drift gate v4 — per-agent response hash history for dedup.

Revision ID: s9t0u1v2w3x4
Revises: r8s9t0u1v2w3
Create Date: 2026-04-15

Persists every accepted X-Drift-Response hash for this agent. The gate
rejects any response whose hash already appears in this list (replay).

Reset only by the clear-drift handler (after human intervention) or by
an admin — NOT by routine contract refresh. That means a cached response
from three cycles ago is still caught, which is the whole point.

Capped at 200 entries in the gate logic; oldest are evicted first.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "s9t0u1v2w3x4"
down_revision = "r8s9t0u1v2w3"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "agents",
        sa.Column(
            "drift_eval_hashes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade():
    op.drop_column("agents", "drift_eval_hashes")
