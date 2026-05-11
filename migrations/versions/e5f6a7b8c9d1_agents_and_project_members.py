"""agents and project_members tables

Revision ID: e5f6a7b8c9d1
Revises: e5f6a7b8c9d0
Create Date: 2026-03-30

NOTE 2026-04-10 (VF dev reset rehearsal): originally shared revision id
e5f6a7b8c9d0 with add_phases_table.py — duplicate id caused alembic to
fail with multi-head error on a fresh DB. Renamed to e5f6a7b8c9d1 and
chained after phases. Prod at l2g3h4i5j6k7 already has both tables; this
rename is graph-only and does not affect already-migrated databases.
"""
from alembic import op
import sqlalchemy as sa

revision = "e5f6a7b8c9d1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("slug", sa.String(60), nullable=False, unique=True, index=True),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("api_token_hash", sa.String(255), nullable=True),
        sa.Column("api_token_plain", sa.String(255), nullable=True),  # transient, dropped by l2g3h4i5j6k7
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "project_members",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=True),
        sa.Column("role", sa.String(20), nullable=False, server_default="write"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade():
    op.drop_table("project_members")
    op.drop_table("agents")
