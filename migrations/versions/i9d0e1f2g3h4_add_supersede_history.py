"""Add supersede_history JSON to task_notes

Revision ID: i9d0e1f2g3h4
Revises: h8c9d0e1f2g3
Create Date: 2026-04-05
"""
from alembic import op
import sqlalchemy as sa

revision = "i9d0e1f2g3h4"
down_revision = "h8c9d0e1f2g3"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("task_notes", sa.Column("supersede_history", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("task_notes", "supersede_history")
