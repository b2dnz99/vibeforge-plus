#!/usr/bin/env bash
# ── test_fixtures.sh ─────────────────────────────────────────
# Manage test users + project + agent for auth/RBAC validation.
#
# Usage:
#   ./scripts/test_fixtures.sh seed     # create test users + project + agent
#   ./scripts/test_fixtures.sh wipe     # remove all test fixtures
#   ./scripts/test_fixtures.sh cycle    # wipe + seed
#   ./scripts/test_fixtures.sh status   # show what is present
#
# Test users (all password=1234, must_change_password=false):
#   tsu      super_user (NOT a member of test-fixture — uses SU global)
#   towner   user, OWNER of test-fixture
#   tmember  user, MEMBER of test-fixture (write role, not owner)
#   tview    viewer, MEMBER of test-fixture (read-only)
#
# Test project: test-fixture (owned by towner)
# Test agent:   test-agent (token written to scripts/.test-agent-token)
# ─────────────────────────────────────────────────────────────

set -euo pipefail

CMD="${1:-status}"

case "$CMD" in
  seed|wipe|cycle|status) ;;
  *) echo "Usage: $0 {seed|wipe|cycle|status}"; exit 1 ;;
esac

# Run inside the app container (where bcrypt + models live)
docker compose exec -T -w /app -e PYTHONPATH=/app app python scripts/test_fixtures.py "$CMD"

# If seed/cycle, fetch the token file out of the container
if [ "$CMD" = "seed" ] || [ "$CMD" = "cycle" ]; then
  echo ""
  echo "Fetching test agent token..."
  docker compose cp app:/app/scripts/.test-agent-token ./scripts/.test-agent-token 2>/dev/null \
    && echo "  Token saved to scripts/.test-agent-token" \
    || echo "  (no token file — agent may already exist)"
fi
