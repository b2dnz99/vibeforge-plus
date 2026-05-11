"""VF-353 — projects.onboard_state JSONB column for customer-onboard mechanism.

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-04-30

Adds the onboard_state JSONB column to the projects table per
CUSTOMER-ONBOARD-PROPOSAL §3.3:

  - onboard_state JSONB NOT NULL DEFAULT '{}'::jsonb — flexible map carrying
    per-step hashes for the first-onboard workflow. Expected keys (all optional
    until set by the agent during onboard):
      framing_acknowledged   sha256:... · set in step 4a
      doc_complexity         "minimal"|"medium"|"heavy" · set in step 4b
      plan_hash              sha256:... · set in step 4c
      tooling_hash           sha256:... · set in step 4d
      agent_md_hash          sha256:... · set in step 4e (gate-clearing)
                             — content of CLAUDE.md (Claude) or AGENT.md (most others)
      completed_at           ISO-8601 timestamp · set when agent_md_hash registers

Schema validation lives at the application layer (no DB CHECK constraint) —
the field is intentionally flexible to accommodate future per-step additions
without migrations. Existing rows get '{}' on backfill.

The onboard gate (separate slice) reads agent_md_hash from this column to
decide whether to allow writes under /projects/{slug}/* and /tasks/*.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "c9d0e1f2a3b4"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "projects",
        sa.Column(
            "onboard_state",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    # GRANDFATHER: every project that exists at migration time predates the
    # onboard mechanism. If we let them pass through with onboard_state = {},
    # the onboard gate (in app/api/v2/projects.py::_resolve_actor) would block
    # ALL agent writes on those projects — breaking dogfood + every customer
    # already in the wild. Backfill with a sentinel marker so the gate's
    # check (`agent_md_hash AND completed_at` both truthy) passes for legacy
    # projects without giving them a real cryptographic hash they don't have.
    #
    # New projects (created after this migration) start with onboard_state = {}
    # via server_default and DO go through the gate, which is the intended path.
    op.execute(sa.text("""
        UPDATE projects
        SET onboard_state = jsonb_build_object(
            'agent_md_hash', 'legacy:grandfathered',
            'completed_at', to_char(now() at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
            'grandfathered_in_migration', 'c9d0e1f2a3b4'
        )
        WHERE onboard_state = '{}'::jsonb
    """))


def downgrade():
    op.drop_column("projects", "onboard_state")
