"""system_settings key-value table + seed drift_gate_enabled=true on dev.

Revision ID: v2w3x4y5z6a7
Revises: u1v2w3x4y5z6
Create Date: 2026-04-15

Minimal key-value table for global app flags. First consumer is the drift
gate's system-wide enable bool; future flags can land here without more
migrations.

Seeded with `drift_gate_enabled = 'true'` on migrate (dev default — the
experimental drift gate is ON by default on dev). For fresh UAT/prod
installs that want it off, post-install SQL can flip it to 'false'.
"""
from alembic import op
import sqlalchemy as sa

revision = "v2w3x4y5z6a7"
down_revision = "u1v2w3x4y5z6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(length=100), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column(
            "updated_by", sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
    )
    op.execute(
        "INSERT INTO system_settings (key, value) VALUES ('drift_gate_enabled', 'true')"
    )


def downgrade():
    op.drop_table("system_settings")
