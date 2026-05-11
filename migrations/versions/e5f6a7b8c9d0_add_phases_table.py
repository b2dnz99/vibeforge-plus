"""Add phases table and phase_id to tasks

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-03-28
"""
from alembic import op
import sqlalchemy as sa

revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "phases",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("milestone_id", sa.String(36), sa.ForeignKey("milestones.id", ondelete="CASCADE"), nullable=True, index=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.add_column("tasks", sa.Column("phase_id", sa.String(36), sa.ForeignKey("phases.id", ondelete="SET NULL"), nullable=True))


def downgrade():
    op.drop_column("tasks", "phase_id")
    op.drop_table("phases")
