"""Add last_contract_read_at to sessions for context drift refresh.

Revision ID: n4o5p6q7r8s9
Revises: m3n4o5p6q7r8
Create Date: 2026-04-12

VF-266: Context Drift Refresh. One timestamp field on sessions. The gate
checks how long since the agent last read the contract; if > 1 hour,
the next mutation gets a 422 telling it to refresh. GET /agentnotes
resets the timestamp. See CONTEXT-DRIFT-REFRESH-ENGINEER.md for spec.
"""
from alembic import op
import sqlalchemy as sa

revision = "n4o5p6q7r8s9"
down_revision = "m3n4o5p6q7r8"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agents", sa.Column(
        "last_contract_read_at", sa.DateTime(timezone=True), nullable=True
    ))


def downgrade():
    op.drop_column("agents", "last_contract_read_at")
