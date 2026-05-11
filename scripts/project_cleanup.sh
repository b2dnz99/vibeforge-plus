#!/usr/bin/env bash
# ── project_cleanup.sh ──────────────────────────────────────
# Removes all child data from a project, preserving the project
# record itself (name, description, slug, settings).
#
# Usage:
#   ./scripts/project_cleanup.sh <project_slug>          # dry run
#   ./scripts/project_cleanup.sh <project_slug> --commit  # execute
#
# Runs inside Docker against the vibeforge DB.
# ─────────────────────────────────────────────────────────────

set -euo pipefail

SLUG="${1:-}"
COMMIT="${2:-}"

if [ -z "$SLUG" ]; then
  echo "Usage: $0 <project_slug> [--commit]"
  echo "  Dry run by default. Pass --commit to execute."
  exit 1
fi

# Resolve project ID
PID=$(docker compose exec -T db psql -U vibeforge -tAc \
  "SELECT id FROM projects WHERE slug = '$SLUG'")

if [ -z "$PID" ]; then
  echo "ERROR: No project found with slug '$SLUG'"
  exit 1
fi

PNAME=$(docker compose exec -T db psql -U vibeforge -tAc \
  "SELECT name FROM projects WHERE id = '$PID'")

echo "================================================"
echo "  Project Cleanup: $PNAME ($SLUG)"
echo "  Project ID: $PID"
echo "================================================"
echo ""

# ── Show past cleanups from lifecycle_log ──
PAST=$(docker compose exec -T db psql -U vibeforge -tAc \
  "SELECT lifecycle_log FROM projects WHERE id = '$PID'")

if [ "$PAST" != "[]" ] && [ -n "$PAST" ]; then
  echo "--- PREVIOUS CLEANUPS ---"
  echo "$PAST" | tr -d '\r' | python3 -c "
import json, sys
raw = sys.stdin.read().strip()
log = json.loads(raw)
cleanups = [e for e in log if e.get('action') == 'project_cleanup']
if not cleanups:
    print('  (none)')
else:
    for c in cleanups:
        ts = c.get('timestamp','?')
        d = c.get('deleted',{})
        print(f\"  {ts}  tasks={d.get('tasks',0)} notes={d.get('notes',0)} phases={d.get('phases',0)} milestones={d.get('milestones',0)} agents={d.get('agents',0)} members={d.get('members',0)} events={d.get('events',0)}\")
" 2>/dev/null || echo "  (parse error)"
  echo ""
fi

# ── Dry run: show what would be deleted ──

echo "--- TASKS ---"
docker compose exec -T db psql -U vibeforge -c \
  "SELECT task_number, title, status, owner_label FROM tasks WHERE project_id = '$PID' ORDER BY task_number;"

TASK_COUNT=$(docker compose exec -T db psql -U vibeforge -tAc \
  "SELECT count(*) FROM tasks WHERE project_id = '$PID'")

echo ""
echo "--- TASK NOTES ---"
NOTE_COUNT=$(docker compose exec -T db psql -U vibeforge -tAc \
  "SELECT count(*) FROM task_notes WHERE task_id IN (SELECT id FROM tasks WHERE project_id = '$PID')")
echo "  $NOTE_COUNT notes to delete"

echo ""
echo "--- PHASES ---"
docker compose exec -T db psql -U vibeforge -c \
  "SELECT name, sort_order FROM phases WHERE project_id = '$PID' ORDER BY sort_order;"

PHASE_COUNT=$(docker compose exec -T db psql -U vibeforge -tAc \
  "SELECT count(*) FROM phases WHERE project_id = '$PID'")

echo ""
echo "--- MILESTONES ---"
docker compose exec -T db psql -U vibeforge -c \
  "SELECT label, status FROM milestones WHERE project_id = '$PID' ORDER BY label;"

MS_COUNT=$(docker compose exec -T db psql -U vibeforge -tAc \
  "SELECT count(*) FROM milestones WHERE project_id = '$PID'")

echo ""
echo "--- AGENTS ---"
docker compose exec -T db psql -U vibeforge -c \
  "SELECT name, slug, status, model_type FROM agents WHERE project_id = '$PID';"

AGENT_COUNT=$(docker compose exec -T db psql -U vibeforge -tAc \
  "SELECT count(*) FROM agents WHERE project_id = '$PID'")

echo ""
echo "--- PROJECT MEMBERS ---"
docker compose exec -T db psql -U vibeforge -c \
  "SELECT pm.role, u.display_name as user_name, a.name as agent_name
   FROM project_members pm
   LEFT JOIN users u ON pm.user_id = u.id
   LEFT JOIN agents a ON pm.agent_id = a.id
   WHERE pm.project_id = '$PID';"

MEMBER_COUNT=$(docker compose exec -T db psql -U vibeforge -tAc \
  "SELECT count(*) FROM project_members WHERE project_id = '$PID'")

echo ""
echo "--- ACTIVITY EVENTS ---"
EVENT_COUNT=$(docker compose exec -T db psql -U vibeforge -tAc \
  "SELECT count(*) FROM activity_events WHERE project_id = '$PID'")
echo "  $EVENT_COUNT events to delete"

echo ""
echo "================================================"
echo "  SUMMARY — will delete:"
echo "    Tasks:           $TASK_COUNT"
echo "    Task notes:      $NOTE_COUNT"
echo "    Phases:          $PHASE_COUNT"
echo "    Milestones:      $MS_COUNT"
echo "    Agents:          $AGENT_COUNT"
echo "    Project members: $MEMBER_COUNT"
echo "    Activity events: $EVENT_COUNT"
echo ""
echo "  PRESERVED:"
echo "    Project record:  $PNAME ($SLUG)"
echo "    Project fields:  name, description, slug, status,"
echo "                     root_path, docs_path, project_url,"
echo "                     owner_id, created_at, settings"
echo "================================================"

if [ "$COMMIT" != "--commit" ]; then
  echo ""
  echo "  DRY RUN — no changes made."
  echo "  Re-run with --commit to execute."
  exit 0
fi

echo ""
echo "  EXECUTING cleanup..."
echo ""

# Order matters — foreign keys
docker compose exec -T db psql -U vibeforge -c "
BEGIN;

-- 1. Task notes (FK to tasks)
DELETE FROM task_notes WHERE task_id IN (SELECT id FROM tasks WHERE project_id = '$PID');

-- 2. Activity events
DELETE FROM activity_events WHERE project_id = '$PID';

-- 3. Tasks
DELETE FROM tasks WHERE project_id = '$PID';

-- 4. Phases
DELETE FROM phases WHERE project_id = '$PID';

-- 5. Milestones
DELETE FROM milestones WHERE project_id = '$PID';

-- 6. Project members (includes agent memberships)
DELETE FROM project_members WHERE project_id = '$PID';

-- 7. Agents
DELETE FROM agents WHERE project_id = '$PID';

COMMIT;
"

# ── Append to lifecycle_log ──
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
ENTRY="{\"action\":\"project_cleanup\",\"timestamp\":\"$TIMESTAMP\",\"deleted\":{\"tasks\":$TASK_COUNT,\"notes\":$NOTE_COUNT,\"phases\":$PHASE_COUNT,\"milestones\":$MS_COUNT,\"agents\":$AGENT_COUNT,\"members\":$MEMBER_COUNT,\"events\":$EVENT_COUNT}}"

# WHY: lifecycle_log is a Text column, not JSONB. Parse existing, append, write back as valid JSON string.
docker compose exec -T db psql -U vibeforge -c "
UPDATE projects
SET lifecycle_log = (
  SELECT COALESCE(lifecycle_log::jsonb, '[]'::jsonb) || '[$ENTRY]'::jsonb
  FROM projects WHERE id = '$PID'
)::text
WHERE id = '$PID';
"

echo "  Done. Project '$PNAME' is now empty."
echo "  Cleanup recorded in lifecycle_log."
echo "  Project record preserved with all metadata."
