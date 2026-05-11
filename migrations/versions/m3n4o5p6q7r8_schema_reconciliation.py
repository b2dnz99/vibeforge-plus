"""Schema reconciliation — columns and tables that exist on prod but were
never captured in a migration.

Revision ID: m3n4o5p6q7r8
Revises: l2g3h4i5j6k7
Create Date: 2026-04-12

VF-256: Migration tree drift. These columns/tables were added to the
models and to prod via ad-hoc means (direct SQL, model-only changes
that relied on SQLAlchemy create_all, or sidecar processes) but never
had a corresponding alembic migration. This migration brings the
migration chain into full parity with the live prod schema.

Excluded: health_metrics (managed by the health sidecar container via
raw SQL CREATE TABLE IF NOT EXISTS — deliberately outside alembic).
"""
from alembic import op
import sqlalchemy as sa

revision = "m3n4o5p6q7r8"
down_revision = "l2g3h4i5j6k7"
branch_labels = None
depends_on = None


def upgrade():
    # -- enums: missing values --
    op.execute("ALTER TYPE task_priority_enum ADD VALUE IF NOT EXISTS 'critical'")

    # -- agents: missing columns --
    op.add_column("agents", sa.Column("token_prefix", sa.String(8), nullable=True))
    op.add_column("agents", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))

    # -- projects: missing columns --
    op.add_column("projects", sa.Column("project_number", sa.Integer(), nullable=True))
    op.add_column("projects", sa.Column("prefix", sa.String(4), nullable=True))
    op.add_column("projects", sa.Column("lifecycle_log", sa.Text(), nullable=False, server_default="[]"))
    op.add_column("projects", sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("projects", sa.Column("pin_order", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("projects", sa.Column("card_order", sa.Integer(), nullable=False, server_default="0"))

    # -- sessions: missing column --
    op.add_column("sessions", sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True))

    # -- tasks: missing columns --
    op.add_column("tasks", sa.Column("task_number", sa.Integer(), nullable=True))
    op.add_column("tasks", sa.Column("short_description", sa.String(120), nullable=False, server_default=""))

    # -- notifications: new table --
    op.create_table(
        "notifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("type", sa.String(50), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=True),
        sa.Column("task_id", sa.String(36), nullable=True),
        sa.Column("read", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # -- user_preferences: new table --
    op.create_table(
        "user_preferences",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("prefs_json", sa.Text(), nullable=True),
    )

    # -- user_project_pins: new table --
    op.create_table(
        "user_project_pins",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("pin_order", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("user_id", "project_id", name="uq_user_project_pin"),
    )


def downgrade():
    op.drop_table("user_project_pins")
    op.drop_table("user_preferences")
    op.drop_table("notifications")

    op.drop_column("tasks", "short_description")
    op.drop_column("tasks", "task_number")
    op.drop_column("sessions", "last_activity_at")
    op.drop_column("projects", "card_order")
    op.drop_column("projects", "pin_order")
    op.drop_column("projects", "pinned")
    op.drop_column("projects", "lifecycle_log")
    op.drop_column("projects", "prefix")
    op.drop_column("projects", "project_number")
    op.drop_column("agents", "last_seen_at")
    op.drop_column("agents", "token_prefix")
