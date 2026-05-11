"""Add username field to users

Revision ID: k1f2g3h4i5j6
Revises: j0e1f2g3h4i5
Create Date: 2026-04-05
"""
from alembic import op
import sqlalchemy as sa
import bcrypt

revision = "k1f2g3h4i5j6"
down_revision = "j0e1f2g3h4i5"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("username", sa.String(10), nullable=True, unique=True))
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    # Gate the SA-backfill block on "existing users present" — i.e. this is a
    # real upgrade, not a fresh-DB install. Without this gate, a clean
    # vibeforge-install.sh run creates the username column AND inserts a phantom
    # SA (sa@vibeforge.local), which then breaks the install wizard's "no SA
    # exists -> install_open=True" check. Fix surfaced 2026-05-09 during the
    # 0.7.0-RC handover validation pass on DEV.
    bind = op.get_bind()
    existing_count = bind.execute(sa.text("SELECT COUNT(*) FROM users")).scalar()
    if existing_count == 0:
        # Fresh DB — no users to backfill, no phantom SA needed. The install
        # wizard / bootstrap.create-sa endpoint will provision the real SA.
        return

    # Backfill path (real upgrade from j0e1f2g3h4i5):
    # - existing user becomes SU (super_user) with username 'pkhan'
    # - new SA seeded with default creds; the operator changes via reset_sa_password.py
    import os
    _pw = os.environ.get("VF_BOOTSTRAP_PASSWORD", "changeme-on-first-login")
    password_hash = bcrypt.hashpw(_pw.encode(), bcrypt.gensalt()).decode()
    op.execute(
        sa.text(f"""
            UPDATE users SET username = 'pkhan', role = 'super_user', password_hash = '{password_hash}',
                   nickname = 'Parvez Khan'
            WHERE role = 'super_admin'
        """)
    )
    # Create new SA account: superadmin
    import uuid
    sa_id = str(uuid.uuid4())
    sa_hash = bcrypt.hashpw(_pw.encode(), bcrypt.gensalt()).decode()
    op.execute(
        sa.text(f"""
            INSERT INTO users (id, username, email, display_name, role, password_hash, status, nickname, created_at, updated_at)
            VALUES ('{sa_id}', 'superadmin', 'sa@vibeforge.local', 'Super Admin', 'super_admin',
                    '{sa_hash}', 'active', 'SA', NOW(), NOW())
        """)
    )


def downgrade():
    op.drop_index("ix_users_username", "users")
    op.drop_column("users", "username")
