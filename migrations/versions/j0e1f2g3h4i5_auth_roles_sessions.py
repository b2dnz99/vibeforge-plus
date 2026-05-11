"""Auth: roles, user profile fields, sessions table, project owner_id

Revision ID: j0e1f2g3h4i5
Revises: i9d0e1f2g3h4
Create Date: 2026-04-05
"""
from alembic import op
import sqlalchemy as sa

revision = "j0e1f2g3h4i5"
down_revision = "i9d0e1f2g3h4"
branch_labels = None
depends_on = None


def upgrade():
    # -- Users: role, profile fields, soft delete --
    op.add_column("users", sa.Column("role", sa.String(20), nullable=False, server_default="user"))
    op.add_column("users", sa.Column("display_role", sa.String(50), nullable=True))
    op.add_column("users", sa.Column("title", sa.String(100), nullable=True))
    op.add_column("users", sa.Column("nickname", sa.String(50), nullable=True))
    op.add_column("users", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("deleted_by", sa.String(36), nullable=True))
    op.add_column("users", sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.text("false")))

    # Expand status enum to include 'deleted'
    # Postgres enum alter: add new value
    op.execute("ALTER TYPE user_status_enum ADD VALUE IF NOT EXISTS 'deleted'")

    # -- Projects: owner_id --
    op.add_column("projects", sa.Column("owner_id", sa.String(36), nullable=True))

    # -- Sessions table --
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("session_type", sa.String(10), nullable=False, server_default="user"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])

    # -- Backfill: first user becomes super_admin, set password, update display_name --
    # NOTE: Migration already ran. Password redacted from source.
    import bcrypt, os
    _pw = os.environ.get("VF_BOOTSTRAP_PASSWORD", "changeme-on-first-login")
    password_hash = bcrypt.hashpw(_pw.encode(), bcrypt.gensalt()).decode()
    # Update first user to SA with username 'pkhan'
    op.execute(
        sa.text("""
            UPDATE users SET role = 'super_admin', display_name = 'Parvez Khan'
            WHERE id = (SELECT id FROM users ORDER BY created_at LIMIT 1)
        """)
    )
    op.execute(
        sa.text(f"""
            UPDATE users SET password_hash = '{password_hash}'
            WHERE id = (SELECT id FROM users ORDER BY created_at LIMIT 1)
        """)
    )
    # Backfill project owner_id from created_by_user_id (or first user)
    op.execute(
        sa.text("""
            UPDATE projects SET owner_id = COALESCE(created_by_user_id,
                (SELECT id FROM users ORDER BY created_at LIMIT 1))
        """)
    )


def downgrade():
    op.drop_table("sessions")
    op.drop_column("projects", "owner_id")
    op.drop_column("users", "deleted_by")
    op.drop_column("users", "deleted_at")
    op.drop_column("users", "nickname")
    op.drop_column("users", "title")
    op.drop_column("users", "display_role")
    op.drop_column("users", "role")
