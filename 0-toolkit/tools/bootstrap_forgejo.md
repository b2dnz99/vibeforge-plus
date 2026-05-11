---
title: bootstrap_forgejo — Forgejo Bootstrap & Recovery
audience: rescue
ip: none
style: practical
status: Active
last_updated: 2026-04-08
tool_script: scripts/bootstrap_forgejo.sh
tool_target: Forgejo (board git engine)
---

# `bootstrap_forgejo` — Forgejo bootstrap and recovery

**When to use:** fresh VibeForge+ install, lost Forgejo admin password, need to issue a new service token, or just want to verify Forgejo is wired correctly.

**What it does:** automates Forgejo's first-run setup so the operator never has to open the web wizard. Creates an admin user, creates a service account, issues a long-lived API token for the service account, parks all credentials in a chmod-600 JSON file. Subcommands cover routine recovery without re-bootstrapping from scratch.

**Tier:** `audience: rescue` — operational tool, read in emergencies or first-install setup.

## Quick reference

```bash
# From the VM (always run with sudo so the park file gets root-owned 600 perms):
sudo /opt/vibeforge/scripts/bootstrap_forgejo.sh <subcommand>

# From host:
ssh vibeforge "sudo /opt/vibeforge/scripts/bootstrap_forgejo.sh <subcommand>"
```

| Subcommand | What it does | Idempotent? |
|---|---|---|
| `bootstrap` | Fresh install — creates admin + service accounts, issues token | **No** — refuses if park file exists |
| `reset-admin` | Regenerate admin password (in Forgejo + park file) | Yes |
| `reset-service-token` | Issue new API token for service account | Yes (old tokens stay valid until manually revoked) |
| `verify` | Read-only: health + admin auth + service token round-trip | Yes (read-only) |

## What it touches

- **Forgejo container:** runs `forgejo admin user create` via `docker compose exec --user git`
- **Forgejo API:** posts to `/api/v1/admin/users` and `/api/v1/users/{user}/tokens` over HTTPS
- **Park file:** `/opt/vibeforge/.bootstrap/forgejo.json` — created with `chmod 600`, root-owned
- **TLS:** **skips verification** because the script talks to our own Forgejo on the same machine. Documented in the script source as a deliberate trust-boundary decision.

## The park file

```json
{
  "admin_username":   "vibeforge-admin",
  "admin_password":   "...",
  "admin_email":      "admin@vibeforge.local",
  "service_username": "vibeforge-service",
  "service_password": "...",
  "service_token":    "...",
  "forgejo_url":      "https://vibeforge.hydra.net.au/git",
  "generated_at":     "2026-04-08T...",
  "last_verified":    "..."
}
```

**This file is the source of truth for Forgejo credentials on the VM.** Until Vaultwarden migration lands (see `proposed/INSTALL-RECOVERY-TOOLKIT-PROPOSAL.md`), it lives on the filesystem with restrictive permissions. Anyone with root on the VM can read it — same trust boundary as the rest of `/opt/vibeforge/`.

To read it:
```bash
sudo cat /opt/vibeforge/.bootstrap/forgejo.json
```

## What `bootstrap` does, step by step

1. **Refuses if park file exists.** Idempotency safeguard. To re-bootstrap, delete the park file first.
2. **Health check** — GETs `/git/api/healthz`, fails if Forgejo isn't responding
3. **Creates admin user** via `forgejo admin user create` inside the container with a random 24-char password
4. **Creates service account** via Forgejo's admin API using the freshly-created admin
5. **Issues API token** for the service account via `/api/v1/users/{user}/tokens`
6. **Writes park file** with all credentials, sets chmod 600

## What `reset-admin` does

Reads the existing park file, generates a new password, runs `forgejo admin user change-password` inside the container, updates the park file. Idempotent — can be run any time.

## What `reset-service-token` does

Reads the park file, posts to `/api/v1/users/{user}/tokens` to issue a *new* token (named with timestamp so it's distinct from the old one), updates the park file with the new token. **Old tokens stay valid until manually revoked via the Forgejo UI.** This is by design — it lets downstream services pick up the new token before the old one is killed, avoiding outages.

To revoke old tokens after rotation:
1. Log in to Forgejo as `vibeforge-service` (password in park file)
2. Settings → Applications → revoke any tokens you don't recognise

## What `verify` does

Three-step round-trip with no mutations:

1. `GET /git/api/healthz` → expects 200
2. `GET /api/v1/user` with admin basic auth → expects 200, confirms admin still works
3. `GET /api/v1/user` with service token basic auth → expects 200, confirms token still works

If any step fails, prints which one and exits non-zero. On success, updates `last_verified` in the park file.

## Failure modes

- **"Forgejo is not responding (HTTP 0)"** — TLS verification failed (script uses unverified context already, so this means a real network issue) OR Forgejo container is down. Check `docker ps | grep forgejo`.
- **"FAILED. CLI output: ...Forgejo is not supposed to be run as root"** — The script is correctly using `--user git` but if you see this, the container was misconfigured. Should not happen with the shipped docker-compose.
- **"Unable to load config file for a installed Forgejo instance"** — Forgejo's app.ini exists but `INSTALL_LOCK` is `false`. Forgejo's first-run wizard wasn't completed. Fix:
  ```bash
  ssh vibeforge "docker exec --user git vibeforge-forgejo-1 sed -i 's/INSTALL_LOCK = false/INSTALL_LOCK = true/' /data/gitea/conf/app.ini"
  ssh vibeforge "docker compose -f /opt/vibeforge/docker-compose.yml restart forgejo"
  ```
- **"A bootstrap record already exists"** — Idempotency safeguard. Delete `/opt/vibeforge/.bootstrap/forgejo.json` and re-run.

## Source

- Wrapper: `scripts/bootstrap_forgejo.sh`
- Worker: `scripts/bootstrap_forgejo.py`
- Both deployed to `/opt/vibeforge/scripts/` on the VM

## Future migration to Vaultwarden

Once the Vaultwarden bootstrap and the install/recovery toolkit migration script ship, the park file at `/opt/vibeforge/.bootstrap/forgejo.json` will be migrated into a Vaultwarden vault entry under the SA's master password, and the file will be wiped. Until then, the filesystem park is the source of truth.

## See also

- `tools/reset_sa.md` — companion break-glass tool for the VibeForge+ Super Admin
- `0-MD/proposed/INSTALL-RECOVERY-TOOLKIT-PROPOSAL.md` — the broader pattern this is the first instance of
- `0-MD/proposed/SYNC-ARCHITECTURE-PROPOSAL.md` (GUESS) — the gate this Forgejo instance will eventually host
