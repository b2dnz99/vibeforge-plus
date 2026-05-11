"""
VibeForge+ Install Wizard API (per INSTALL-WIZARD-API-PROPOSAL.md)

The bootstrap API layer the install wizard sits on top of. KISS auth gate:

  install_open(db) == True   iff no super_admin user exists in the DB

When install_open is True, the create-sa endpoint is reachable without
authentication, plus a programmatic first-IP-wins lock + rate limit per IP
to defeat opportunistic network attackers during the install window.

Once the SA is created, install_open flips to False and all bootstrap
endpoints require SA-only authentication via the standard session cookie.
"""
from __future__ import annotations

import json
import secrets
import string
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import bcrypt as _bcrypt
import re as _re
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.session import UserSession
from app.models.user import User

# Reuse the existing CLI script's helpers — single source of truth for the
# Forgejo wire calls. The script lives at scripts/bootstrap_forgejo.py.
_REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_REPO / "scripts"))
try:
    import bootstrap_forgejo as fj_script  # type: ignore
except Exception:
    fj_script = None

router = APIRouter()


# ─────────────────────────────────────────────────────────────────────
# In-memory state — reset on app restart, by design
# ─────────────────────────────────────────────────────────────────────

_first_ip: Optional[str] = None
_first_ip_at: Optional[float] = None

_rate_log: dict[str, list[float]] = {}
RATE_LIMIT_WINDOW_SEC = 60.0
RATE_LIMIT_MAX = 5


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_check(ip: str) -> None:
    now = time.time()
    log = _rate_log.setdefault(ip, [])
    log[:] = [t for t in log if now - t < RATE_LIMIT_WINDOW_SEC]
    if len(log) >= RATE_LIMIT_MAX:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded ({RATE_LIMIT_MAX} requests per {int(RATE_LIMIT_WINDOW_SEC)}s)",
        )
    log.append(now)


def install_open(db: Session) -> bool:
    """The KISS auth gate: True until BOTH an active SA and an active SU exist.

    VF-309: install is not considered complete until the first board-facing
    identity (SU) is provisioned in addition to the break-glass SA. See
    0-MD/0-Documentation/public/identity-roles.md §4.
    """
    active_sa = db.query(User).filter(
        User.role == "super_admin", User.status == "active"
    ).count()
    active_su = db.query(User).filter(
        User.role == "super_user", User.status == "active"
    ).count()
    return active_sa == 0 or active_su == 0


def _first_ip_lock(request: Request) -> None:
    global _first_ip, _first_ip_at
    ip = _client_ip(request)
    if _first_ip is None:
        _first_ip = ip
        _first_ip_at = time.time()
        return
    if ip != _first_ip:
        raise HTTPException(
            status_code=403,
            detail="Install wizard already claimed by another source. If this was a mistake, restart the app container.",
        )


def _clear_first_ip_lock() -> None:
    global _first_ip, _first_ip_at
    _first_ip = None
    _first_ip_at = None


def _get_sa_user(request: Request, db: Session) -> Optional[User]:
    """VF-309: SA identifies via the dedicated vf_sa_session cookie (session_type='sa'),
    never the board's vf_session. See 0-MD/0-Documentation/public/identity-roles.md §3."""
    session_id = request.cookies.get("vf_sa_session")
    if not session_id:
        return None
    sess = db.query(UserSession).filter(
        UserSession.id == session_id,
        UserSession.session_type == "sa",
        UserSession.expires_at > datetime.now(timezone.utc),
    ).first()
    if not sess:
        return None
    user = db.query(User).filter(User.id == sess.user_id).first()
    if not user or user.role != "super_admin" or user.status != "active":
        return None
    return user


def _require_sa(request: Request, db: Session) -> User:
    user = _get_sa_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="SA authentication required")
    return user


def _gate_open_or_sa(request: Request, db: Session) -> Optional[User]:
    """Allow if install is open (apply first-IP lock + rate limit) OR if SA-authenticated."""
    _rate_check(_client_ip(request))
    if install_open(db):
        _first_ip_lock(request)
        return None
    return _require_sa(request, db)


# ─────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────


# VF-252: loose email regex (basic shape: local@host.tld, TLD >=2 chars).
# Pydantic's EmailStr (via email-validator) rejects RFC special-use TLDs
# .local / .test / .example / .invalid — all reasonable choices for self-hosted
# operators. We do not need RFC5321 conformance here; we just need a sane
# string that an operator can recognise as their email. Anti-typo only.
_EMAIL_RE = _re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


class CreateSARequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    display_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=12, max_length=200)

    @field_validator("email")
    @classmethod
    def _validate_email_shape(cls, v: str) -> str:
        v = v.strip()
        if not _EMAIL_RE.match(v):
            raise ValueError(
                "email must look like local@host.tld; TLD >= 2 chars. "
                "Special-use TLDs like .local / .test are accepted."
            )
        return v


# ─────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────


@router.get("/api/v2/bootstrap/status")
def bootstrap_status(request: Request, db: Session = Depends(get_db)):
    _gate_open_or_sa(request, db)
    open_state = install_open(db)
    sa_exists = not open_state

    forgejo_park = Path("/opt/vibeforge/.bootstrap/forgejo.json")
    forgejo_park_exists = forgejo_park.exists()
    forgejo_last_verified = None
    if forgejo_park_exists:
        try:
            forgejo_last_verified = json.loads(forgejo_park.read_text()).get("last_verified")
        except Exception:
            pass

    vault_park = Path("/opt/vibeforge/.bootstrap/vaultwarden.json")
    vault_park_exists = vault_park.exists()

    steps = {
        "create_sa": {
            "status": "done" if sa_exists else "pending",
            "required": True,
        },
        "forgejo": {
            "status": "done" if forgejo_park_exists else "pending",
            "required": True,
            "park_file": str(forgejo_park),
            "park_exists": forgejo_park_exists,
            "last_verified": forgejo_last_verified,
        },
        "vaultwarden": {
            "status": "done" if vault_park_exists else "pending",
            "required": False,
            "park_file": str(vault_park),
            "park_exists": vault_park_exists,
        },
    }

    if not sa_exists:
        next_action = "create_sa"
    elif not forgejo_park_exists:
        next_action = "forgejo"
    elif not vault_park_exists:
        next_action = "vaultwarden"
    else:
        next_action = None

    return {
        "install_open": open_state,
        "sa_exists": sa_exists,
        "steps": steps,
        "next_action": next_action,
    }


@router.post("/api/v2/bootstrap/create-sa")
def create_sa(body: CreateSARequest, request: Request, response: Response, db: Session = Depends(get_db)):
    _rate_check(_client_ip(request))

    # VF-309: create-sa is gated on "no SA exists", not install_open (which now also
    # requires an SU). Narrower gate so the SA-creation step itself is idempotent once done.
    existing_sa = db.query(User).filter(User.role == "super_admin").count()
    if existing_sa > 0:
        raise HTTPException(
            status_code=403,
            detail="An SA already exists. The install wizard cannot be re-run. Use scripts/reset_sa.sh to recover lost credentials.",
        )

    _first_ip_lock(request)

    user_id = str(uuid.uuid4())
    user = User(
        id=user_id,
        email=body.email,
        display_name=body.display_name,
        password_hash=_bcrypt.hashpw(body.password.encode(), _bcrypt.gensalt()).decode(),
        role="super_admin",
        must_change_password=False,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    db.commit()

    # VF-309: SA identifies via the dedicated vf_sa_session cookie scoped to /admin/,
    # never the board's vf_session. The SA is break-glass, not a board participant.
    # See 0-MD/0-Documentation/public/identity-roles.md §2, §3.
    session_id = str(uuid.uuid4())
    sess = UserSession(
        id=session_id,
        user_id=user_id,
        session_type="sa",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent", "")[:500],
    )
    db.add(sess)
    db.commit()

    response.set_cookie(
        key="vf_sa_session",
        value=session_id,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=30 * 60,
        path="/admin/",
    )

    _clear_first_ip_lock()

    return {
        "status": "ok",
        "sa_id": user_id,
        "email": user.email,
        "display_name": user.display_name,
        "next_action": "create_su",
        "redirect": "/admin/",
    }


# ─────────────────────────────────────────────────────────────────────
# Forgejo endpoints — all SA-only, reuse the script's helpers
# ─────────────────────────────────────────────────────────────────────


def _check_fj_script() -> None:
    if fj_script is None:
        raise HTTPException(
            status_code=500,
            detail="bootstrap_forgejo.py not importable. Check scripts/ deployment.",
        )


def _gen_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _docker_forgejo(*forgejo_args: str) -> tuple[bool, str]:
    """Run a forgejo subcommand inside the container as the git user."""
    cmd = [
        "docker", "compose", "-f", "/opt/vibeforge/docker-compose.yml",
        "exec", "-T", "--user", "git", "forgejo",
        "forgejo", *forgejo_args,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return False, (r.stderr or r.stdout)[:500]
        return True, r.stdout
    except Exception as e:
        return False, str(e)


@router.post("/api/v2/bootstrap/forgejo")
def bootstrap_forgejo_post(request: Request, db: Session = Depends(get_db)):
    _require_sa(request, db)
    _check_fj_script()

    park_file = Path(fj_script.PARK_FILE)
    if park_file.exists():
        raise HTTPException(
            status_code=409,
            detail=f"Forgejo bootstrap already done. Park file at {park_file}. Use reset-admin / reset-service-token to rotate credentials.",
        )

    status, _ = fj_script.forgejo_health()
    if status != 200:
        raise HTTPException(status_code=502, detail=f"Forgejo not responding (HTTP {status}).")

    admin_password = _gen_password()
    ok, out = _docker_forgejo(
        "admin", "user", "create",
        "--username", fj_script.ADMIN_USER,
        "--password", admin_password,
        "--email", fj_script.ADMIN_EMAIL,
        "--admin", "--must-change-password=false",
    )
    if not ok:
        raise HTTPException(status_code=500, detail=f"Failed to create Forgejo admin: {out}")

    service_password = _gen_password()
    status, body = fj_script.forgejo_admin_create_user(
        (fj_script.ADMIN_USER, admin_password),
        fj_script.SERVICE_USER, service_password, fj_script.SERVICE_EMAIL,
        must_change=False,
    )
    if status not in (200, 201):
        raise HTTPException(status_code=500, detail=f"Failed to create Forgejo service account: {status} {body}")

    status, body = fj_script.forgejo_create_token(
        (fj_script.SERVICE_USER, service_password),
        fj_script.SERVICE_USER, "vibeforge-bootstrap-via-wizard",
    )
    if status not in (200, 201):
        raise HTTPException(status_code=500, detail=f"Failed to issue Forgejo service token: {status} {body}")
    service_token = body.get("sha1") if body else None

    now = datetime.now(timezone.utc).isoformat()
    park = {
        "admin_username": fj_script.ADMIN_USER,
        "admin_password": admin_password,
        "admin_email": fj_script.ADMIN_EMAIL,
        "service_username": fj_script.SERVICE_USER,
        "service_password": service_password,
        "service_token": service_token,
        "forgejo_url": fj_script.FORGEJO_URL,
        "generated_at": now,
        "last_verified": now,
        "via": "wizard-api",
    }
    fj_script.park_write(park)

    return {
        "status": "ok",
        "park_file": str(park_file),
        "admin_username": fj_script.ADMIN_USER,
        "service_username": fj_script.SERVICE_USER,
        "next_action": "vaultwarden",
    }


@router.get("/api/v2/bootstrap/credentials/forgejo")
def get_forgejo_credentials(request: Request, db: Session = Depends(get_db)):
    _require_sa(request, db)
    _check_fj_script()
    park_file = Path(fj_script.PARK_FILE)
    if not park_file.exists():
        raise HTTPException(status_code=404, detail="No Forgejo park file. Run /bootstrap/forgejo first.")
    return json.loads(park_file.read_text())


@router.post("/api/v2/bootstrap/forgejo/verify")
def verify_forgejo(request: Request, db: Session = Depends(get_db)):
    _require_sa(request, db)
    _check_fj_script()
    park = fj_script.park_read()
    if not park:
        raise HTTPException(status_code=404, detail="No Forgejo park file.")
    status, _ = fj_script.forgejo_health()
    if status != 200:
        raise HTTPException(status_code=502, detail=f"Forgejo health check failed ({status})")
    status, body = fj_script.forgejo_authed_user((park["admin_username"], park["admin_password"]))
    if status != 200:
        raise HTTPException(status_code=502, detail=f"Admin auth round-trip failed ({status})")
    admin_login = body.get("login") if body else None
    status, body = fj_script.forgejo_authed_user((park["service_username"], park["service_token"]))
    if status != 200:
        raise HTTPException(status_code=502, detail=f"Service token round-trip failed ({status})")
    service_login = body.get("login") if body else None
    park["last_verified"] = datetime.now(timezone.utc).isoformat()
    fj_script.park_write(park)
    return {
        "status": "ok",
        "admin_login": admin_login,
        "service_login": service_login,
        "last_verified": park["last_verified"],
    }


@router.post("/api/v2/bootstrap/forgejo/reset-admin")
def reset_forgejo_admin(request: Request, db: Session = Depends(get_db)):
    _require_sa(request, db)
    _check_fj_script()
    park = fj_script.park_read()
    if not park:
        raise HTTPException(status_code=404, detail="No Forgejo park file.")
    new_password = _gen_password()
    ok, out = _docker_forgejo(
        "admin", "user", "change-password",
        "--username", park["admin_username"],
        "--password", new_password,
        "--must-change-password=false",
    )
    if not ok:
        raise HTTPException(status_code=500, detail=f"Reset failed: {out}")
    park["admin_password"] = new_password
    park["last_verified"] = datetime.now(timezone.utc).isoformat()
    fj_script.park_write(park)
    return {
        "status": "ok",
        "admin_username": park["admin_username"],
        "new_password": new_password,
        "park_file": str(fj_script.PARK_FILE),
    }


@router.post("/api/v2/bootstrap/forgejo/reset-service-token")
def reset_forgejo_service_token(request: Request, db: Session = Depends(get_db)):
    _require_sa(request, db)
    _check_fj_script()
    park = fj_script.park_read()
    if not park:
        raise HTTPException(status_code=404, detail="No Forgejo park file.")
    name = f"vibeforge-svc-wizard-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    status, body = fj_script.forgejo_create_token(
        (park["service_username"], park["service_password"]),
        park["service_username"], name,
    )
    if status not in (200, 201):
        raise HTTPException(status_code=502, detail=f"Token issue failed ({status}): {body}")
    new_token = body.get("sha1") if body else None
    park["service_token"] = new_token
    park["last_verified"] = datetime.now(timezone.utc).isoformat()
    fj_script.park_write(park)
    return {
        "status": "ok",
        "service_username": park["service_username"],
        "token_name": name,
        "new_token": new_token,
        "park_file": str(fj_script.PARK_FILE),
        "note": "Old service tokens remain valid until manually revoked via Forgejo UI.",
    }
