#!/usr/bin/env bash
#
# CLAUDE.md Testing Discipline rule 4 / sdlc-mature.md §4.9 — weekly Docker
# hygiene safety net. Runs the prune-pair with a 7-day TTL filter so the
# most-recent promote's old `app` image stays available as a rollback target.
#
# Install (root, on each VM):
#   sudo install -m 755 /opt/vibeforge/scripts/vf_docker_prune.sh \
#     /etc/cron.weekly/vf-docker-prune
#
# /etc/cron.weekly/* runs every Sunday around 06:25 (Debian/Ubuntu default).
# Logs to /var/log/vf-docker-prune.log; rotated by logrotate's catch-all.
#
# Pairs with the per-promote auto-prune in scripts/run_e2e.sh — that one
# fires immediately after a green UAT/PROD E2E. This cron catches anything
# that bypassed the wrapper (manual deploys, failed promotes that later got
# fixed without a follow-up E2E run, etc.).
set -euo pipefail

LOG="/var/log/vf-docker-prune.log"
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

{
  echo "── vf-docker-prune $TS ──"
  echo "BEFORE:"
  df -h / | tail -1
  echo

  # --filter "until=168h" preserves anything <7 days old. Last week's images
  # stay around as rollback targets; older orphans go.
  echo "image prune -a -f --filter until=168h:"
  docker image prune -a -f --filter "until=168h" 2>&1 | tail -3
  echo

  # builder prune doesn't accept until-style filters in older Docker; use
  # --keep-storage if you want a floor instead. For now, full builder prune
  # is acceptable — build cache is reproducible from layers any time.
  echo "builder prune -f:"
  docker builder prune -f 2>&1 | tail -3
  echo

  echo "AFTER:"
  df -h / | tail -1
  echo
} >> "$LOG" 2>&1
