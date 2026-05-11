"""Drift gate v4 schema — escalation freeze + task flag + internal notes.

Revision ID: r8s9t0u1v2w3
Revises: q7r8s9t0u1v2
Create Date: 2026-04-15

v4 mechanism (see 0-MD/proposed/SYNC-ARCH-EXPERIMENT.md):

- agents.drift_escalated_at — timestamp; non-null = agent writes return 403
  until human clears via POST /tasks/{id}/clear-drift
- tasks.has_active_drift_flag — bool; UI renders DRIFT FLAGGED badge
- tasks.drift_escalated_agent_id — FK to agents; which agent to unfreeze
  on clear
- task_notes.is_internal — bool; human-only visibility. Used by drift
  audit notes AND general internal human discussion (security plane,
  customer-hidden content). Agents cannot set this; agent-facing note
  GETs strip rows where is_internal=true.

Four new columns. Reversible.
"""
from alembic import op
import sqlalchemy as sa

revision = "r8s9t0u1v2w3"
down_revision = "q7r8s9t0u1v2"
branch_labels = None
depends_on = None


def upgrade():
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
    op.add_column(
        "task_notes",
        sa.Column("is_internal", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade():
    op.drop_column("task_notes", "is_internal")
    op.drop_constraint("fk_tasks_drift_escalated_agent", "tasks", type_="foreignkey")
    op.drop_column("tasks", "drift_escalated_agent_id")
    op.drop_column("tasks", "has_active_drift_flag")
    op.drop_column("agents", "drift_escalated_at")
