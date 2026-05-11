"""Add task_notes table

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d1
Create Date: 2026-03-28
"""
from alembic import op
import sqlalchemy as sa

revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "task_notes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("author_type", sa.String(20), nullable=False, server_default="human"),
        sa.Column("author_name", sa.String(100), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade():
    op.drop_table("task_notes")
