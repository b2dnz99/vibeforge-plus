# VibeForge+ install on macOS (Apple Silicon)

Notes from a working install on macOS 25.4 (Apple Silicon) on 2026-05-11. The
upstream `INSTALL.md` is Ubuntu-only; the upstream install script (`scripts/vibeforge-install.sh`)
is also Ubuntu-only (uses `apt`, expects root, requires `/etc/os-release` to
say `ID=ubuntu` and `VERSION_ID=24.x`).

The path that worked: run the official install script unchanged, but inside an
Ubuntu 24.04 VM managed by **OrbStack**.

## Prereqs

- macOS on Apple Silicon (M1/M2/M3/M4)
- Homebrew

## Steps

### 1. Install OrbStack

OrbStack is a polished Apple-native VM/container manager. We tried Multipass
first — it failed on Apple Silicon because the bundled QEMU does not know about
the host's `host-arm-cpu.sme` property and refuses to launch a VM. OrbStack uses
Apple's native virtualization framework instead and does not have this problem.

```bash
brew install --cask orbstack
```

The cask installs the OrbStack app to `/Applications` and links the `orb` and
`orbctl` CLIs into `/opt/homebrew/bin`. No admin password required.

### 2. Launch an Ubuntu 24.04 VM

```bash
orbctl create ubuntu:24.04 vibeforge
```

This creates a VM named `vibeforge` running Ubuntu 24.04 (Noble) on arm64. It
inherits OrbStack's defaults for CPU/RAM/disk, which are enough for VibeForge+'s
six containers. Verify:

```bash
orbctl list
orbctl info vibeforge
```

OrbStack auto-publishes a hostname `<vm-name>.orb.local` resolvable from the
Mac, so the VM is reachable at `vibeforge.orb.local`. Use this as the `Hostname`
answer when the install script prompts.

OrbStack also auto-mounts the Mac's `/Users` tree into the VM, so files under
`/Users/<you>/...` on the Mac appear at the same path inside the VM. We use
this to point the install script at our local clone via `VIBEFORGE_SOURCE_DIR`.

### 3. Patch `health/Dockerfile` for arm64

The `health` container's Dockerfile pins `psutil==5.9.8`. There is no arm64
manylinux wheel for that version on Python 3.12, so pip falls back to building
from source — and `python:3.12-slim` does not ship `gcc`. The Docker build
fails at `health 4/5 RUN pip install` with `error: command 'gcc' failed: No
such file or directory`.

Add a build-deps step in `health/Dockerfile` before the `pip install`:

```dockerfile
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc python3-dev \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
```

(Bumping `psutil` to a 6.x or 7.x release would also fix this, but adding the
build deps is more conservative and keeps the project's pinned versions
intact.)

### 4. Use a different email for SA and operator

The interactive install asks for an email for both the Super-Admin and the
first operator user. The backend enforces unique emails on insert, so giving
the same address to both fails the operator creation with HTTP 409 "Email
already registered" — leaving the SA created and the operator missing.

Either:

- give different addresses during the install (SA: `sa@vibeforge.local`,
  operator: your real email), or
- accept the failure and create the operator manually after install via
  the admin portal at `/admin/portal/administration/users`

### 5. Run the install script inside the VM

The official script asks 11 prompts. The simplest non-interactive path is to
write the answers to a file (under `/Users/<you>/`, since OrbStack auto-mounts
that path into the VM) and pipe it as stdin. Generate two strong passwords
first (12+ chars, must contain a symbol). Example using random alphanumerics
plus an `@7` suffix:

```bash
SA_PWD=$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 22)@7
USER_PWD=$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 22)@7

ANSFILE=/Users/$(whoami)/.vibeforge-install-answers.tmp
printf 'I accept\nvibeforge.orb.local\n\n\nsa@vibeforge.local\n\n%s\n\noperator@vibeforge.local\n\n%s\n' \
    "$SA_PWD" "$USER_PWD" > "$ANSFILE"
chmod 600 "$ANSFILE"

orb -m vibeforge -u root bash -c \
    "VIBEFORGE_SOURCE_DIR=/Users/$(whoami)/DevProjects/vibeforge-plus bash /Users/$(whoami)/DevProjects/vibeforge-plus/scripts/vibeforge-install.sh < $ANSFILE"

rm -f "$ANSFILE"
```

The 11 lines in the printf correspond to the script's 11 prompts in order:

1. `I accept` — threat-model acknowledgement
2. Hostname — `vibeforge.orb.local`
3. Public URL — empty (defaults to `https://vibeforge.orb.local`)
4. SA username — empty (default `sa`)
5. SA email
6. SA display name — empty (default `Super Admin`)
7. SA password
8. Operator username — empty (default `operator`)
9. Operator email — must differ from SA email
10. Operator display name — empty (default `Operator`)
11. Operator password

Setting `VIBEFORGE_SOURCE_DIR` makes the script `cp -aT` from the local clone
instead of trying to `git clone` the (non-existent at the time of writing)
`0.7.0-RC` upstream branch. Use whatever path your local clone actually lives
at — the example assumes `~/DevProjects/vibeforge-plus`.

The script will:

- install Docker Engine inside the VM via the official apt repo
- copy source from the `VIBEFORGE_SOURCE_DIR` to `/opt/vibeforge`
- generate `/opt/vibeforge/.env` with random `POSTGRES_PASSWORD` and
  `SECRET_KEY`
- render `/opt/vibeforge/ops/caddy/Caddyfile`
- bring the six containers up via `docker compose`
- run `alembic upgrade head`
- bootstrap the SA via the API, then the operator (which will 409 if you used
  the same email twice)
- print credentials in a summary block

Save the credentials block somewhere (1Password / sealed note). The user
passwords are hashed in the DB and unrecoverable; the DB password and
`SECRET_KEY` are also stored at `/opt/vibeforge/.env` mode 0600 inside the VM.

### 6. Verify

```bash
curl -sk -o /dev/null -w "HTTP %{http_code}\n" https://vibeforge.orb.local/
orb -m vibeforge -u root docker compose -f /opt/vibeforge/docker-compose.yml ps
```

You should see HTTP 302 (redirect to login) and six containers running:
`app`, `caddy`, `db` (healthy), `forgejo` (healthy), `health`, `vaultwarden`.

Then in a browser:

- Board UI: <https://vibeforge.orb.local/ui/login>
- Admin portal: <https://vibeforge.orb.local/admin/login>

The browser will warn about the self-signed certificate — accept the warning,
or download the Caddy CA bundle from the admin portal Cert tab and add it to
the macOS keychain as a trusted root.

## What did not work (for reference)

- **Multipass** — bundled QEMU on Apple Silicon errors out with
  `Property 'host-arm-cpu.sme' not found` for any guest, regardless of Ubuntu
  version. Known issue, no workaround in 1.16.x. If you already installed
  Multipass to try this, `brew uninstall --cask multipass` cleans up.
- **Docker Desktop on the Mac directly** — `docker-compose.yml` hard-codes
  `/opt/vibeforge/.bootstrap` as a host mount and the `health` container
  mounts `/etc/crontab` + `/var/spool/cron/crontabs`, both of which are
  Linux-specific paths that don't exist on macOS. Going off the supported
  install path on a pre-RC product means a lot of patches and a lot of
  unknown breakage.
- **Letting the install script clone its own source** — the script defaults
  to branch `0.7.0-RC` which doesn't exist on the upstream repo (only `main`
  and the tag `v0.7.0-pre-rc`). Use `VIBEFORGE_SOURCE_DIR=...` to skip the
  clone, or set `VIBEFORGE_BRANCH=main`.

## Tearing it down

```bash
orbctl stop vibeforge
orbctl delete vibeforge
```

If you also want to remove OrbStack itself:

```bash
brew uninstall --cask orbstack
```

## Updating later

Inside the VM, the source lives at `/opt/vibeforge`. To pull updates from a
new local clone, repeat the `cp -aT` step or follow the upstream
`INSTALL.md` "Updating later" section. Schema migrations are forward-only.
