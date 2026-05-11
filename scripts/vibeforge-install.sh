#!/usr/bin/env bash
#
# VibeForge+ first-boot installer for Ubuntu 24.04 LTS
# ─────────────────────────────────────────────────────
#
# Usage (as root):
#   sudo bash vibeforge-install.sh
#
# What it does:
#   1. Pre-flight (root + Ubuntu 24 + internet)
#   2. Installs Docker Engine via the official apt repo (idempotent)
#   3. Clones the 0.7.0-RC branch into /opt/vibeforge
#   4. Surfaces the THREAT-MODEL warning + asks acknowledgement
#   5. Prompts for hostname, intended public URL, SA creds, first-user creds
#   6. Generates .env (random POSTGRES_PASSWORD + SECRET_KEY)
#   7. Renders a self-signed (caddy_internal) Caddyfile
#   8. docker compose up -d
#   9. Waits for app health
#   10. Creates the SA via /api/v2/bootstrap/create-sa
#   11. Logs in as SA + creates the first regular user via /admin/api/users
#   12. Prints a summary the operator should keep (passwords shown ONCE)
#
# Idempotent on re-run for steps 1-3 (Docker install + clone are skipped if
# already present). Steps 4+ assume a fresh /opt/vibeforge tree; running
# against an existing install is NOT supported (use the migration path
# documented in 0.7.0-RC-BASELINE.md instead).
#
# Branch pinned: 0.7.0-RC (the Kelly-handover baseline). To install master
# instead, set VIBEFORGE_BRANCH=master before running.
#
# Source choice (in priority order):
#   1. VIBEFORGE_SOURCE_TARBALL=/path/to/file.tar  (offline / private-repo)
#   2. VIBEFORGE_SOURCE_DIR=/path/to/checkout      (already-extracted tree)
#   3. git clone $VIBEFORGE_REPO @ $VIBEFORGE_BRANCH (default; needs network +
#      repo readability — public repo, or git creds present on the host)

set -euo pipefail

# ── Colour output ────────────────────────────────────────────────────────────
if [ -t 1 ]; then
  RED='\033[1;31m'; YELLOW='\033[1;33m'; GREEN='\033[1;32m'; BLUE='\033[1;34m'; DIM='\033[2m'; RESET='\033[0m'
else
  RED=''; YELLOW=''; GREEN=''; BLUE=''; DIM=''; RESET=''
fi
say()  { printf "%b\n" "${BLUE}▶${RESET} $1"; }
ok()   { printf "%b\n" "${GREEN}✓${RESET} $1"; }
warn() { printf "%b\n" "${YELLOW}⚠${RESET} $1"; }
die()  { printf "%b\n" "${RED}✗ $1${RESET}" >&2; exit 1; }
hr()   { printf "%b\n" "${DIM}────────────────────────────────────────────────────────────────────${RESET}"; }

# ── Defaults (overridable via env) ───────────────────────────────────────────
VIBEFORGE_BRANCH="${VIBEFORGE_BRANCH:-0.7.0-RC}"
VIBEFORGE_REPO="${VIBEFORGE_REPO:-https://github.com/b2dnz99/vibeforge-plus.git}"
VIBEFORGE_DIR="${VIBEFORGE_DIR:-/opt/vibeforge}"

# ── 1. Pre-flight ────────────────────────────────────────────────────────────
hr
say "Pre-flight checks"

[ "$EUID" -eq 0 ] || die "Run as root (use: sudo bash $0)"

if [ -r /etc/os-release ]; then
  . /etc/os-release
  if [ "${ID:-}" != "ubuntu" ] || [ "${VERSION_ID%%.*}" != "24" ]; then
    warn "This script is tested on Ubuntu 24.04 LTS. You appear to be running ${PRETTY_NAME:-unknown}."
    warn "Continue at your own risk. Press Ctrl-C to abort or Enter to proceed."
    read -r _
  else
    ok "Ubuntu 24 LTS detected (${PRETTY_NAME})"
  fi
else
  warn "/etc/os-release not readable — cannot verify Ubuntu version. Continuing."
fi

# Internet check (required for apt + git clone + docker pulls)
if ! curl -sSf -m 5 https://github.com -o /dev/null; then
  die "No internet access — script requires GitHub + Docker Hub reachability."
fi
ok "Internet reachable"

# ── 2. Install Docker (idempotent) ───────────────────────────────────────────
hr
say "Docker Engine"

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  ok "Docker + compose plugin already installed ($(docker --version | head -1))"
else
  say "Installing Docker Engine via official apt repo..."
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
  ok "Docker installed: $(docker --version | head -1)"
fi

# ── 3. Clone the repo (or refuse if dir is non-empty) ───────────────────────
hr
say "Source tree"

if [ -d "$VIBEFORGE_DIR" ]; then
  if [ -n "$(ls -A "$VIBEFORGE_DIR" 2>/dev/null)" ]; then
    die "$VIBEFORGE_DIR already exists and is not empty. Move it aside first (mv $VIBEFORGE_DIR ${VIBEFORGE_DIR}.bak.\$(date +%s)) and re-run."
  fi
fi

mkdir -p "$VIBEFORGE_DIR"

if [ -n "${VIBEFORGE_SOURCE_TARBALL:-}" ]; then
  [ -f "$VIBEFORGE_SOURCE_TARBALL" ] || die "VIBEFORGE_SOURCE_TARBALL=$VIBEFORGE_SOURCE_TARBALL not found"
  say "Source: extracting tarball $VIBEFORGE_SOURCE_TARBALL"
  tar xf "$VIBEFORGE_SOURCE_TARBALL" -C "$VIBEFORGE_DIR" --strip-components=0
  ok "Extracted into $VIBEFORGE_DIR"
elif [ -n "${VIBEFORGE_SOURCE_DIR:-}" ]; then
  [ -d "$VIBEFORGE_SOURCE_DIR" ] || die "VIBEFORGE_SOURCE_DIR=$VIBEFORGE_SOURCE_DIR not found"
  say "Source: copying from $VIBEFORGE_SOURCE_DIR"
  cp -aT "$VIBEFORGE_SOURCE_DIR" "$VIBEFORGE_DIR"
  ok "Copied into $VIBEFORGE_DIR"
else
  apt-get install -y -qq git
  say "Source: cloning $VIBEFORGE_REPO @ $VIBEFORGE_BRANCH"
  git clone --branch "$VIBEFORGE_BRANCH" --depth 1 "$VIBEFORGE_REPO" "$VIBEFORGE_DIR"
  ok "Cloned $VIBEFORGE_REPO @ $VIBEFORGE_BRANCH"
fi

cd "$VIBEFORGE_DIR"
if [ -d .git ]; then
  git_rev="$(git rev-parse --short HEAD 2>/dev/null || echo 'unknown')"
else
  git_rev="(no .git — extracted from tarball)"
fi
ok "Source ready at $VIBEFORGE_DIR (rev: $git_rev)"

# ── 4. Threat-model warning + acknowledgement ────────────────────────────────
hr
say "Threat-model disclosure"
cat <<'EOF'

  ┌──────────────────────────────────────────────────────────────────────┐
  │  ⚠  USE AT YOUR OWN RISK — pre-RC build                              │
  │                                                                      │
  │  VibeForge+ is built to sit behind a TLS-terminating reverse proxy   │
  │  (Caddy is bundled). However, a formal threat-model review has NOT   │
  │  been completed. Known posture:                                      │
  │                                                                      │
  │   • Self-signed cert by default — operators must trust it manually   │
  │   • SA password is set once; no rotation policy enforced             │
  │   • No rate limiting beyond the bootstrap window                     │
  │   • No CSRF tokens on API mutations (cookie-only same-origin)        │
  │   • No content-security-policy headers tuned for prod                │
  │   • Audit log is local-only (no off-host shipping)                   │
  │   • Bootstrap endpoints rate-limited (5/min/IP) but trust the gate   │
  │                                                                      │
  │  Suitable for: small-team self-hosted use, dev/lab installs,         │
  │  Claude-paired workflows behind your own network controls.           │
  │                                                                      │
  │  NOT suitable for: public-internet exposure without further          │
  │  hardening, multi-tenant SaaS, regulated workloads.                  │
  └──────────────────────────────────────────────────────────────────────┘

EOF
read -r -p "Type 'I accept' to continue: " ack
[ "$ack" = "I accept" ] || die "Acknowledgement not given. Aborting."

# ── 5. Prompts ───────────────────────────────────────────────────────────────
hr
say "Setup questions"

prompt() {
  # prompt "Question" "default" -> sets REPLY
  # Note: under `set -e`, a `[ ... ] && cmd` chain that returns false on the
  # test exits the script. Use `if`/`fi` to keep the test side-effect-free.
  local q="$1" def="${2:-}"
  if [ -n "$def" ]; then
    read -r -p "  $q [$def]: " REPLY
    if [ -z "$REPLY" ]; then REPLY="$def"; fi
  else
    read -r -p "  $q: " REPLY
    if [ -z "$REPLY" ]; then die "Required value missing — aborting."; fi
  fi
}

prompt_secret() {
  # prompt_secret "Question" -> sets REPLY (no echo)
  local q="$1"
  read -r -s -p "  $q: " REPLY
  echo
  if [ -z "$REPLY" ]; then die "Required value missing — aborting."; fi
}

prompt "Hostname Caddy will serve on (e.g. vibeforge.example.com or vibeforge.local)" "localhost"
HOSTNAME="$REPLY"

prompt "Intended public URL (informational — what URL will operators + agents use? leave default if same as hostname)" "https://${HOSTNAME}"
PUBLIC_URL="$REPLY"

echo
say "Super-Admin (SA) account — admin portal, system config. Cannot log into the board."
prompt "SA username (short login name)" "sa"
SA_USERNAME="$REPLY"
prompt "SA email (any valid-looking string; .local TLDs accepted per VF-252)" "sa@${HOSTNAME}"
SA_EMAIL="$REPLY"
prompt "SA display name (shown in audit log)" "Super Admin"
SA_DISPLAY="$REPLY"
prompt_secret "SA password (min 12 chars + at least one symbol; shown back at end)"
SA_PASSWORD="$REPLY"
[ "${#SA_PASSWORD}" -ge 12 ] || die "SA password must be at least 12 chars."
echo "$SA_PASSWORD" | grep -q '[^A-Za-z0-9]' || die "SA password must contain at least one symbol (regular-user policy applies)."

echo
say "First USER account — board operator. This is the human who'll actually use the board day-to-day."
prompt "User username" "operator"
U_USERNAME="$REPLY"
prompt "User email" "operator@${HOSTNAME}"
U_EMAIL="$REPLY"
prompt "User display name" "Operator"
U_DISPLAY="$REPLY"
prompt_secret "User password (min 12 chars + at least one symbol; shown back at end)"
U_PASSWORD="$REPLY"
[ "${#U_PASSWORD}" -ge 12 ] || die "User password must be at least 12 chars."
echo "$U_PASSWORD" | grep -q '[^A-Za-z0-9]' || die "User password must contain at least one symbol (regular-user policy applies)."

# ── 6. Generate .env ─────────────────────────────────────────────────────────
hr
say "Writing .env"

POSTGRES_PASSWORD="$(openssl rand -base64 24 | tr -d '\n=' | head -c 32)"
SECRET_KEY="$(openssl rand -hex 32)"

cat > "$VIBEFORGE_DIR/.env" <<EOF
# Generated by vibeforge-install.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)
# DO NOT commit this file. Rotate secrets via .env edit + container restart.

VIBEFORGE_HOSTNAME=${HOSTNAME}
SECRET_KEY=${SECRET_KEY}
APP_ENV=production

POSTGRES_DB=vibeforge
POSTGRES_USER=vibeforge
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
DATABASE_URL=postgresql+psycopg2://vibeforge:${POSTGRES_PASSWORD}@db:5432/vibeforge

# Vaultwarden token kept for legacy compose service (if enabled). Random; not used by app.
VAULTWARDEN_ADMIN_TOKEN=$(openssl rand -hex 32)
EOF
chmod 600 "$VIBEFORGE_DIR/.env"
ok ".env written (mode 0600)"

# ── 7. Render self-signed Caddyfile (caddy_internal mode) ────────────────────
hr
say "Caddy config (self-signed via internal CA)"

mkdir -p "$VIBEFORGE_DIR/ops/caddy" "$VIBEFORGE_DIR/ops/certs"
cat > "$VIBEFORGE_DIR/ops/caddy/Caddyfile" <<'EOF'
# Generated by vibeforge-install.sh — caddy_internal mode (self-signed).
# Caddy mints + rotates a leaf cert from its own internal CA. The root cert
# lives in the caddy_data volume at /data/caddy/pki/authorities/local/root.crt
# and can be downloaded for device-trust install via the admin portal Cert tab.
#
# To switch to file mode (operator-supplied cert) or ACME (Let's Encrypt), use
# the cert wizard at /admin/portal/configuration/certificates.

{
    # Admin API on the docker-internal network so the health container can
    # poll Caddy's Prometheus metrics endpoint at http://caddy:2019/metrics.
    # Compose isolates this network — port 2019 is NOT exposed to the host
    # outside the bundled stack (see docker-compose.yml port mapping).
    admin 0.0.0.0:2019

    # Expose Prometheus metrics on /metrics (consumed by the health container
    # for the system-health dashboard). Without this, the dashboard reports
    # Caddy as "error" even though it's serving requests fine.
    servers {
        metrics
    }

    log {
        output stdout
        format console
        level INFO
    }
}

{$VIBEFORGE_HOSTNAME}:443 {
    tls internal

    header {
        Strict-Transport-Security "max-age=31536000"
        X-Content-Type-Options    "nosniff"
        X-Frame-Options           "SAMEORIGIN"
        Referrer-Policy           "no-referrer-when-downgrade"
    }

    handle /api/v2/projects/* {
        reverse_proxy app:8000 {
            flush_interval -1
            transport http {
                read_timeout 300s
            }
        }
    }

    handle /health-dashboard/* {
        uri strip_prefix /health-dashboard
        reverse_proxy health:9090
    }

    handle /api/health/* {
        reverse_proxy health:9090
    }

    handle /git/* {
        uri strip_prefix /git
        reverse_proxy forgejo:3000 {
            transport http {
                read_timeout  600s
                write_timeout 600s
            }
        }
    }

    handle /vault/* {
        reverse_proxy vaultwarden:80
    }

    handle {
        reverse_proxy app:8000
    }
}
EOF
ok "Caddyfile written (caddy_internal / self-signed)"

# ── 8. docker compose up ────────────────────────────────────────────────────
hr
say "Starting stack (docker compose up -d)"

cd "$VIBEFORGE_DIR"
docker compose up -d

# ── 9. Wait for app health ──────────────────────────────────────────────────
# Probe via python (guaranteed inside the app container; curl is not).
say "Waiting for app to come healthy..."
HEALTH_PROBE='import urllib.request,sys
try: urllib.request.urlopen("http://localhost:8000/api/v2/health", timeout=2).read(); sys.exit(0)
except Exception: sys.exit(1)'
deadline=$(( $(date +%s) + 90 ))
healthy=0
while [ "$(date +%s)" -lt "$deadline" ]; do
  if docker compose exec -T app python -c "$HEALTH_PROBE" 2>/dev/null; then
    healthy=1; break
  fi
  sleep 2
done
if [ "$healthy" -ne 1 ]; then
  die "App failed to come healthy in 90s. Check 'docker compose logs app'."
fi
ok "App is responding"

# Apply alembic migrations (alembic head reflected in BUILD_TAG fallback).
say "Applying alembic migrations..."
docker compose exec -T app alembic upgrade head 2>&1 | tail -5
ok "Alembic at head"

# ── 10. Create SA ───────────────────────────────────────────────────────────
hr
say "Creating Super-Admin"

# Use the localhost path INSIDE the app container so we hit the bootstrap
# endpoint without going through Caddy's TLS (avoids self-signed-trust issues
# during install). Bootstrap is gated by install_open(db).
sa_resp=$(docker compose exec -T app python -c "
import json, urllib.request, urllib.error
body = json.dumps({
  'email': '${SA_EMAIL}',
  'display_name': '${SA_DISPLAY}',
  'password': '${SA_PASSWORD}',
}).encode()
req = urllib.request.Request('http://localhost:8000/api/v2/bootstrap/create-sa',
    data=body,
    headers={'Content-Type': 'application/json'},
    method='POST')
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        print(r.read().decode())
except urllib.error.HTTPError as e:
    print('HTTP_ERROR', e.code, e.read().decode())
" 2>&1)

if echo "$sa_resp" | grep -q "HTTP_ERROR"; then
  die "SA creation failed: $sa_resp"
fi
ok "SA created (email: $SA_EMAIL)"

# Set SA username if it differs from default — bootstrap currently uses email-derived username.
# Skipping for now; SA login in the portal accepts the email as identifier.

# ── 11. Login as SA + create first user ─────────────────────────────────────
hr
say "Creating first user (board operator)"

# Login as SA via /admin/login to get the SA session cookie.
sa_cookie=$(docker compose exec -T app python -c "
import json, urllib.request, http.cookiejar, urllib.error
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
body = json.dumps({'username': '${SA_USERNAME}', 'password': '${SA_PASSWORD}'}).encode()
# Try username first; bootstrap may have used the email as username.
for uname in ('${SA_USERNAME}', '${SA_EMAIL}'):
    body = json.dumps({'username': uname, 'password': '${SA_PASSWORD}'}).encode()
    req = urllib.request.Request('http://localhost:8000/admin/login',
        data=body, headers={'Content-Type': 'application/json'}, method='POST')
    try:
        opener.open(req, timeout=10).read()
        break
    except urllib.error.HTTPError as e:
        last = (e.code, e.read().decode()[:120])
        continue
else:
    print('LOGIN_FAILED', last)
    raise SystemExit(1)
for c in jar:
    if c.name == 'vf_sa_session':
        print(c.value)
        break
else:
    print('NO_SESSION_COOKIE')
" 2>&1 | tail -1)

if [ "$sa_cookie" = "NO_SESSION_COOKIE" ] || echo "$sa_cookie" | grep -q "LOGIN_FAILED"; then
  warn "SA login failed: $sa_cookie"
  warn "First-user creation skipped. You can create the user manually from the admin portal."
else
  user_resp=$(docker compose exec -T app python -c "
import json, urllib.request, urllib.error
body = json.dumps({
  'username': '${U_USERNAME}',
  'email': '${U_EMAIL}',
  'display_name': '${U_DISPLAY}',
  'password': '${U_PASSWORD}',
  'role': 'super_user',
}).encode()
req = urllib.request.Request('http://localhost:8000/admin/api/users',
    data=body,
    headers={'Content-Type': 'application/json', 'Cookie': 'vf_sa_session=${sa_cookie}'},
    method='POST')
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        print(r.read().decode())
except urllib.error.HTTPError as e:
    print('HTTP_ERROR', e.code, e.read().decode())
" 2>&1)
  if echo "$user_resp" | grep -q "HTTP_ERROR"; then
    warn "First-user creation failed: $user_resp"
    warn "Create the user manually from /admin/portal/administration/users."
  else
    ok "First user created (username: $U_USERNAME, role: super_user)"
  fi
fi

# ── 12. Summary ─────────────────────────────────────────────────────────────
hr
cat <<EOF

  ${GREEN}╔══════════════════════════════════════════════════════════════════════╗${RESET}
  ${GREEN}║  VibeForge+ install complete                                         ║${RESET}
  ${GREEN}╚══════════════════════════════════════════════════════════════════════╝${RESET}

  ${BLUE}Branch:${RESET}            $VIBEFORGE_BRANCH @ $git_rev
  ${BLUE}Install dir:${RESET}       $VIBEFORGE_DIR
  ${BLUE}Hostname:${RESET}          $HOSTNAME
  ${BLUE}Intended URL:${RESET}      $PUBLIC_URL
  ${BLUE}Cert mode:${RESET}         caddy_internal (self-signed; trust manually or
                     download CA bundle from the admin portal Cert tab)

  ${RED}╔══════════════════════════════════════════════════════════════════════╗${RESET}
  ${RED}║  ⚠  RECORD THESE NOW — they are shown ONCE and not retrievable      ║${RESET}
  ${RED}║     after this terminal closes. The .env file holds the DB password ║${RESET}
  ${RED}║     and SECRET_KEY at mode 0600 (root-only). The user passwords are ║${RESET}
  ${RED}║     hashed in the DB and unrecoverable. Copy + store in a password  ║${RESET}
  ${RED}║     manager / Vaultwarden / sealed envelope BEFORE you continue.    ║${RESET}
  ${RED}╚══════════════════════════════════════════════════════════════════════╝${RESET}

  ${BLUE}Super-Admin (SA):${RESET}
     username:     $SA_USERNAME
     email:        $SA_EMAIL
     display:      $SA_DISPLAY
     password:     ${YELLOW}$SA_PASSWORD${RESET}
     log in at:    https://${HOSTNAME}/admin/login

  ${BLUE}First user (operator):${RESET}
     username:     $U_USERNAME
     email:        $U_EMAIL
     display:      $U_DISPLAY
     password:     ${YELLOW}$U_PASSWORD${RESET}
     log in at:    https://${HOSTNAME}/ui/login

  ${BLUE}Database password:${RESET}      ${YELLOW}$POSTGRES_PASSWORD${RESET}
                          ${DIM}(also in .env as POSTGRES_PASSWORD; needed for pg_dump,
                          psql restore, or any out-of-container DB access)${RESET}

  ${BLUE}SECRET_KEY:${RESET}             ${YELLOW}$SECRET_KEY${RESET}
                          ${DIM}(also in .env; signs session cookies. Rotating invalidates
                          all logged-in sessions — operator chooses if/when.)${RESET}

  ${BLUE}Build identifier:${RESET}     $VIBEFORGE_BRANCH @ $git_rev
                          ${DIM}(quote this when reporting issues)${RESET}

  ${YELLOW}═══ NEXT STEPS ═══${RESET}

  1. Browse to ${BLUE}https://${HOSTNAME}/${RESET}
     ${DIM}Your browser will warn about the self-signed cert. Accept the warning,
     or download the Caddy CA bundle from the admin portal Cert tab and install
     it as a trusted root on your devices.${RESET}

  2. Log in as the operator user. The board is empty by default — create your
     first project.

  3. The admin portal lives at ${BLUE}https://${HOSTNAME}/admin/login${RESET} for SA work
     (system config, user management, cert wizard, etc.).

  4. ${YELLOW}Re-read the threat-model warning above.${RESET} If exposing this install
     beyond your local network, harden the surface (real cert, CSP headers,
     reverse proxy with WAF, etc.) before doing so.

  5. To update later: \`cd $VIBEFORGE_DIR && git pull && docker compose exec
     app alembic upgrade head && docker compose restart app\`. Always read the
     CHANGELOG before pulling — schema migrations are forward-only.

  ${DIM}Generated by vibeforge-install.sh — bug reports / questions to PK.${RESET}

EOF
hr
ok "Done."
