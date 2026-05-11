---
title: reset_sa — Super Admin Password Reset
audience: rescue
ip: none
style: practical
status: Active
last_updated: 2026-04-08
tool_script: scripts/reset_sa.sh
tool_target: VibeForge+ app
---

# `reset_sa` — VibeForge+ Super Admin password reset

**When to use:** the SA has lost their password and cannot log in. Cannot recover via email (no email integration yet). Cannot use a second SA (only one in the install). The break-glass tool.

**What it does:** generates a random 16-character password, updates the SA user's password hash directly in the database, prints the new password to stdout once. Does NOT send the password anywhere — it's displayed for the operator to copy.

**Tier:** `audience: rescue` — read in emergencies, not for routine reading.

## Quick reference

```bash
# From the VM:
/opt/vibeforge/scripts/reset_sa.sh

# From host (via SSH):
ssh vibeforge "/opt/vibeforge/scripts/reset_sa.sh"
```

No subcommands. One script, one job. Always idempotent (can be run multiple times — each run generates a fresh random password).

## What it touches

- **Database:** the `users` table, specifically the row where `role = 'super_admin'`
- **Filesystem:** none
- **Network:** none

The script runs **inside the app container** via `docker compose exec`, which means it inherits the container's database connection from the existing `.env` config. No credentials in the script itself.

## Example output

```
==================================================
  VibeForge+ Super Admin Password Reset
==================================================

  User:     Parvez Khan (admin@example.com)
  Role:     super_admin
  Password: aB3$dE7fG9hJkL2!

  CHANGE THIS PASSWORD IMMEDIATELY AFTER LOGIN
  Go to: Admin Panel > View SA account > Change Password

==================================================
```

## What to do after running it

1. **Log in to the board** at `https://vibeforge.hydra.net.au/ui/login` with the SA email and the new password
2. **Change the password** via Admin Panel → View SA account → Change Password
3. **Do NOT leave the auto-generated password in place.** It's strong but it lives in your terminal scrollback now and on whatever screen the operator was using.

## Failure modes

- **"No super_admin user found in database"** — The auth migration hasn't been run, OR the SA was deleted. Run `alembic upgrade head` and check the user table. If the SA was deleted, you have a bigger problem — see `bootstrap.md` for first-boot SA creation.
- **`ERROR: ...`** — Database connection failed. Check `.env` has correct `POSTGRES_*` variables and the db container is up. `docker compose ps`.

## Source

- Script: `scripts/reset_sa.sh` (1-line wrapper)
- Worker: `scripts/reset_sa_password.py`
- Both deployed to `/opt/vibeforge/scripts/` on the VM

## See also

- `tools/bootstrap_forgejo.md` — companion bootstrap/reset tool for Forgejo
- `0-MD/0-Documentation/auth-agent.md` — the SA role definition and how it fits the trust axes
- `0-MD/0-Documentation/auth-agent-internal.md` — recovery procedures (confidential)
