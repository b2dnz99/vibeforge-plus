"""Add is_completion_note flag to task_notes

Revision ID: g7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-04-04
"""
from alembic import op
import sqlalchemy as sa

revision = "g7b8c9d0e1f2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("task_notes", sa.Column("is_completion_note", sa.Boolean(), nullable=False, server_default="false"))


def downgrade():
    op.drop_column("task_notes", "is_completion_note")
