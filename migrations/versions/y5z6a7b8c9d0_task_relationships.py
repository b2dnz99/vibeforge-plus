"""task_relationships for VF-304 expanded — related many-to-many primitive.

Revision ID: y5z6a7b8c9d0
Revises: x4y5z6a7b8c9
Create Date: 2026-04-22

Adds a small table for loose "related" task↔task associations (no gating
semantics; complements `blocked_by_task_id` which is 1:1 + gating).

Rows are stored canonically: lower id in `a_id`, higher in `b_id`. Uniqueness
enforced on the pair. Reads query both columns when collecting a task's
related list. A `kind` column leaves room for future relationship types
without another migration.
"""
from alembic import op
import sqlalchemy as sa

revision = "y5z6a7b8c9d0"
down_revision = "x4y5z6a7b8c9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "task_relationships",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("a_id", sa.String(36),
                  sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("b_id", sa.String(36),
                  sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("kind", sa.String(20), nullable=False, server_default="related"),
        sa.Column("reason", sa.Text, nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(36), nullable=True),
        sa.UniqueConstraint("a_id", "b_id", "kind", name="uq_task_relationships_pair_kind"),
        sa.CheckConstraint("a_id <> b_id", name="ck_task_relationships_not_self"),
    )


def downgrade():
    op.drop_table("task_relationships")
