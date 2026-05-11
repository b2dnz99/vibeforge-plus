"""Add drift_eval_count to agents for self-eval gate tracking.

Revision ID: q7r8s9t0u1v2
Revises: p6q7r8s9t0u1
Create Date: 2026-04-13

Tracks how many self-eval prompts the agent has answered in the current
refresh cycle. Reset to 0 on each contract refresh. NULL = not yet
evaluated this cycle.
"""
from alembic import op
import sqlalchemy as sa

revision = "q7r8s9t0u1v2"
down_revision = "p6q7r8s9t0u1"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agents", sa.Column("drift_eval_count", sa.Integer(), nullable=True))


def downgrade():
    op.drop_column("agents", "drift_eval_count")
