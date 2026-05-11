#!/usr/bin/env python3
"""
VibeForge+ Forgejo Bootstrap & Recovery Toolkit (VF-232)

Subcommands:
  bootstrap              Fresh install — creates admin + service accounts, issues API token
  reset-admin            Regenerate the admin user's password
  reset-service-token    Issue a new API token for the service account
  verify                 Read-only health + auth round-trip check

Run inside /opt/vibeforge on the VM (or invoke via bootstrap_forgejo.sh wrapper).

Park location:
  /opt/vibeforge/.bootstrap/forgejo.json  (chmod 600, root-owned)

Park file shape:
  {
    "admin_username":   "vibeforge-admin",
    "admin_password":   "...",
    "admin_email":      "admin@vibeforge.local",
    "service_username": "vibeforge-service",
    "service_password": "...",
    "service_token":    "...",
    "forgejo_url":      "https://<your-hostname>/git",
    "generated_at":     "2026-04-08T...",
    "last_verified":    "..."
  }

This is the FIRST INSTANCE of the bootstrap pattern that the install/recovery
toolkit proposal documents. Same shape as scripts/reset_sa_password.py:
visible banner, generated random secrets, big "CHANGE THIS" warning where
applicable. Idempotent for read-only ops; refuses to clobber for destructive
ones unless explicitly invoked.
"""
import json
import os
import secrets
import ssl
import string
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# WHY: This script runs on the VM host and talks to OUR OWN Forgejo via the
# public URL (https://<hostname>/git). The host's Python may not trust the
# wildcard CA chain in its system store, but we know we're talking to our own
# service on the same machine. Skip TLS verification — same trust boundary as
# `docker compose exec` against our own containers.
SSL_CTX = ssl._create_unverified_context()

# Derive Forgejo URL from hostname — never hardcode a specific domain.
import socket as _socket
_hostname = os.environ.get("VIBEFORGE_HOSTNAME", _socket.getfqdn())
FORGEJO_URL = f"https://{_hostname}/git"
FORGEJO_INTERNAL = "http://forgejo:3000"  # used when running inside the docker network (e.g. from app container)

# Auto-detect: if /.dockerenv exists we're inside a container, prefer the internal URL
if os.path.exists("/.dockerenv"):
    FORGEJO_URL = FORGEJO_INTERNAL

PARK_DIR = Path("/opt/vibeforge/.bootstrap")
PARK_FILE = PARK_DIR / "forgejo.json"

ADMIN_USER = "vibeforge-admin"
SERVICE_USER = "vibeforge-service"
ADMIN_EMAIL = "admin@vibeforge.local"
SERVICE_EMAIL = "service@vibeforge.local"


def banner(title: str, char: str = "=") -> None:
    print()
    print(char * 70)
    print(f"  {title}")
    print(char * 70)
    print()


def gen_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def park_read() -> dict:
    if not PARK_FILE.exists():
        return {}
    return json.loads(PARK_FILE.read_text())


def park_write(data: dict) -> None:
    PARK_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    PARK_FILE.write_text(json.dumps(data, indent=2))
    os.chmod(PARK_FILE, 0o600)


def http_json(method: str, url: str, body: dict | None = None,
              auth: tuple[str, str] | None = None) -> tuple[int, dict | None]:
    """Tiny JSON HTTP client. Returns (status, body_dict_or_none)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if auth:
        import base64
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read())
        except Exception:
            err_body = None
        return e.code, err_body
    except Exception as e:
        return 0, {"error": str(e)}


def forgejo_admin_create_user(admin_auth: tuple[str, str], username: str,
                              password: str, email: str, must_change: bool = False) -> tuple[int, dict | None]:
    return http_json(
        "POST",
        f"{FORGEJO_URL}/api/v1/admin/users",
        body={
            "username": username,
            "password": password,
            "email": email,
            "must_change_password": must_change,
            "source_id": 0,
        },
        auth=admin_auth,
    )


def forgejo_create_token(auth: tuple[str, str], username: str, name: str) -> tuple[int, dict | None]:
    return http_json(
        "POST",
        f"{FORGEJO_URL}/api/v1/users/{username}/tokens",
        body={
            "name": name,
            "scopes": ["all"],  # broad scope for service account; v1 simplification
        },
        auth=auth,
    )


def forgejo_health() -> tuple[int, dict | None]:
    return http_json("GET", f"{FORGEJO_URL}/api/healthz")


def forgejo_authed_user(auth: tuple[str, str]) -> tuple[int, dict | None]:
    return http_json("GET", f"{FORGEJO_URL}/api/v1/user", auth=auth)


# ─────────────────────────────────────────────────────────────────────
# Subcommands
# ─────────────────────────────────────────────────────────────────────

def cmd_bootstrap() -> int:
    """Fresh install. Creates admin + service accounts, issues service token, parks credentials."""
    if PARK_FILE.exists():
        existing = park_read()
        banner("Forgejo bootstrap REFUSED", char="!")
        print(f"  A bootstrap record already exists at {PARK_FILE}")
        print(f"  Generated: {existing.get('generated_at', 'unknown')}")
        print()
        print("  To re-bootstrap, delete the park file and re-run:")
        print(f"    sudo rm {PARK_FILE}")
        print()
        print("  Or use one of the recovery subcommands:")
        print("    bootstrap_forgejo.sh reset-admin")
        print("    bootstrap_forgejo.sh reset-service-token")
        print("    bootstrap_forgejo.sh verify")
        print()
        return 1

    banner("Forgejo bootstrap — fresh install")
    print(f"  Target: {FORGEJO_URL}")
    print()

    # Step 1 — health check
    print("  [1/5] Health check...")
    status, body = forgejo_health()
    if status != 200:
        print(f"  FAILED. Forgejo is not responding (HTTP {status}). Is the container up?")
        return 2
    print(f"        OK ({status})")

    # Step 2 — create admin via Forgejo's install endpoint OR via direct DB seed
    # Forgejo's first-run install requires the web UI walkthrough, but we can
    # bypass it via the `forgejo admin user create` CLI inside the container.
    print("  [2/5] Creating admin user via container CLI...")
    admin_password = gen_password()
    cli_create_admin = (
        f'docker compose exec -T --user git forgejo forgejo admin user create '
        f'--username {ADMIN_USER} --password "{admin_password}" '
        f'--email {ADMIN_EMAIL} --admin --must-change-password=false'
    )
    rc = os.system(f"cd /opt/vibeforge && {cli_create_admin} > /tmp/forgejo_admin.log 2>&1")
    if rc != 0:
        with open("/tmp/forgejo_admin.log") as f:
            print("  FAILED. CLI output:")
            print("  " + f.read().replace("\n", "\n  "))
        return 3
    print(f"        OK — admin user '{ADMIN_USER}' created")

    # Step 3 — create service account via API using the admin we just made
    print("  [3/5] Creating service account via API...")
    service_password = gen_password()
    status, body = forgejo_admin_create_user(
        (ADMIN_USER, admin_password),
        SERVICE_USER, service_password, SERVICE_EMAIL,
        must_change=False,
    )
    if status not in (200, 201):
        print(f"  FAILED. API returned {status}: {body}")
        return 4
    print(f"        OK — service user '{SERVICE_USER}' created")

    # Step 4 — issue API token for the service account
    print("  [4/5] Issuing API token for service account...")
    status, body = forgejo_create_token(
        (SERVICE_USER, service_password),
        SERVICE_USER, "vibeforge-bootstrap"
    )
    if status not in (200, 201):
        print(f"  FAILED. API returned {status}: {body}")
        return 5
    service_token = body.get("sha1")
    print("        OK — token issued")

    # Step 5 — park credentials
    print("  [5/5] Parking credentials...")
    now = datetime.now(timezone.utc).isoformat()
    park = {
        "admin_username": ADMIN_USER,
        "admin_password": admin_password,
        "admin_email": ADMIN_EMAIL,
        "service_username": SERVICE_USER,
        "service_password": service_password,
        "service_token": service_token,
        "forgejo_url": FORGEJO_URL,
        "generated_at": now,
        "last_verified": now,
    }
    park_write(park)
    print(f"        OK — parked at {PARK_FILE} (chmod 600)")

    banner("Bootstrap complete")
    print(f"  Admin user:       {ADMIN_USER}")
    print(f"  Admin password:   {admin_password}")
    print(f"  Service user:     {SERVICE_USER}")
    print(f"  Service token:    {service_token[:8]}... (full token in park file)")
    print()
    print(f"  Park file:        {PARK_FILE}")
    print(f"  Web UI:           {FORGEJO_URL}/")
    print()
    print("  NEXT STEPS:")
    print("  1. Log in to the Forgejo web UI as the admin user above")
    print("  2. CHANGE THE ADMIN PASSWORD if you intend to log in regularly")
    print("  3. The service token will be used by VF-237 (repo provisioning)")
    print()
    print("  Park file contents are also available via:")
    print(f"    sudo cat {PARK_FILE}")
    print()
    return 0


def cmd_reset_admin() -> int:
    park = park_read()
    if not park:
        print("ERROR: No bootstrap record found. Run 'bootstrap' first.")
        return 1
    banner("Forgejo admin password reset")
    new_password = gen_password()
    cli = (
        f'docker compose exec -T --user git forgejo forgejo admin user change-password '
        f'--username {park["admin_username"]} --password "{new_password}" --must-change-password=false'
    )
    rc = os.system(f"cd /opt/vibeforge && {cli} > /tmp/forgejo_reset.log 2>&1")
    if rc != 0:
        with open("/tmp/forgejo_reset.log") as f:
            print("FAILED. CLI output:")
            print(f.read())
        return 2
    park["admin_password"] = new_password
    park["last_verified"] = datetime.now(timezone.utc).isoformat()
    park_write(park)
    print(f"  Admin user:       {park['admin_username']}")
    print(f"  New password:     {new_password}")
    print(f"  Park file updated: {PARK_FILE}")
    print()
    print("  CHANGE THIS PASSWORD AFTER NEXT LOGIN if you log in interactively.")
    return 0


def cmd_reset_service_token() -> int:
    park = park_read()
    if not park:
        print("ERROR: No bootstrap record found. Run 'bootstrap' first.")
        return 1
    banner("Forgejo service token reset")
    # Issue a new token, named with timestamp so old ones are easy to find/revoke later
    name = f"vibeforge-svc-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    status, body = forgejo_create_token(
        (park["service_username"], park["service_password"]),
        park["service_username"], name
    )
    if status not in (200, 201):
        print(f"FAILED. API returned {status}: {body}")
        return 2
    new_token = body.get("sha1")
    park["service_token"] = new_token
    park["last_verified"] = datetime.now(timezone.utc).isoformat()
    park_write(park)
    print(f"  Service user:     {park['service_username']}")
    print(f"  Token name:       {name}")
    print(f"  New token:        {new_token[:8]}... (full token in park file)")
    print()
    print("  Old service tokens remain valid until manually revoked via Forgejo UI.")
    print("  Consider revoking them once downstream services pick up the new one.")
    return 0


def cmd_verify() -> int:
    banner("Forgejo verify — read-only health + auth check")
    park = park_read()

    # Step 1 — health
    print("  [1/3] Forgejo health...")
    status, _body = forgejo_health()
    if status != 200:
        print(f"        FAIL ({status}) — Forgejo not responding")
        return 1
    print(f"        OK ({status})")

    # Step 2 — admin auth round-trip
    if not park:
        print("  [2/3] Park file not present — bootstrap has not run.")
        print("  [3/3] (skipped)")
        return 2
    print("  [2/3] Admin auth round-trip...")
    status, body = forgejo_authed_user((park["admin_username"], park["admin_password"]))
    if status != 200:
        print(f"        FAIL ({status}) — admin credentials invalid")
        return 3
    print(f"        OK — logged in as {body.get('login', '?')}")

    # Step 3 — service token round-trip
    print("  [3/3] Service token round-trip...")
    # Use the token as bearer-style basic auth (Forgejo accepts token in basic auth pw position)
    status, body = forgejo_authed_user((park["service_username"], park["service_token"]))
    if status != 200:
        print(f"        FAIL ({status}) — service token invalid")
        return 4
    print(f"        OK — token authenticated as {body.get('login', '?')}")

    # Update last_verified
    park["last_verified"] = datetime.now(timezone.utc).isoformat()
    park_write(park)

    banner("Verify OK", char="=")
    print(f"  Park file:        {PARK_FILE}")
    print(f"  Last verified:    {park['last_verified']}")
    return 0


# ─────────────────────────────────────────────────────────────────────
# Dispatch
# ─────────────────────────────────────────────────────────────────────

SUBCOMMANDS = {
    "bootstrap": cmd_bootstrap,
    "reset-admin": cmd_reset_admin,
    "reset-service-token": cmd_reset_service_token,
    "verify": cmd_verify,
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in SUBCOMMANDS:
        print(f"Usage: {sys.argv[0]} {{{'|'.join(SUBCOMMANDS)}}}")
        return 64
    return SUBCOMMANDS[sys.argv[1]]()


if __name__ == "__main__":
    sys.exit(main())
