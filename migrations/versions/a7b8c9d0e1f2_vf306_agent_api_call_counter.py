"""VF-306 — agent api_call_count + api_call_count_since columns.

Revision ID: a7b8c9d0e1f2
Revises: z6a7b8c9d0e1
Create Date: 2026-04-28

Adds two columns to `agents` for the bare-minimum API-call counter
(VF-306 item 1):

  - api_call_count INTEGER NOT NULL DEFAULT 0
    Cumulative count of authenticated requests bearing this agent's token.
    Incremented in projects.py agent-auth helper (next to last_seen_at).
    Reset to 0 on every token cycle (admin.py cycle_agent_token).

  - api_call_count_since TIMESTAMPTZ NOT NULL DEFAULT now()
    Marks the start of the current counting window. Defaults to row
    creation; reset to now() on token cycle. UI displays "Total: N
    (since <date>)".

This is the bare-minimum cut per the v0.2.0 proposal — no bucketing,
no per-window queries. Bucketed/windowed views deferred to POST RC/1.0
under VF-337 umbrella.
"""
from alembic import op
import sqlalchemy as sa


revision = "a7b8c9d0e1f2"
down_revision = "z6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade():
    # NOTE: NOT NULL with server_default so the migration backfills existing
    # rows (cumulative count starts at 0; counter-since starts at NOW for
    # all pre-existing agents — they "begin tracking" at migration time).
    op.add_column(
        "agents",
        sa.Column(
            "api_call_count",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "agents",
        sa.Column(
            "api_call_count_since",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade():
    op.drop_column("agents", "api_call_count_since")
    op.drop_column("agents", "api_call_count")
