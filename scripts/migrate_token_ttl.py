#!/usr/bin/env python3
"""VF-341 §7 — Token TTL backfill script.

One-shot per env. Backfills `agents.expires_at` for rows where it's NULL
(today's eternal-token state) to `now + ttl_days` (default 90d). Operator
names exempt agents on the CLI — exemptions are NOT baked into source so
a future deploy with differently-named operational agents doesn't get
silently locked out.

Usage (run from repo root inside the app container):
  docker compose exec -T app python scripts/migrate_token_ttl.py \\
      --exempt-agent vibeforge \\
      --exempt-agent claude-vibeforge-plus \\
      --ttl-days 90

The script prints a confirmation summary listing exemptions BEFORE acting
— operator gets one chance to verify they didn't typo a name before the
irreversible mutation. Pass --yes to skip the prompt for automation.

Idempotent: re-runs only touch rows still at NULL expires_at; once a row
is backfilled the next run leaves it alone.
"""
import argparse
import sys
from datetime import datetime, timezone, timedelta


def main():
    parser = argparse.ArgumentParser(description="VF-341 token TTL backfill")
    parser.add_argument("--exempt-agent", action="append", default=[],
                        help="Agent name to exempt from TTL backfill (repeatable)")
    parser.add_argument("--exempt-user-id", action="append", default=[],
                        help="User ID whose agents to exempt (repeatable)")
    parser.add_argument("--ttl-days", type=int, default=90,
                        help="TTL in days for backfilled tokens (default 90)")
    parser.add_argument("--yes", action="store_true",
                        help="Skip the confirm prompt (for scripted runs).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would change but don't mutate.")
    args = parser.parse_args()

    # Late imports — let argparse fail fast on bad CLI before touching the app.
    from app.db.session import SessionLocal
    from app.models.agent import Agent

    cutoff = datetime.now(timezone.utc) + timedelta(days=args.ttl_days)
    db = SessionLocal()
    try:
        q = db.query(Agent).filter(
            Agent.expires_at.is_(None),
            Agent.revoked_at.is_(None),
        )
        if args.exempt_agent:
            q = q.filter(~Agent.name.in_(args.exempt_agent))
        if args.exempt_user_id:
            q = q.filter(~Agent.created_by.in_(args.exempt_user_id))

        count = q.count()
        sample = q.order_by(Agent.name).limit(10).all()

        print("=" * 60)
        print(f"VF-341 token TTL backfill")
        print("=" * 60)
        print(f"  TTL:          {args.ttl_days} days")
        print(f"  Cutoff:       {cutoff.isoformat()}")
        print(f"  Exempt names: {args.exempt_agent or '(none)'}")
        print(f"  Exempt users: {args.exempt_user_id or '(none)'}")
        print(f"  Eligible:     {count} agent token(s)")
        if sample:
            print(f"  Sample (first {len(sample)}):")
            for a in sample:
                print(f"    - {a.name}  id={a.id[:8]}  created_by={a.created_by[:8] if a.created_by else '?'}")
        print("=" * 60)

        if args.dry_run:
            print("--dry-run set; no changes made.")
            return 0
        if count == 0:
            print("Nothing to backfill (all tokens already have expires_at OR are exempt). Exiting.")
            return 0

        if not args.yes:
            try:
                input("Confirm with ENTER, Ctrl-C to abort... ")
            except KeyboardInterrupt:
                print("\nAborted.")
                return 1

        n = q.update({"expires_at": cutoff}, synchronize_session=False)
        db.commit()
        print(f"Backfilled {n} agent tokens to expires_at={cutoff.isoformat()}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
