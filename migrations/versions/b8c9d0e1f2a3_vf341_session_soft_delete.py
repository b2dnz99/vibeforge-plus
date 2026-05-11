"""VF-341 — sessions soft-delete columns (revoked_at + revoked_by + revoke_reason).

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-04-28

Adds the three soft-delete columns to the sessions table per
SESSION-LIFECYCLE-PROPOSAL §6.2:

  - revoked_at TIMESTAMPTZ NULL — soft-delete marker; auth check stays
    single-WHERE (`expires_at > now AND revoked_at IS NULL`).
  - revoked_by VARCHAR(36) NULL — FK-shaped (no enforced FK to keep
    auto-revoke from auto-actor possible); NULL for auto-revoke paths.
  - revoke_reason VARCHAR(64) NULL — short tag: "operator", "auto-stale",
    "auto-cap", "elevation-expired", etc.

State derivation per §4.1 stays a pure read-time computation; no state
column on the row.
"""
from alembic import op
import sqlalchemy as sa


revision = "b8c9d0e1f2a3"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sessions",
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("sessions",
        sa.Column("revoked_by", sa.String(36), nullable=True))
    op.add_column("sessions",
        sa.Column("revoke_reason", sa.String(64), nullable=True))
    # Partial index on the live set: most queries filter `revoked_at IS NULL`.
    # Cheap to add now; the query planner picks it up automatically.
    op.create_index(
        "ix_sessions_revoked_at_null",
        "sessions",
        ["expires_at"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade():
    op.drop_index("ix_sessions_revoked_at_null", table_name="sessions")
    op.drop_column("sessions", "revoke_reason")
    op.drop_column("sessions", "revoked_by")
    op.drop_column("sessions", "revoked_at")
