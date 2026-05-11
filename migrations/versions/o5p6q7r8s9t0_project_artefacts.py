"""Project artefacts table — per-project versioned markdown storage.

Revision ID: o5p6q7r8s9t0
Revises: n4o5p6q7r8s9
Create Date: 2026-04-12

Per PROJECT-SCAFFOLD-PROPOSAL.md §4. First artefact type: 'contract'
(the documentation contract). Versioned, audited, 64KB capped.
Agent read access via GET, human write access via PUT.
"""
from alembic import op
import sqlalchemy as sa

revision = "o5p6q7r8s9t0"
down_revision = "n4o5p6q7r8s9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "project_artefacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("name", sa.String(50), nullable=False),  # e.g. 'contract', 'plan', 'handover'
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column("actor_type", sa.String(10), nullable=False),  # 'human' or 'agent'
        sa.Column("actor_name", sa.String(100), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),  # sha256 of body
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_artefact_project_name", "project_artefacts", ["project_id", "name"])


def downgrade():
    op.drop_index("ix_artefact_project_name", table_name="project_artefacts")
    op.drop_table("project_artefacts")
