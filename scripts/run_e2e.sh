#!/usr/bin/env bash
#
# VF-306 follow-on (PK 2026-04-28): E2E run wrapper that captures the suite
# output + per-test screenshots into a timestamped folder under 0-E2E-Runs/
# along with a NOTES.md skeleton for operator highlights.
#
# Usage:
#   scripts/run_e2e.sh <slug> [target]
#     slug    short kebab-case identifier (e.g. vf306-agent-telemetry)
#     target  dev | uat | prod (default: uat)
#
# Examples:
#   scripts/run_e2e.sh vf306-agent-telemetry uat
#   scripts/run_e2e.sh vf344-portal-ia-reorder uat
#
# Output structure:
#   0-E2E-Runs/2026-04-28T1620Z-vf306-agent-telemetry/
#     ├── NOTES.md          # auto-stamped header + sections for highlights/flags
#     ├── output.log        # full pytest stdout
#     └── screenshots/      # one PNG per test (pass + fail)
#
# Discipline: codified in 0-MD/0-Documentation/internal/sdlc-mature.md §4.8 and
# CLAUDE.md "Testing Discipline" section. Every promote that goes through the
# E2E gate should be captured this way so the run history is browseable.
set -euo pipefail

SLUG="${1:?Usage: $0 <slug> [target]}"
TARGET="${2:-uat}"
TIMESTAMP=$(date -u +%Y-%m-%dT%H%MZ)

case "$TARGET" in
  dev)  HOSTNAME="vibeforge-dev" ;;
  uat)  HOSTNAME="vibeforge-uat" ;;
  prod) HOSTNAME="vibeforge"     ;;
  *)    echo "ERROR: target must be one of: dev, uat, prod (got: $TARGET)"; exit 2 ;;
esac

# VF-335 follow-on (PK 2026-04-28): on PROD, deselect tests marked
# `requires_auth`. PROD has different SU/SA credentials than the dev/uat
# seeds the conftest fixtures default to, so auth-dependent tests would all
# return HTTP 401 and pollute the run with false-failures. The unauth
# coverage (test_admin_graduation.py + any future unauth tests) still runs.
# DEV and UAT keep running everything. Future enhancement: dedicated PROD
# test-cred path (see NOTES on the 1550Z PROD run for options).
PYTEST_M_FILTER=""
if [ "$TARGET" = "prod" ]; then
  PYTEST_M_FILTER="-m 'not requires_auth'"
fi

# Repo root assumed = parent of scripts/ — script can be invoked from anywhere.
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUN_DIR="$REPO_ROOT/0-E2E-Runs/${TIMESTAMP}-${SLUG}"
mkdir -p "$RUN_DIR/screenshots"

echo "── E2E run capture ──────────────────────────────────────"
echo "Slug:       $SLUG"
echo "Target:     $TARGET ($HOSTNAME)"
echo "Run dir:    $RUN_DIR"
echo

# Run the suite on the target VM. Always exit-0 here so we can capture even
# on failure; the actual exit status is recovered from output.log via grep.
START_TS=$(date -u +%s)
if [ -n "$PYTEST_M_FILTER" ]; then
  # Override the playwright service's default command so we can inject the
  # marker filter. Mirrors the docker-compose.yml command line + filter.
  REMOTE_CMD="cd /opt/vibeforge && sudo rm -rf tests/e2e/_artifacts/screenshots && sudo docker compose --profile test run --rm playwright bash -c \"pip install --quiet pytest pytest-playwright playwright==1.58.0 && pytest tests/e2e/ -v --tb=short ${PYTEST_M_FILTER}\""
else
  REMOTE_CMD="cd /opt/vibeforge && sudo rm -rf tests/e2e/_artifacts/screenshots && sudo docker compose --profile test run --rm playwright"
fi
ssh "$HOSTNAME" "$REMOTE_CMD" 2>&1 | tee "$RUN_DIR/output.log" || true
END_TS=$(date -u +%s)
DURATION=$((END_TS - START_TS))

# Pull screenshots back from the VM.
SHOT_REMOTE="/opt/vibeforge/tests/e2e/_artifacts/screenshots"
SHOT_COUNT_REMOTE=$(ssh "$HOSTNAME" "sudo ls $SHOT_REMOTE 2>/dev/null | wc -l" || echo 0)
if [ "$SHOT_COUNT_REMOTE" -gt 0 ]; then
  # tar-stream pattern keeps perms simple + works under sudo on the remote side.
  ssh "$HOSTNAME" "cd $SHOT_REMOTE && sudo tar c ." | tar x -C "$RUN_DIR/screenshots/"
fi
SHOT_COUNT=$(ls "$RUN_DIR/screenshots" 2>/dev/null | wc -l)

# Parse the pytest summary line out of the log for the NOTES header.
SUMMARY=$(grep -E "passed|failed" "$RUN_DIR/output.log" | tail -1 || echo "(see output.log)")

# Write NOTES.md skeleton.
cat > "$RUN_DIR/NOTES.md" <<EOF
# E2E Run — ${SLUG}

**Date:**       $(date -u +%Y-%m-%dT%H:%M:%SZ)
**Target:**     ${TARGET} (${HOSTNAME}.hydra.net.au)
**Trigger:**    ${SLUG}
**Suite:**      tests/e2e/
**Duration:**   ${DURATION}s
**Result:**     ${SUMMARY}
**Screenshots:** ${SHOT_COUNT} captured (see ./screenshots/)

## Highlights
_Operator notes — what worked, what surprised, what looks right._

-

## Flags
_Anything that needs PK attention, follow-on tickets, deferred scope spotted during the run._

-

## Reference
- output.log — full pytest stdout
- screenshots/ — one PNG per test (pass + fail)
- Discipline: 0-MD/0-Documentation/internal/sdlc-mature.md §4.8
EOF

echo
echo "── Captured ─────────────────────────────────────────────"
echo "  $RUN_DIR/"
echo "    NOTES.md         (edit to add highlights/flags)"
echo "    output.log       ($SUMMARY)"
echo "    screenshots/     ($SHOT_COUNT files)"
echo

# CLAUDE.md Testing Discipline rule 4: post-promote Docker hygiene.
# Auto-prune on green runs against UAT/PROD (the promote-gate targets).
# DEV intentionally skipped — DEV churns less and operator sometimes wants
# the previous image kept around for ad-hoc debug.
# Skip when 0 passed (pytest didn't even start; nothing to clean).
# Skip if any "failed" appears in the summary — preserves rollback target.
# Override with NO_PRUNE=1 (e.g. mid-rollback investigation).
if [ "$TARGET" != "dev" ] && [ "${NO_PRUNE:-0}" != "1" ] \
   && echo "$SUMMARY" | grep -q "passed" \
   && ! echo "$SUMMARY" | grep -q "failed"; then
  echo "── Post-promote prune (CLAUDE.md rule 4) ────────────────"
  ssh "$HOSTNAME" "docker image prune -a -f 2>&1 | tail -1; docker builder prune -f 2>&1 | tail -1; df -h / | tail -1" || true
  echo
fi

echo "Next: open $RUN_DIR/NOTES.md, fill the Highlights/Flags sections."
