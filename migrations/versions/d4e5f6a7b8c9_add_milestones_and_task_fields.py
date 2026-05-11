"""Add milestones table + task_type, assignee_id, estimated_hours, milestone_id to tasks

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-03-27
"""
from alembic import op
import sqlalchemy as sa

revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade():
    # Create milestones table
    op.create_table(
        "milestones",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("label", sa.String(50), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("target_date", sa.Date(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # Add new columns to tasks
    op.add_column("tasks", sa.Column("milestone_id", sa.String(36), sa.ForeignKey("milestones.id", ondelete="SET NULL"), nullable=True))
    op.add_column("tasks", sa.Column("task_type", sa.String(20), nullable=True))
    op.add_column("tasks", sa.Column("assignee_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True))
    op.add_column("tasks", sa.Column("estimated_hours", sa.Float(), nullable=True))


def downgrade():
    op.drop_column("tasks", "estimated_hours")
    op.drop_column("tasks", "assignee_id")
    op.drop_column("tasks", "task_type")
    op.drop_column("tasks", "milestone_id")
    op.drop_table("milestones")
