"""Drift gate v4.1 — storage refactor: drift_escalations + drift_eval_attempts tables.

Revision ID: u1v2w3x4y5z6
Revises: t0u1v2w3x4y5
Create Date: 2026-04-15

Option B refactor per SYNC-ARCH-EXPERIMENT.md §2:

- New table `drift_escalations` — one row per escalation, ended_at=NULL while active.
- New table `drift_eval_attempts` — one row per drift-eval prompt/response (observability).
- Drop `agents.drift_escalated_at` (moved to drift_escalations)
- Drop `tasks.has_active_drift_flag` (derived from drift_escalations EXISTS-query)
- Drop `tasks.drift_escalated_agent_id` (moved to drift_escalations)

Keep on agents: drift_eval_count, drift_eval_passed_at, drift_eval_hashes (cycle memory, unchanged).
Keep on task_notes: is_internal (generic human-only flag, still used for audit notes).

Dev only — no data migration, no prod at this revision.
"""
from alembic import op
import sqlalchemy as sa

revision = "u1v2w3x4y5z6"
down_revision = "t0u1v2w3x4y5"
branch_labels = None
depends_on = None


def upgrade():
    # drift_escalations — active + historical
    op.create_table(
        "drift_escalations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("agent_id", sa.String(length=36), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True, index=True),
        sa.Column("cleared_by", sa.String(length=36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("cleared_reason", sa.Text(), nullable=True),
    )
    # Partial index for "active" lookups — the common hot path
    op.create_index(
        "ix_drift_escalations_active",
        "drift_escalations",
        ["agent_id"],
        postgresql_where=sa.text("ended_at IS NULL"),
    )

    # drift_eval_attempts — per-prompt observability
    op.create_table(
        "drift_eval_attempts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("agent_id", sa.String(length=36), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("escalation_id", sa.String(length=36), sa.ForeignKey("drift_escalations.id", ondelete="SET NULL"), nullable=True, index=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("question_idx", sa.Integer(), nullable=False),
        sa.Column("response_hash", sa.String(length=64), nullable=True),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        # outcome: 'prompt' (no response yet), 'accepted', 'dedup', 'too_short', 'too_long', 'escalated'
    )

    # Drop the sprinkled columns — state now lives in drift_escalations
    op.drop_constraint("fk_tasks_drift_escalated_agent", "tasks", type_="foreignkey")
    op.drop_column("tasks", "drift_escalated_agent_id")
    op.drop_column("tasks", "has_active_drift_flag")
    op.drop_column("agents", "drift_escalated_at")


def downgrade():
    # Re-add the dropped columns
    op.add_column(
        "agents",
        sa.Column("drift_escalated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column("has_active_drift_flag", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "tasks",
        sa.Column("drift_escalated_agent_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_tasks_drift_escalated_agent",
        "tasks",
        "agents",
        ["drift_escalated_agent_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Drop the new tables
    op.drop_table("drift_eval_attempts")
    op.drop_index("ix_drift_escalations_active", table_name="drift_escalations")
    op.drop_table("drift_escalations")
