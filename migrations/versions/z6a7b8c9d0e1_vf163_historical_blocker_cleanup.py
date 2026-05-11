"""VF-163 historical cleanup — convert stale blocked_by to related.

Revision ID: z6a7b8c9d0e1
Revises: y5z6a7b8c9d0
Create Date: 2026-04-23

Any task currently carrying `blocked_by_task_id` that points at a task with
status in ('done', 'cancelled') is stale — the blocker is closed, yet the
downstream still displays a pink "blocked:" pill on the board. The forward
rule shipped in patch_task (see app/api/v2/projects.py) prevents NEW stale
links, but doesn't retroactively fix what was already in the DB when the
feature shipped.

This migration is a one-shot cleanup:
  1. For each (downstream, blocker) pair where blocker is done/cancelled,
     insert a canonical `related` row into task_relationships (skipping if
     one already exists).
  2. Null out the downstream's `blocked_by_task_id`.

Written as raw SQL because the logic is set-based and lives cleanly in two
statements. No audit events are emitted — this is a migration, not a user
action; the migration itself is the audit trail.
"""
from alembic import op


revision = "z6a7b8c9d0e1"
down_revision = "y5z6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade():
    # Insert `related` rows for each stale pair, canonical ordering (lower id first).
    # ON CONFLICT DO NOTHING guards the uniqueness constraint (a_id, b_id, kind) so
    # running this twice is safe.
    op.execute(
        """
        INSERT INTO task_relationships (id, a_id, b_id, kind, reason, created_at, created_by)
        SELECT
            gen_random_uuid()::text,
            LEAST(downstream.id, blocker.id),
            GREATEST(downstream.id, blocker.id),
            'related',
            'Auto-converted — blocker ' || COALESCE(
                (SELECT prefix FROM projects WHERE id = blocker.project_id) || '-' || blocker.task_number::text,
                substr(blocker.id, 1, 8)
            ) || ' was already ' || blocker.status || ' when VF-163 migration ran',
            NOW(),
            NULL
        FROM tasks downstream
        JOIN tasks blocker ON blocker.id = downstream.blocked_by_task_id
        WHERE blocker.status IN ('done', 'cancelled')
        ON CONFLICT (a_id, b_id, kind) DO NOTHING
        """
    )

    # Null out the stale blocked_by_task_id on every downstream we just converted.
    op.execute(
        """
        UPDATE tasks
        SET blocked_by_task_id = NULL
        WHERE blocked_by_task_id IN (
            SELECT id FROM tasks WHERE status IN ('done', 'cancelled')
        )
        """
    )


def downgrade():
    # Deliberately a no-op. The migration is destructive-by-design (it clears
    # stale blocked_by links that should not exist). Downgrade cannot recover
    # the original blocked_by values without an audit trail we did not capture,
    # and re-creating them would re-introduce the bug this migration fixes.
    pass
