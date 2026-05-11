"""add_milestone_label_to_tasks

Revision ID: a1b2c3d4e5f6
Revises: 036eb23e2efe
Create Date: 2026-03-27

"""
from alembic import op
import sqlalchemy as sa


revision = 'a1b2c3d4e5f6'
down_revision = '036eb23e2efe'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('tasks', sa.Column('milestone_label', sa.String(50), nullable=True))


def downgrade() -> None:
    op.drop_column('tasks', 'milestone_label')
