# VibeForge+ — install (0.7.0-PRE-RC)

**Personal project, GPLv3, fully vibecoded.** Built by the author for their own AI-paired-programming workflow and released as a curiosity that may be useful to others. A formal threat-model review has not been completed. Suitable for self-hosted small-team or single-operator use behind your own network controls. Not for public-internet exposure without further hardening.

## What you're about to install

A self-managed task board for AI-paired programming. Single host, six containers (`app`, `db`, `caddy`, `health`, `forgejo`, `vaultwarden`). Caddy terminates TLS with a self-signed certificate from its own internal CA. After install you'll have:

- A **board UI** at `https://<your-hostname>/` for the board operator
- An **admin portal** at `https://<your-hostname>/admin/login` for system config
- Two pre-created accounts (Super-Admin + first board user) — passwords printed once at end

Note: `forgejo` and `vaultwarden` containers come up but are **not wired into the board flow** in this release. They're present for future use; safe to leave running, safe to comment out of `docker-compose.yml` before install if you want a leaner stack.

Total install time: roughly 2-5 minutes on a fresh VM.

## Prerequisites

- **Ubuntu 24.04 LTS** (script will warn but allow other distros)
- **Root access** (the script must run as `sudo bash …`)
- **Internet reachable** (the script pulls Docker via the official apt repo + clones / extracts the source)
- A **hostname or LAN address** you've decided this install will live at — e.g. `vibeforge.your-domain.com`, `vibeforge.lan`, or `localhost` for single-machine
- About **5 GB free disk**

## Run

If you have a tarball + the script (recommended for offline / private-source installs):

```bash
sudo VIBEFORGE_SOURCE_TARBALL=/path/to/vibeforge-0.7.0-RC.tar bash vibeforge-install.sh
```

If you can clone from GitHub directly:

```bash
sudo bash vibeforge-install.sh
```

## What the script does

1. Pre-flight (root + Ubuntu version + internet)
2. Installs Docker Engine via the official apt repo (skipped if already present)
3. Extracts source into `/opt/vibeforge`
4. Shows the threat-model warning — you must type `I accept`
5. Asks 11 setup questions (hostname, public URL, SA credentials, first-user credentials)
6. Generates `.env` with random database password and signing key
7. Renders a self-signed Caddyfile
8. Brings the stack up (`docker compose up -d`)
9. Applies database migrations
10. Creates the Super-Admin via the bootstrap API
11. Logs in as SA, creates the first board user
12. Prints a summary block with **all credentials shown once**

## Important: record-keeping

The summary at the end of step 12 prints:

- The two account passwords (also stored hashed in the DB; **unrecoverable** if you lose them)
- The database password (also in `.env` mode 0600, root-readable only)
- The signing key (same)
- The build identifier (quote this in any bug report)

**Copy them into a password manager / secure note before closing the terminal.** The script will not show them again.

## After install — what to do next

1. Browse to `https://<your-hostname>/`
2. Your browser will warn about the self-signed certificate. Either accept the warning, or download the Caddy CA bundle from the admin portal Cert tab and trust it as a root.
3. Log in at `/ui/login` as the operator user. The board is empty by default.
4. Create your first project, then point your AI agent at the API per the agent-onboard prompt the system gives you.

## Read these next

The bundled `docs/` folder contains:

- `what-vibeforge-plus-is.md` — what the system is, what it solves, what it isn't
- `board-model.md` — how the board's entities (projects, tasks, phases, notes) fit together
- `identity-and-membership.md` — User / Agent / SU / SA model + project membership + roles
- `admin-portal-tour.md` — what each admin portal section is for and when to use it
- `operator-verbs.md` — recommended verbs to use when prompting your AI agent

## Updating later

```bash
cd /opt/vibeforge
git pull          # or: sudo tar xf <new-tarball> --strip-components=0 -C /opt/vibeforge
docker compose exec app alembic upgrade head
docker compose restart app
```

Always read the changelog notes that ship with the new release before pulling — schema migrations are forward-only.

## If install fails

The script exits non-zero on any pre-flight failure or hard error, with a `✗` line indicating where. The most common causes:

- Source directory `/opt/vibeforge` already exists (script refuses; move it aside first: `sudo mv /opt/vibeforge /opt/vibeforge.bak.$(date +%s)`)
- App container failed to come healthy in 90 s — check `docker compose logs app`
- First-user creation 422'd — create the user manually from the admin portal at `/admin/portal/administration/users` after install completes

The `Build identifier:` line in the summary is the single value to quote when reporting issues.
