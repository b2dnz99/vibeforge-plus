"""Drift gate v4 — per-cycle pass flag.

Revision ID: t0u1v2w3x4y5
Revises: s9t0u1v2w3x4
Create Date: 2026-04-15

Completes the v4 state machine:

- `drift_eval_count` (existing) is repurposed as the pivot counter within
  a single firing: 0 = first attempt, 1-3 = pivots, 4 = escalation trigger.
- `drift_eval_passed_at` (this migration) is the per-cycle pass marker.
  Non-null = this refresh cycle has had a clean eval pass and the gate
  stops firing for the rest of the cycle. Null = gate still active.

Reset on contract refresh: count → 0, passed_at → NULL.
Reset on accepted eval: count → 0, passed_at → now.
Reset on escalation: drift_escalated_at → now (count / passed_at left as-is; agent is frozen until clear).
"""
from alembic import op
import sqlalchemy as sa

revision = "t0u1v2w3x4y5"
down_revision = "s9t0u1v2w3x4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "agents",
        sa.Column("drift_eval_passed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_column("agents", "drift_eval_passed_at")
