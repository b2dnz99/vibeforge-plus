"""Add supersede fields to task_notes

Revision ID: h8c9d0e1f2g3
Revises: g7b8c9d0e1f2
Create Date: 2026-04-05
"""
from alembic import op
import sqlalchemy as sa

revision = "h8c9d0e1f2g3"
down_revision = "g7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("task_notes", sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("task_notes", sa.Column("superseded_by", sa.String(100), nullable=True))
    op.add_column("task_notes", sa.Column("superseded_reason", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("task_notes", "superseded_reason")
    op.drop_column("task_notes", "superseded_by")
    op.drop_column("task_notes", "superseded_at")
