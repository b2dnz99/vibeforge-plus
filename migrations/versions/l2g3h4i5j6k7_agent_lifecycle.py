"""Agent lifecycle: project scoping, creator tracking, token security

Revision ID: l2g3h4i5j6k7
Revises: k1f2g3h4i5j6
Create Date: 2026-04-06
"""
from alembic import op
import sqlalchemy as sa

revision = "l2g3h4i5j6k7"
down_revision = "k1f2g3h4i5j6"
branch_labels = None
depends_on = None


def upgrade():
    # -- Agent lifecycle fields --
    # WHY: project_id scopes agent to one project. created_by tracks who provisioned.
    op.add_column("agents", sa.Column("project_id", sa.String(36), nullable=True))
    op.add_column("agents", sa.Column("created_by", sa.String(36), nullable=True))
    op.add_column("agents", sa.Column("model_type", sa.String(20), nullable=True))
    op.add_column("agents", sa.Column("model_name", sa.String(50), nullable=True))
    op.add_column("agents", sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("agents", sa.Column("revoked_by", sa.String(36), nullable=True))
    op.add_column("agents", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))

    # WHY: Plaintext token is a security hole. Hash only + prefix for display.
    op.drop_column("agents", "api_token_plain")

    # -- Backfill existing agents --
    # Both Claude and Codex scoped to vibeforge-plus, created by first user (pkhan)
    op.execute(sa.text("""
        UPDATE agents SET
            project_id = 'd07e32b1-c6ac-4d0b-8745-2d1df56fd5bd',
            created_by = (SELECT id FROM users WHERE username = 'pkhan' LIMIT 1),
            model_type = CASE WHEN LOWER(name) LIKE '%claude%' THEN 'claude' ELSE 'codex' END
    """))

    # -- Update slug format to project-scoped --
    op.execute(sa.text("""
        UPDATE agents SET slug = 'vibeforge-plus-' || LOWER(name)
        WHERE slug NOT LIKE '%-%'
    """))


def downgrade():
    op.add_column("agents", sa.Column("api_token_plain", sa.String(255), nullable=True))
    op.drop_column("agents", "expires_at")
    op.drop_column("agents", "revoked_by")
    op.drop_column("agents", "revoked_at")
    op.drop_column("agents", "model_name")
    op.drop_column("agents", "model_type")
    op.drop_column("agents", "created_by")
    op.drop_column("agents", "project_id")
