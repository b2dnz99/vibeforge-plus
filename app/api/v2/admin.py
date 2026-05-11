"""
VibeForge+ Admin Panel
# WHY: Separate module for admin routes — keeps board UI (ui.py) clean
# GATE: All routes require SA elevation (vf_sa_session cookie)
# FLOW: Config page → Admin link → SA login prompt → Admin dashboard
"""
import uuid
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session
import bcrypt as _bcrypt

from app.db.session import get_db
from app.models.user import User
from app.models.project import Project
from app.models.agent import Agent
from app.models.project_member import ProjectMember
from app.models.activity import ActivityEvent
from app.models.session import UserSession
from app.models.agent_token_download import AgentTokenDownload

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

SA_COOKIE = "vf_sa_session"
SA_SESSION_MINUTES = 30
SU_ELEVATION_MINUTES = 15  # VF-264: SU admin-context window. Rolling: extends on each use.
TOKEN_DOWNLOAD_TTL_MIN = 5  # VF-303: one-time token-download nonce TTL.

# VF-264: per-request contextvars so _audit picks up the elevated flag without needing
# every callsite to thread the request through. Set by _require_sa; read by _audit.
import contextvars as _cv
_acting_elevated = _cv.ContextVar("_acting_elevated", default=False)
_acting_user_id_cv = _cv.ContextVar("_acting_user_id_cv", default=None)
# VF-328: extra per-request flags for tier-aware audit. See ADMIN-PORTAL-PERM-TIERS-PROPOSAL §6.
_acting_sa_session = _cv.ContextVar("_acting_sa_session", default=False)
_acting_break_glass = _cv.ContextVar("_acting_break_glass", default=False)
_acting_privilege = _cv.ContextVar("_acting_privilege", default=None)  # "sa" | "su-elevated" | None


def _stash_token_download(db: Session, agent_id: str, raw_token: str) -> str:
    """VF-303: write the plaintext agent token to a short-lived store keyed by
    a single-use nonce. Returns the nonce. Caller composes the download URL
    and must commit the session (this helper only adds the row).
    """
    import secrets as _s
    nonce = _s.token_urlsafe(24)
    now = datetime.now(timezone.utc)
    db.add(AgentTokenDownload(
        id=str(uuid.uuid4()),
        agent_id=agent_id,
        nonce=nonce,
        token_plaintext=raw_token,
        created_at=now,
        expires_at=now + timedelta(minutes=TOKEN_DOWNLOAD_TTL_MIN),
    ))
    return nonce


# ── SA session helpers ──

def _get_sa_user(request: Request, db: Session) -> User | None:
    """Check SA elevation cookie. Returns SA user or None."""
    # GATE: SA elevation required for all admin routes
    session_id = request.cookies.get(SA_COOKIE)
    if not session_id:
        return None
    sess = db.query(UserSession).filter(
        UserSession.id == session_id,
        UserSession.session_type == "sa",
        UserSession.expires_at > datetime.now(timezone.utc),
    ).first()
    if not sess:
        return None
    user = db.query(User).filter(
        User.id == sess.user_id,
        User.role == "super_admin",
        User.status == "active",
    ).first()
    return user


def _get_elevated_su(request: Request, db: Session) -> tuple["User | None", "UserSession | None"]:
    """VF-264: check for an elevated SU vf_session. Returns (su_user, session) on success
    so the caller can bump the rolling timeout. (None, None) otherwise."""
    session_id = request.cookies.get("vf_session")
    if not session_id:
        return None, None
    now = datetime.now(timezone.utc)
    sess = db.query(UserSession).filter(
        UserSession.id == session_id,
        UserSession.session_type == "user",
        UserSession.expires_at > now,
        UserSession.elevated_until.is_not(None),
        UserSession.elevated_until > now,
    ).first()
    if not sess:
        return None, None
    user = db.query(User).filter(
        User.id == sess.user_id,
        User.role == "super_user",
        User.status == "active",
    ).first()
    if not user:
        return None, None
    return user, sess


def _set_acting_state(user, *, via_sa: bool, via_su_elevated: bool, has_su_session: bool) -> None:
    """VF-328: single place to stamp all per-request acting-context flags.
    The contextvars are read later by _audit so tier-aware fields land on every event."""
    _acting_elevated.set(via_su_elevated)
    _acting_user_id_cv.set(user.id if user else None)
    _acting_sa_session.set(via_sa)
    _acting_break_glass.set(via_sa and not has_su_session)
    if via_sa:
        _acting_privilege.set("sa")
    elif via_su_elevated:
        _acting_privilege.set("su-elevated")
    else:
        _acting_privilege.set(None)


def _resolve_acting_user(request: Request, db: Session, *, system_only: bool) -> User | None:
    """VF-328: shared gate resolver behind the three tier helpers.

    system_only=False (tier R/U): passes for SA (stacked or break-glass) OR SU-elevated.
    system_only=True  (tier S):    passes for SA only. SU-elevated alone is rejected.

    Returns the *acting human* for audit attribution:
    - Stacked SA  (vf_sa_session + vf_session for an SU): returns the SU.
    - Pure break-glass (vf_sa_session only): returns the SA account.
    - SU-elevated (vf_session with elevated_until in future): returns the SU.

    Side effect: bumps elevated_until on rolling-window SU sessions; sets per-request
    contextvars so _audit emits sa_session_active/break_glass/privilege_used per
    ADMIN-PORTAL-PERM-TIERS-PROPOSAL §6.
    """
    sa = _get_sa_user(request, db)
    has_su_session = bool(request.cookies.get("vf_session"))

    if sa:
        # SA cookie present. Determine attribution:
        # - if SU session also present (stacked): attribute as SU
        # - if no SU session (pure break-glass): attribute as SA
        if has_su_session:
            from app.api.v2.ui import _get_session_user
            su_user = _get_session_user(request, db)
            if su_user and su_user.role == "super_user":
                _set_acting_state(su_user, via_sa=True, via_su_elevated=False, has_su_session=True)
                return su_user
        # Pure break-glass — SA account is the only identity.
        _set_acting_state(sa, via_sa=True, via_su_elevated=False, has_su_session=False)
        return sa

    if system_only:
        # Tier S without SA cookie: SU-elevated is not enough.
        return None

    su, sess = _get_elevated_su(request, db)
    if su and sess:
        # Rolling window per D4.C — bump the expiry each time the gate passes.
        try:
            sess.elevated_until = datetime.now(timezone.utc) + timedelta(minutes=SU_ELEVATION_MINUTES)
            db.commit()
        except Exception:
            db.rollback()
        _set_acting_state(su, via_sa=False, via_su_elevated=True, has_su_session=True)
        return su

    return None


def _require_portal_read(request: Request, db: Session) -> User | None:
    """VF-328 Tier R — passes for SA (stacked or break-glass) OR SU-elevated.
    Same authorisation set as tier U today; distinct helper so callsites name intent
    and future relaxation (e.g. plain SU read) can land here."""
    return _resolve_acting_user(request, db, system_only=False)


def _require_portal_user_write(request: Request, db: Session) -> User | None:
    """VF-328 Tier U — passes for SA (stacked or break-glass) OR SU-elevated.
    Use for actions on users / agents / sessions / memberships / project roles."""
    return _resolve_acting_user(request, db, system_only=False)


def _require_portal_system_write(request: Request, db: Session) -> User | None:
    """VF-328 Tier S — passes for SA only. SU-elevated without SA cookie is rejected.
    Use for system-config writes: certs, SSO, SMTP, backup, branding, feature flags,
    session policy. See ADMIN-PORTAL-PERM-TIERS-PROPOSAL §2.1 for the full mapping."""
    return _resolve_acting_user(request, db, system_only=True)


# Back-compat alias. Existing callsites keep working as tier-U (the prior semantics).
# Piece 3 (callsite triage) walks every use and reclassifies the tier-S subset to
# _require_portal_system_write directly.
_require_sa = _require_portal_user_write


def _render_admin_login(request: Request, db: Session, force_sa: bool = False):
    """VF-264: dual-mode admin login page. If the requester has an active SU
    vf_session, render the elevate form (SU confirms own password). Otherwise
    render the classic SA login form (break-glass). See identity-roles.md §3.

    VF-317: force_sa=True overrides dual-mode and always renders the SA-login
    form. Used by the 'Login as SA →' link in admin nav so an elevated SU can
    establish a concurrent SA session without dropping their board session.
    """
    from app.api.v2.ui import _get_session_user
    current_user = _get_session_user(request, db)
    if force_sa:
        mode = "sa_login"
    else:
        mode = "elevate" if (current_user and current_user.role == "super_user") else "sa_login"
    return templates.TemplateResponse("ui/admin_login.html", {
        "request": request,
        "mode": mode,
        "current_user": current_user,
    })


def _create_sa_session(user: User, db: Session, request: Request) -> str:
    """Create a short-lived SA session.

    VF-341: TTL now read from session_policy.elevation_ttl_minutes (default
    15min, was 30min hardcoded). Concurrent cap also enforced — though SA
    sessions are typically singular per operator, the cap protects against
    a compromised /admin/login spam from accumulating SA cookies.
    """
    from app.api.v2 import session_policy as _sp
    session_id = str(uuid.uuid4())
    expires = datetime.now(timezone.utc) + timedelta(minutes=_sp.elevation_ttl_minutes(db))
    _sp.enforce_concurrent_cap(db, user.id, "sa")
    sess = UserSession(
        id=session_id, user_id=user.id, session_type="sa",
        expires_at=expires,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent", "")[:500],
    )
    db.add(sess)
    db.commit()
    return session_id


def _audit(db: Session, action: str, actor_name: str, details: dict, user_id: str = None,
           request: Request = None):
    """Log an admin action to activity_events. v3: dual preservation (FK + snapshot).

    VF-264: picks up the acting-via-SU-elevation flag from the per-request contextvar
    set by _require_sa. Callsites don't need to thread the request through. When the
    acting user reached this point via SU elevation (not SA cookie), tags elevated=true
    in details. Also backfills actor_user_id from the contextvar if the callsite didn't
    pass one, so audits keep their dual-preservation integrity (FK + snapshot).
    """
    out = {**details, "actor": actor_name}
    try:
        if _acting_elevated.get():
            out["elevated"] = True
    except Exception:
        pass
    # VF-328: tier-aware audit fields. sa_session_active flags any action performed
    # while a vf_sa_session cookie was present. break_glass narrows that to the
    # pure-SA path (no vf_session). privilege_used names which gate satisfied:
    # "sa" (SA cookie path) or "su-elevated" (SU password-confirm path).
    try:
        if _acting_sa_session.get():
            out["sa_session_active"] = True
    except Exception:
        pass
    try:
        if _acting_break_glass.get():
            out["break_glass"] = True
    except Exception:
        pass
    try:
        priv = _acting_privilege.get()
        if priv:
            out["privilege_used"] = priv
    except Exception:
        pass
    if user_id is None:
        try:
            user_id = _acting_user_id_cv.get()
        except Exception:
            pass
    db.add(ActivityEvent(
        project_id=None, task_id=None,
        actor_type="human", actor_user_id=user_id,
        action=action,
        details=json.dumps(out),
    ))


# ── Validation helpers (imported pattern from ui.py) ──

import re as _re

ILLEGAL_PASSWORD_CHARS = set('<>{}()[]|\\`~')

def _validate_password(password: str) -> str | None:
    # RULE: Password complexity — min 12, upper+lower+number+symbol, no illegal chars
    if len(password) < 12:
        return "Password must be at least 12 characters."
    if not _re.search(r'[A-Z]', password):
        return "Must contain at least one uppercase letter."
    if not _re.search(r'[a-z]', password):
        return "Must contain at least one lowercase letter."
    if not _re.search(r'[0-9]', password):
        return "Must contain at least one number."
    if not _re.search(r'[!@#$%^&*\-_=+.,;:?/]', password):
        return "Must contain at least one symbol."
    if any(c in ILLEGAL_PASSWORD_CHARS for c in password):
        return "Contains illegal characters."
    return None

def _validate_username(username: str) -> str | None:
    # RULE: Username — max 10 chars, lowercase, no spaces
    if not username or len(username) > 10:
        return "Username must be 1-10 characters."
    if ' ' in username:
        return "Username cannot contain spaces."
    if not _re.match(r'^[a-z0-9_]+$', username):
        return "Username must be lowercase letters, numbers, or underscores only."
    return None


# ── SA Login ──

class SALoginBody(BaseModel):
    password: str


@router.get("/admin/", response_class=HTMLResponse)
def admin_dashboard(request: Request, db: Session = Depends(get_db)):
    # VF-335: Old admin landing graduated to /admin/portal/. Hard 301 so
    # bookmarks + agent muscle-memory route to the canonical surface.
    return RedirectResponse(url="/admin/portal/", status_code=301)


@router.get("/admin/users/new", response_class=HTMLResponse)
def admin_user_new(request: Request):
    # VF-335: New-user form is now the master-detail __new__ panel per VF-329 §3.3.
    return RedirectResponse(url="/admin/portal/administration/users/__new__", status_code=301)


@router.get("/admin/users/{user_id}", response_class=HTMLResponse)
def admin_user_detail(user_id: str, request: Request):
    # VF-335: User detail graduated to portal master-detail (VF-329).
    return RedirectResponse(url=f"/admin/portal/administration/users/{user_id}", status_code=301)


class UpdateUserBody(BaseModel):
    model_config = ConfigDict(extra='forbid')  # VF-357: reject undocumented PATCH fields
    username: str | None = None
    display_name: str | None = None
    email: str | None = None
    nickname: str | None = None
    title: str | None = None
    display_role: str | None = None


@router.patch("/admin/api/users/{user_id}")
def update_user(user_id: str, body: UpdateUserBody, request: Request, db: Session = Depends(get_db)):
    sa = _require_sa(request, db)
    if not sa:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="SA elevation required.")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="User not found.")

    changes = []

    if body.username is not None and body.username != user.username:
        un_err = _validate_username(body.username)
        if un_err:
            from fastapi import HTTPException
            raise HTTPException(status_code=422, detail=un_err)
        # GATE: Check uniqueness
        if db.query(User).filter(User.username == body.username, User.id != user_id).first():
            from fastapi import HTTPException
            raise HTTPException(status_code=409, detail="Username already taken.")
        changes.append(f"username: {user.username} → {body.username}")
        user.username = body.username

    if body.email is not None:
        # VF-334: normalise blank/whitespace; reject empty (column nullable=False); uniqueness only when non-empty.
        email_normalized = body.email.strip()
        if not email_normalized:
            from fastapi import HTTPException
            raise HTTPException(status_code=422, detail="Email cannot be blank.")
        if email_normalized != user.email:
            if db.query(User).filter(User.email == email_normalized, User.id != user_id).first():
                from fastapi import HTTPException
                raise HTTPException(status_code=409, detail="Email already registered.")
            changes.append(f"email: {user.email} → {email_normalized}")
            user.email = email_normalized

    if body.display_name is not None and body.display_name != user.display_name:
        changes.append(f"display_name: {user.display_name} → {body.display_name}")
        user.display_name = body.display_name

    if body.nickname is not None:
        user.nickname = body.nickname
    if body.title is not None:
        user.title = body.title
    if body.display_role is not None:
        user.display_role = body.display_role

    if changes:
        _audit(db, "user_updated", sa.display_name, {
            "username": user.username, "changes": changes,
        })

    db.commit()
    return {"ok": True, "changes": changes}


@router.get("/admin")
def admin_root_redirect():
    # Convenience: GET /admin → /admin/ (FastAPI's redirect_slashes doesn't fire through the reverse proxy).
    return RedirectResponse(url="/admin/", status_code=302)


@router.get("/admin/login")
def admin_login_page(request: Request, db: Session = Depends(get_db)):
    """Renders the SA login page directly. Smart mode decides between elevate
    (SU password-confirm) vs classic SA login based on the active board session.

    VF-317: `?as=sa` forces the SA-login form (for the "Login as SA ->"
    affordance from admin nav when acting as elevated SU).

    VF-335: previously redirected to `/admin/` and let admin_dashboard render
    the login fallback. Now that `/admin/` 301s to `/admin/portal/`, that hop
    creates a 302 loop with the portal's own unauth redirect back to here.
    Render directly to break the cycle.
    """
    force_sa = request.query_params.get("as") == "sa"
    return _render_admin_login(request, db, force_sa=force_sa)


@router.post("/admin/login")
def admin_login(body: SALoginBody, request: Request, db: Session = Depends(get_db)):
    # GATE: Only super_admin can elevate
    sa = db.query(User).filter(User.role == "super_admin", User.status == "active").first()
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "unknown").split(",")[0].strip()
    ua = request.headers.get("user-agent", "")[:300]

    # VF-264 polish: capture the active board-session user (if any) so the audit trail
    # tells us which human attempted SA login. Anonymous direct-URL hits still have no
    # identifiable attempter — only active-session hits are captured here.
    from app.api.v2.ui import _get_session_user
    attempting = _get_session_user(request, db)
    attempting_info = None
    if attempting:
        attempting_info = {
            "id": attempting.id,
            "username": attempting.username,
            "display_name": attempting.display_name,
            "role": attempting.role,
        }

    base_details = {"ip": ip, "user_agent": ua}
    if attempting_info is not None:
        base_details["attempting_user"] = attempting_info

    if not sa:
        _audit(db, "sa_login_failed", "system",
               {**base_details, "reason": "no_sa_account"},
               user_id=(attempting.id if attempting else None))
        db.commit()
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="No Super Admin account exists.")
    if not _bcrypt.checkpw(body.password.encode(), sa.password_hash.encode()):
        _audit(db, "sa_login_failed", sa.display_name,
               {**base_details, "reason": "bad_password"},
               user_id=(attempting.id if attempting else None))
        db.commit()
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Invalid SA credentials.")

    _audit(db, "sa_login_success", sa.display_name, base_details,
           user_id=sa.id)
    session_id = _create_sa_session(sa, db, request)
    response = JSONResponse(content={"ok": True, "user": sa.display_name})
    # VF-326: cookie path "/" so SA cookie reaches tier-S API endpoints under
    # /api/v2/proxy/* (the wizard's mutation calls). Was "/admin/" which scoped
    # the cookie to admin HTML routes only — making /cert/swap, /mode/switch,
    # /cert/validate, /cert/export all silently 403 from the browser despite
    # active SA elevation. SameSite=lax + HttpOnly retain CSRF / XSS posture.
    response.set_cookie(
        key=SA_COOKIE, value=session_id,
        httponly=True, samesite="lax", path="/",
        max_age=SA_SESSION_MINUTES * 60,
    )
    return response


class ElevateBody(BaseModel):
    password: str


@router.post("/admin/elevate")
def admin_elevate(body: ElevateBody, request: Request, db: Session = Depends(get_db)):
    """VF-264 Phase 2: SU self-elevation via password re-confirm (sudo pattern).

    Requires an active vf_session belonging to a super_user. On password match,
    stamps sessions.elevated_until = now() + SU_ELEVATION_MINUTES. No new cookie
    issued — the existing vf_session carries the elevation via its column.
    """
    from app.api.v2.ui import _get_session_user  # late import to avoid circular
    user = _get_session_user(request, db)
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "unknown").split(",")[0].strip()
    ua = request.headers.get("user-agent", "")[:300]
    if not user:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Login required before elevating.")
    if user.role != "super_user":
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Only Super Users can elevate. SA uses /admin/login.")

    if not _bcrypt.checkpw(body.password.encode(), user.password_hash.encode()):
        _audit(db, "su_elevation_failed", user.display_name,
               {"ip": ip, "user_agent": ua, "reason": "bad_password"},
               user_id=user.id)
        db.commit()
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Incorrect password.")

    # Stamp the current vf_session with elevation
    session_id = request.cookies.get("vf_session")
    sess = db.query(UserSession).filter(
        UserSession.id == session_id,
        UserSession.session_type == "user",
    ).first()
    if not sess:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Session not found — please log in again.")

    expires = datetime.now(timezone.utc) + timedelta(minutes=SU_ELEVATION_MINUTES)
    sess.elevated_until = expires
    _audit(db, "su_elevation_granted", user.display_name,
           {"ip": ip, "user_agent": ua, "elevated_until": expires.isoformat()},
           user_id=user.id)
    db.commit()
    return {"ok": True, "elevated_until": expires.isoformat()}


@router.post("/admin/unelevate")
def admin_unelevate(request: Request, db: Session = Depends(get_db)):
    """VF-264 Phase 2: drop SU elevation early (explicit 'exit admin mode').

    Clears elevated_until on the current vf_session. Cheap to offer; agents a
    'lock admin' button for SU who want to step away without logging out.
    """
    from app.api.v2.ui import _get_session_user
    user = _get_session_user(request, db)
    if not user:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Login required.")
    session_id = request.cookies.get("vf_session")
    sess = db.query(UserSession).filter(
        UserSession.id == session_id,
        UserSession.session_type == "user",
    ).first()
    if sess and sess.elevated_until is not None:
        sess.elevated_until = None
        _audit(db, "su_elevation_dropped", user.display_name, {}, user_id=user.id)
        db.commit()
    return {"ok": True}


@router.get("/admin/logout")
def admin_logout(request: Request, db: Session = Depends(get_db)):
    """Unified exit from admin mode. Handles both paths:
    - SA cookie present: delete SA session, clear vf_sa_session cookie.
    - SU elevation present: clear elevated_until on the existing vf_session (stays logged in to the board).
    Always redirects to /ui/.
    """
    from app.api.v2.ui import _get_session_user
    # SA path
    sa_session_id = request.cookies.get(SA_COOKIE)
    cleared_sa = False
    if sa_session_id:
        sess = db.query(UserSession).filter(UserSession.id == sa_session_id).first()
        if sess:
            db.delete(sess)
            cleared_sa = True

    # SU elevation path
    user = _get_session_user(request, db)
    cleared_su_elevation = False
    if user and user.role == "super_user":
        session_id = request.cookies.get("vf_session")
        if session_id:
            sess = db.query(UserSession).filter(
                UserSession.id == session_id,
                UserSession.session_type == "user",
            ).first()
            if sess and sess.elevated_until is not None:
                sess.elevated_until = None
                cleared_su_elevation = True
                _audit(db, "su_elevation_dropped", user.display_name, {"via": "admin_logout"}, user_id=user.id)

    db.commit()
    response = RedirectResponse(url="/ui/", status_code=302)
    if cleared_sa:
        response.delete_cookie(SA_COOKIE, path="/")
    return response


# ── User Management API ──

class ChangeSAPasswordBody(BaseModel):
    current_password: str
    new_password: str


class ChangeUserPasswordBody(BaseModel):
    current_password: str
    new_password: str


@router.post("/admin/api/users/{user_id}/change-password")
def change_user_password(user_id: str, body: ChangeUserPasswordBody, request: Request, db: Session = Depends(get_db)):
    # GATE: SA elevation required
    sa = _require_sa(request, db)
    if not sa:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="SA elevation required.")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="User not found.")
    if not _bcrypt.checkpw(body.current_password.encode(), user.password_hash.encode()):
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Current password is incorrect.")
    pw_err = _validate_password(body.new_password)
    if pw_err:
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail=pw_err)
    user.password_hash = _bcrypt.hashpw(body.new_password.encode(), _bcrypt.gensalt()).decode()
    user.must_change_password = False
    _audit(db, "user_password_changed", sa.display_name, {"username": user.username, "changed_by": "admin"})
    db.commit()
    return {"ok": True, "message": "Password changed."}


@router.post("/admin/api/change-sa-password")
def change_sa_password(body: ChangeSAPasswordBody, request: Request, db: Session = Depends(get_db)):
    # GATE: VF-328 Tier-S — SA only. Elevated SU cannot rotate the SA password
    # (would let them lock break-glass out and hijack the recovery path).
    sa = _require_portal_system_write(request, db)
    if not sa:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Super Admin credentials required to change the SA password.")
    if not _bcrypt.checkpw(body.current_password.encode(), sa.password_hash.encode()):
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Current password is incorrect.")
    pw_err = _validate_password(body.new_password)
    if pw_err:
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail=pw_err)
    sa.password_hash = _bcrypt.hashpw(body.new_password.encode(), _bcrypt.gensalt()).decode()
    sa.must_change_password = False
    # VF-311: distinct action type so SA can recognise self-change vs force-reset on their
    # own activity view. Captures IP/UA for parity with login events.
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "unknown").split(",")[0].strip()
    ua = request.headers.get("user-agent", "")[:300]
    _audit(db, "sa_password_self_change", sa.display_name, {"ip": ip, "user_agent": ua}, user_id=sa.id)
    db.commit()
    return {"ok": True, "message": "SA password changed."}


@router.post("/admin/api/sa-password-force-reset/ack")
def ack_sa_password_force_reset(request: Request, db: Session = Depends(get_db)):
    """VF-311: SA acknowledges having reviewed the most recent force-reset event.
    Writes an sa_password_force_reset_ack event so the dashboard banner stops showing
    until the next force-reset fires.
    VF-328: Tier-S — only SA can ack their own password-event audit."""
    sa = _require_portal_system_write(request, db)
    if not sa:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Super Admin credentials required to ack SA password events.")
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "unknown").split(",")[0].strip()
    ua = request.headers.get("user-agent", "")[:300]
    _audit(db, "sa_password_force_reset_ack", sa.display_name, {"ip": ip, "user_agent": ua}, user_id=sa.id)
    db.commit()
    return {"ok": True}


@router.post("/admin/api/users/{user_id}/force-password-change")
def force_user_password_change(user_id: str, request: Request, db: Session = Depends(get_db)):
    """VF-329 F2: tier-U action — flip the must_change_password flag on a user
    without rotating their password. Previously the UI POSTed {must_change_password: true}
    to /change-password which 422'd because that endpoint requires {current,new}_password.
    """
    sa = _require_sa(request, db)
    if not sa:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="SA elevation required.")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="User not found.")
    if user.must_change_password:
        # idempotent — already flagged
        return {"ok": True, "message": "Force-change flag already set.", "changed": False}
    user.must_change_password = True
    _audit(db, "user_force_password_change_set", sa.display_name,
           {"username": user.username, "display_name": user.display_name})
    db.commit()
    return {"ok": True, "message": f"{user.display_name} will be prompted to change password on next login.", "changed": True}


# ── Agent Management API ──

import hashlib
import secrets


class CreateAgentBody(BaseModel):
    name: str
    project_slug: str
    model_type: str = "claude"
    description: str = ""
    # VF-307: admin can create an agent on behalf of a specific user (Shape B flow).
    # If set, created_by = target_user_id; otherwise falls back to the acting SA/SU
    # (legacy path; kept for backward-compat but discouraged under v3).
    target_user_id: str | None = None


@router.get("/admin/api/agents")
def list_all_agents(request: Request, db: Session = Depends(get_db)):
    """List all agents grouped by project — SA only."""
    sa = _require_sa(request, db)
    if not sa:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="SA elevation required.")
    agents = db.query(Agent).order_by(Agent.name).all()
    result = []
    for a in agents:
        proj = db.query(Project.slug, Project.name).filter(Project.id == a.project_id).first() if a.project_id else None
        creator = db.query(User.username, User.display_name).filter(User.id == a.created_by).first() if a.created_by else None
        # WHY: Show first 6 + last 3 of token prefix area for identification
        token_display = f"{a.token_prefix}...{a.api_token_hash[-3:]}" if a.api_token_hash and a.token_prefix else "no token"
        result.append({
            "id": a.id, "name": a.name, "slug": a.slug, "status": a.status,
            "description": a.description,
            "model_type": a.model_type, "model_name": a.model_name,
            "token_display": token_display,
            "project": {"slug": proj[0], "name": proj[1]} if proj else None,
            "creator": {"username": creator[0], "display_name": creator[1]} if creator else None,
            "revoked_at": a.revoked_at.isoformat() if a.revoked_at else None,
            "created_at": a.created_at.isoformat(),
        })
    return result


@router.post("/admin/api/agents", status_code=201)
def create_agent(body: CreateAgentBody, request: Request, db: Session = Depends(get_db)):
    """Create a new agent scoped to a project — SA only."""
    sa = _require_sa(request, db)
    if not sa:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="SA elevation required.")

    # GATE: Validate project
    project = db.query(Project).filter(Project.slug == body.project_slug).first()
    if not project:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Project not found.")

    # GATE: Unique name per project
    slug = f"{body.project_slug}-{body.name.lower().replace(' ', '-')}"
    if db.query(Agent).filter(Agent.slug == slug).first():
        from fastapi import HTTPException
        raise HTTPException(status_code=409, detail=f"Agent '{body.name}' already exists on this project.")

    # VF-307: resolve created_by — target user if provided and eligible, else the acting SA/SU.
    # Eligible = role in (super_user, user), active. Reject viewer/SA targets with 422.
    created_by_id = sa.id
    target_label = sa.display_name
    if body.target_user_id:
        target = db.query(User).filter(User.id == body.target_user_id).first()
        if not target:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Target user not found.")
        if target.role not in ("super_user", "user") or target.status != "active":
            from fastapi import HTTPException
            raise HTTPException(status_code=422,
                detail="Target user is not eligible to own an agent (must be active super_user or user).")
        created_by_id = target.id
        target_label = target.display_name

    # Generate token — shown once, then hash only
    raw_token = "vf_" + secrets.token_hex(20)  # WHY: 40 hex chars = longer token per requirement
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    prefix = raw_token[:8]

    # VF-341 §4.5 + §7.1: stamp expires_at on issuance. Eternal-token toggle
    # is a separate UI gesture (Phase 2/VF-342); for v1 every newly-minted
    # token has TTL = token.ttl_days from session_policy.
    from app.api.v2 import session_policy as _sp
    token_expires = datetime.now(timezone.utc) + timedelta(days=_sp.token_ttl_days(db))
    agent = Agent(
        id=str(uuid.uuid4()),
        name=body.name, slug=slug, description=body.description,
        status="active",
        project_id=project.id, created_by=created_by_id,
        model_type=body.model_type,
        api_token_hash=token_hash, token_prefix=prefix,
        expires_at=token_expires,
    )
    db.add(agent)

    # Auto-add to project_members for @mentions and notifications
    db.add(ProjectMember(
        project_id=project.id, agent_id=agent.id, role="write",
    ))

    # VF-303: stash plaintext under a single-use nonce so the client can
    # download the token from a same-origin URL (sidesteps SmartScreen delay
    # on blob: URLs). 5-minute TTL, consumed on first GET.
    download_nonce = _stash_token_download(db, agent.id, raw_token)

    _audit(db, "agent_created", sa.display_name, {
        "agent_name": body.name, "project": body.project_slug, "model_type": body.model_type,
        "owner_user_id": created_by_id, "owner_label": target_label,
        "created_on_behalf_of": created_by_id != sa.id,
    }, user_id=sa.id)
    db.commit()

    # WHY: Token shown once. After this response, only hash exists (plus the
    # short-lived plaintext in agent_token_downloads, gone within 5 min).
    return {
        "id": agent.id, "name": agent.name, "slug": slug,
        "token": raw_token,  # SHOWN ONCE
        "token_display": f"{prefix}...{token_hash[-3:]}",
        "project": body.project_slug,
        "download_url": f"/ui/api/agents/{agent.id}/token-file?nonce={download_nonce}",
    }


@router.post("/admin/api/agents/{agent_id}/cycle")
def cycle_agent_token(agent_id: str, request: Request, db: Session = Depends(get_db)):
    """Cycle token — same agent, new token. Old token dead immediately."""
    sa = _require_sa(request, db)
    if not sa:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="SA elevation required.")
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Agent not found.")
    if agent.status != "active":
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="Can only cycle active agents.")

    raw_token = "vf_" + secrets.token_hex(20)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    prefix = raw_token[:8]

    agent.api_token_hash = token_hash
    agent.token_prefix = prefix
    # VF-306: reset API-call counter on token cycle. New token = fresh count
    # window; UI labels the "since" date so the operator knows what they're seeing.
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    agent.api_call_count = 0
    agent.api_call_count_since = _dt.now(_tz.utc)
    # VF-341 §4.5: cycle = new TTL clock. Carry-over of original expires_at
    # would be a bug (operator cycled to refresh; old TTL is no longer the
    # right answer). Eternal carry-over is also wrong here — eternal tokens
    # don't get re-issued via the cycle path; they get explicit reissue with
    # the eternal toggle flipped (Phase 2 / VF-342 board UI).
    from app.api.v2 import session_policy as _sp
    agent.expires_at = _dt.now(_tz.utc) + _td(days=_sp.token_ttl_days(db))
    # VF-303: same nonce treatment as create flow
    download_nonce = _stash_token_download(db, agent.id, raw_token)
    _audit(db, "agent_token_cycled", sa.display_name, {
        "agent_name": agent.name, "project": agent.project_id,
    })
    db.commit()

    return {
        "id": agent.id, "name": agent.name,
        "token": raw_token,  # SHOWN ONCE
        "token_display": f"{prefix}...{token_hash[-3:]}",
        "download_url": f"/ui/api/agents/{agent.id}/token-file?nonce={download_nonce}",
    }


@router.post("/admin/api/agents/{agent_id}/revoke")
def revoke_agent(agent_id: str, request: Request, db: Session = Depends(get_db)):
    """Soft-revoke agent. Token dead. Tasks become read-only."""
    sa = _require_sa(request, db)
    if not sa:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="SA elevation required.")
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Agent not found.")
    if agent.status == "revoked":
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="Agent is already revoked.")

    agent.status = "revoked"
    agent.revoked_at = datetime.now(timezone.utc)
    agent.revoked_by = sa.id
    agent.api_token_hash = None
    agent.token_prefix = None

    # Flag project member as inactive
    pm = db.query(ProjectMember).filter(
        ProjectMember.agent_id == agent_id, ProjectMember.project_id == agent.project_id
    ).first()
    if pm:
        pm.role = "revoked"

    _audit(db, "agent_revoked", sa.display_name, {
        "agent_name": agent.name, "project": agent.project_id,
    })
    db.commit()
    return {"ok": True, "message": f"Agent {agent.name} revoked."}


@router.post("/admin/api/agents/{agent_id}/restore")
def restore_agent(agent_id: str, request: Request, db: Session = Depends(get_db)):
    """Restore a revoked agent. Must cycle for new token after."""
    sa = _require_sa(request, db)
    if not sa:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="SA elevation required.")
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Agent not found.")
    if agent.status != "revoked":
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="Agent is not revoked.")

    agent.status = "active"
    agent.revoked_at = None
    agent.revoked_by = None

    # Reactivate project member
    pm = db.query(ProjectMember).filter(
        ProjectMember.agent_id == agent_id, ProjectMember.project_id == agent.project_id
    ).first()
    if pm:
        pm.role = "write"

    _audit(db, "agent_restored", sa.display_name, {
        "agent_name": agent.name, "project": agent.project_id,
    })
    db.commit()
    return {"ok": True, "message": f"Agent {agent.name} restored. Cycle token to issue new credentials."}


# ── Agent pages in admin (VF-335: graduated to portal; old routes 301) ──

@router.get("/admin/agents/new", response_class=HTMLResponse)
def admin_agent_new(request: Request):
    # VF-335: new-agent form is now the master-detail __new__ panel.
    return RedirectResponse(url="/admin/portal/administration/agents/__new__", status_code=301)


@router.get("/admin/agents/{agent_id}", response_class=HTMLResponse)
def admin_agent_detail(agent_id: str, request: Request):
    # VF-335: agent detail graduated to portal master-detail (VF-329).
    return RedirectResponse(url=f"/admin/portal/administration/agents/{agent_id}", status_code=301)


@router.get("/admin/api/agents/{agent_id}/onboard-prompt")
def get_onboard_prompt(agent_id: str, request: Request, db: Session = Depends(get_db)):
    """Generate onboarding prompt dynamically from contract + agent context."""
    # WHY: Single source of truth — prompt derives from contract, not a separate template
    sa = _require_sa(request, db)
    if not sa:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="SA elevation required.")
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Agent not found.")
    proj = db.query(Project).filter(Project.id == agent.project_id).first() if agent.project_id else None
    # WHY: derive from request, not hardcoded setting — same fix as contract.py (VF-266 session)
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    base = f"{scheme}://{host}"

    prompt = f"""You are connecting to a VibeForge+ project board for collaborative development.

Board URL: {base}
Project: {proj.name + ' (' + proj.slug + ')' if proj else 'unknown'}
Agent: {agent.name} ({agent.slug})
Model: {agent.model_type or 'unknown'}

== BOOTSTRAP ==

1. If .agent-config exists in the project root, source it. Otherwise:
   - Read the agent-token.txt file the human placed for you.
   - Create .agent-config with: VIBEFORGE_API={base}/api/v2 VIBEFORGE_TOKEN=<token> VIBEFORGE_PROJECT={proj.slug if proj else '{project_slug}'}
   - Add .agent-config AND agent-token.txt to .gitignore.
   - Delete the agent-token.txt file (ONLY that — NOT .agent-config). Never display tokens.

2. Verify identity: source .agent-config && curl -sL -H "Authorization: Bearer $VIBEFORGE_TOKEN" "$VIBEFORGE_API/me"

3. Fetch your project contract: curl -sL -H "Authorization: Bearer $VIBEFORGE_TOKEN" "{base}/agentnotes/{proj.slug if proj else '$VIBEFORGE_PROJECT'}"
   This contains all API endpoints, rules, and workflows. Follow it.

4. Write AGENTS.md (or CLAUDE.md) from the agents_md_template field in the contract.

5. Run checktasks — fetch tasks and begin work. If no tasks exist, you are the onboarding partner."""

    return {"prompt": prompt}


@router.get("/admin/auditlog", response_class=HTMLResponse)
def admin_audit_log(request: Request):
    # VF-335: audit log graduated to /admin/portal/health/audit. Query-param
    # filters (actor_user_id / actor_agent_id / project_id) are passed through
    # in the URL so future portal-side filter parity can honour bookmarks.
    qs = request.url.query
    target = "/admin/portal/health/audit"
    if qs:
        target = target + "?" + qs
    return RedirectResponse(url=target, status_code=301)


class CreateUserBody(BaseModel):
    username: str
    display_name: str
    email: str
    password: str
    role: str = "user"  # user, super_user, or viewer
    # VF-329 F7: optional vanity fields. The OLD new-user form captured these
    # but the endpoint silently dropped them. Now persisted; future UI surfaces
    # them in the user-detail Profile panel.
    nickname: str | None = None
    title: str | None = None
    display_role: str | None = None


@router.post("/admin/api/users", status_code=201)
def create_user(body: CreateUserBody, request: Request, db: Session = Depends(get_db)):
    sa = _require_sa(request, db)
    if not sa:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="SA elevation required.")

    # GATE: Validate inputs
    un_err = _validate_username(body.username)
    if un_err:
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail=un_err)
    pw_err = _validate_password(body.password)
    if pw_err:
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail=pw_err)
    # RULE: Cannot create another super_admin
    if body.role == "super_admin":
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="Cannot create another Super Admin.")
    if body.role not in ("viewer", "user", "super_user"):
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail=f"Invalid role: {body.role}")

    # Check uniqueness
    if db.query(User).filter(User.username == body.username).first():
        from fastapi import HTTPException
        raise HTTPException(status_code=409, detail="Username already taken.")
    # VF-334: normalise blank/whitespace; reject empty (column nullable=False); uniqueness only when non-empty.
    email_normalized = (body.email or "").strip()
    if not email_normalized:
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="Email is required.")
    if db.query(User).filter(User.email == email_normalized).first():
        from fastapi import HTTPException
        raise HTTPException(status_code=409, detail="Email already registered.")

    user = User(
        username=body.username,
        email=email_normalized,
        display_name=body.display_name,
        role=body.role,
        password_hash=_bcrypt.hashpw(body.password.encode(), _bcrypt.gensalt()).decode(),
        # VF-329 F7: persist vanity fields if provided (otherwise NULL).
        nickname=(body.nickname or None),
        title=(body.title or None),
        display_role=(body.display_role or None),
    )
    db.add(user)
    _audit(db, "user_created", sa.display_name, {
        "username": body.username, "role": body.role, "display_name": body.display_name,
    })
    db.commit()
    db.refresh(user)
    return {"id": user.id, "username": user.username, "display_name": user.display_name, "role": user.role}


@router.get("/admin/api/users")
def list_users(request: Request, db: Session = Depends(get_db)):
    sa = _require_sa(request, db)
    if not sa:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="SA elevation required.")
    users = db.query(User).order_by(User.role.desc(), User.display_name).all()
    return [{
        "id": u.id, "username": u.username, "display_name": u.display_name,
        "email": u.email, "role": u.role, "status": u.status,
        "display_role": u.display_role, "title": u.title, "nickname": u.nickname,
        "deleted_at": u.deleted_at.isoformat() if u.deleted_at else None,
        "created_at": u.created_at.isoformat(),
    } for u in users]


# ── VF-286 Phase 2b: admin-side memberships (SA-only) ───────────────────────

@router.get("/admin/api/users/{user_id}/memberships")
def user_memberships(user_id: str, request: Request, db: Session = Depends(get_db)):
    """List all project memberships for a user, plus the projects they own.
    SA-only. Powers the Memberships section on /admin/users/{user_id}."""
    sa = _require_sa(request, db)
    if not sa:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="SA elevation required.")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="User not found.")

    # Explicit ProjectMember rows
    rows = (
        db.query(ProjectMember, Project)
        .join(Project, ProjectMember.project_id == Project.id)
        .filter(ProjectMember.user_id == user_id)
        .order_by(Project.name)
        .all()
    )
    result = []
    seen_project_ids = set()
    for m, p in rows:
        seen_project_ids.add(p.id)
        result.append({
            "member_id": m.id,
            "project_id": p.id,
            "project_slug": p.slug,
            "project_name": p.name,
            "project_status": p.status,
            "role": m.role,
            "is_owner": p.owner_id == user_id,
        })

    # Owned projects without an explicit member row (legacy / backfill gap)
    owned_only = (
        db.query(Project)
        .filter(Project.owner_id == user_id, ~Project.id.in_(seen_project_ids))
        .order_by(Project.name)
        .all()
        if seen_project_ids else
        db.query(Project).filter(Project.owner_id == user_id).order_by(Project.name).all()
    )
    for p in owned_only:
        result.append({
            "member_id": None,      # no ProjectMember row — owner-implicit only
            "project_id": p.id,
            "project_slug": p.slug,
            "project_name": p.name,
            "project_status": p.status,
            "role": "owner",
            "is_owner": True,
        })

    # Stable sort: active projects first, then by name
    result.sort(key=lambda r: (0 if r["project_status"] == "active" else 1, r["project_name"].lower()))
    return result


class TransferOwnerBody(BaseModel):
    new_owner_user_id: str


@router.post("/admin/api/projects/{slug}/transfer-owner")
def transfer_project_owner(slug: str, body: TransferOwnerBody, request: Request, db: Session = Depends(get_db)):
    """VF-308: SA-only owner transfer. v1 scope decisions: SA-only, prior owner keeps
    their PM role unchanged, agents untouched. See 0-MD/0-Documentation/public/identity-roles.md
    §8. Lives under /admin/api/ because the SA session cookie is scoped to /admin/."""
    sa = _require_sa(request, db)
    if not sa:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="SA elevation required.")

    project = db.query(Project).filter(Project.slug == slug).first()
    if not project:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Project not found.")
    if project.status != "active":
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="Only active projects can transfer ownership.")

    prior_owner_id = project.owner_id
    prior_owner = db.query(User).filter(User.id == prior_owner_id).first() if prior_owner_id else None
    prior_owner_name = prior_owner.display_name if prior_owner else None

    new_owner = db.query(User).filter(User.id == body.new_owner_user_id).first()
    if not new_owner:
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="New owner not found.")
    if new_owner.status != "active":
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="New owner must be an active user.")
    if new_owner.role == "super_admin":
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="Super Admin cannot own a project.")
    if new_owner.role == "viewer":
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="Viewers cannot own a project.")
    if new_owner.id == prior_owner_id:
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="New owner is already the current owner.")

    # RULE: new owner must be an acknowledged project member. Upgrade their role to admin
    # if it's write/read, so ownership implies working admin rights.
    new_owner_pm = db.query(ProjectMember).filter(
        ProjectMember.project_id == project.id,
        ProjectMember.user_id == new_owner.id,
    ).first()
    if not new_owner_pm:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=422,
            detail="New owner must already be a project member. Add them to the project first, then transfer.",
        )
    role_before = new_owner_pm.role
    if new_owner_pm.role != "admin":
        new_owner_pm.role = "admin"

    # Flip ownership. Prior owner's ProjectMember row stays as-is per v1 design decision.
    project.owner_id = new_owner.id

    _audit(
        db,
        "project_owner_transferred",
        sa.display_name,
        {
            "project_slug": project.slug,
            "project_name": project.name,
            "prior_owner_id": prior_owner_id,
            "prior_owner_name": prior_owner_name,
            "new_owner_id": new_owner.id,
            "new_owner_name": new_owner.display_name,
            "new_owner_role_before": role_before,
            "new_owner_role_after": new_owner_pm.role,
        },
        user_id=sa.id,
    )
    db.commit()
    return {
        "ok": True,
        "project_slug": project.slug,
        "prior_owner": {"id": prior_owner_id, "display_name": prior_owner_name},
        "new_owner": {"id": new_owner.id, "display_name": new_owner.display_name, "role": new_owner_pm.role},
    }


@router.get("/admin/api/projects/{slug}/eligible-new-owners")
def eligible_new_owners(slug: str, request: Request, db: Session = Depends(get_db)):
    """VF-308: list active human project members who could become the new owner.
    Excludes current owner, SA, viewers, and any non-active users. Powers the
    Transfer dropdown on /admin/users/{id}."""
    sa = _require_sa(request, db)
    if not sa:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="SA elevation required.")
    project = db.query(Project).filter(Project.slug == slug).first()
    if not project:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Project not found.")

    rows = (
        db.query(ProjectMember, User)
        .join(User, ProjectMember.user_id == User.id)
        .filter(
            ProjectMember.project_id == project.id,
            User.status == "active",
            User.role.in_(("user", "super_user")),
            User.id != project.owner_id,
        )
        .order_by(User.display_name)
        .all()
    )
    return [
        {
            "user_id": u.id,
            "display_name": u.display_name,
            "username": u.username,
            "email": u.email,
            "role": u.role,
            "current_project_role": m.role,
        }
        for m, u in rows
    ]


@router.get("/admin/api/users/{user_id}/addable-projects")
def user_addable_projects(user_id: str, request: Request, db: Session = Depends(get_db)):
    """Active projects this user is NOT yet a member of. Powers the picker in add-project-to-user mode."""
    sa = _require_sa(request, db)
    if not sa:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="SA elevation required.")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="User not found.")

    existing_project_ids = {
        pm.project_id for pm in db.query(ProjectMember).filter(
            ProjectMember.user_id == user_id,
        ).all()
    }
    # Also exclude projects where the user is the owner (they already have implicit admin)
    owned = {p.id for p in db.query(Project).filter(Project.owner_id == user_id).all()}
    excluded = existing_project_ids | owned

    query = db.query(Project).filter(Project.status == "active").order_by(Project.name)
    projects = query.all()
    return [
        {
            "id": p.id,
            "slug": p.slug,
            "name": p.name,
            "owner_id": p.owner_id,
        }
        for p in projects if p.id not in excluded
    ]


@router.post("/admin/api/users/{user_id}/soft-delete")
def soft_delete_user(user_id: str, request: Request, db: Session = Depends(get_db)):
    sa = _require_sa(request, db)
    if not sa:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="SA elevation required.")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="User not found.")
    # RULE: Cannot delete super_admin
    if user.role == "super_admin":
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="Cannot delete Super Admin. Ever.")
    # RULE: Cannot soft-delete the last active super_user (would strand admin functions)
    if user.role == "super_user":
        active_su_count = db.query(User).filter(
            User.role == "super_user", User.status == "active"
        ).count()
        if active_su_count <= 1:
            from fastapi import HTTPException
            raise HTTPException(status_code=422, detail="Cannot delete the last active Super User. Promote another user to super_user first.")
    if user.status == "deleted":
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="User is already deleted.")

    # VF-308: RULE — cannot soft-delete a user who owns active projects. Transfer ownership first.
    # Silent cascade would orphan projects. See identity-roles.md §2 (User) + VF-308 ticket.
    owned_active = (
        db.query(Project)
        .filter(Project.owner_id == user.id, Project.status == "active")
        .order_by(Project.name)
        .all()
    )
    if owned_active:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=422,
            detail={
                "code": "OWNED_PROJECTS_BLOCKING",
                "message": f"{user.display_name} owns {len(owned_active)} active project(s). Transfer ownership before deleting.",
                "projects": [
                    {"slug": p.slug, "name": p.name, "id": p.id}
                    for p in owned_active
                ],
            },
        )

    # VF-315: cascade-revoke the user's active agents across ALL projects. Same rationale
    # as the per-project cascade in members.remove_member — agents derive trust from the
    # user's access; when the user is deleted, the tools go with them.
    # See user-agent-model.md §4.1 (v1.1).
    now = datetime.now(timezone.utc)
    active_agents = db.query(Agent).filter(
        Agent.created_by == user.id,
        Agent.status == "active",
    ).all()
    cascaded = []
    for ag in active_agents:
        ag.status = "revoked"
        ag.revoked_at = now
        ag.revoked_by = sa.id
        ag.api_token_hash = None
        ag.token_prefix = None
        cascaded.append({"id": ag.id, "name": ag.name, "project_id": ag.project_id})
        db.add(ActivityEvent(
            id=str(uuid.uuid4()),
            project_id=ag.project_id, task_id=None,
            actor_type="human", actor_user_id=sa.id,
            action="agent_revoked",
            details=json.dumps({
                "agent_name": ag.name,
                "agent_slug": ag.slug,
                "actor": sa.display_name,
                "reason": "cascade_user_soft_deleted",
                "removed_user_id": user.id,
                "removed_user_label": user.display_name,
            }),
        ))

    user.status = "deleted"
    user.deleted_at = now
    user.deleted_by = sa.id
    _audit(db, "user_soft_deleted", sa.display_name, {
        "username": user.username, "display_name": user.display_name,
        "agents_cascaded": len(cascaded),
    })
    db.commit()
    return {
        "ok": True,
        "message": f"User {user.display_name} soft-deleted.",
        "agents_cascade_revoked": len(cascaded),
    }


@router.post("/admin/api/users/{user_id}/suspend")
def suspend_user(user_id: str, request: Request, db: Session = Depends(get_db)):
    """VF-312: toggle user status to suspended. Blocks login with a contact-admin message.
    Non-destructive — memberships, agents, history all preserved. See identity-roles.md §2."""
    sa = _require_sa(request, db)
    if not sa:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="SA elevation required.")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="User not found.")
    if user.role == "super_admin":
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="Cannot suspend Super Admin.")
    if user.role == "super_user":
        active_su_count = db.query(User).filter(
            User.role == "super_user", User.status == "active"
        ).count()
        if active_su_count <= 1:
            from fastapi import HTTPException
            raise HTTPException(status_code=422, detail="Cannot suspend the last active Super User.")
    if user.status == "deleted":
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="User is deleted; restore first, then suspend if needed.")
    if user.status == "suspended":
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="User is already suspended.")

    user.status = "suspended"
    _audit(db, "user_suspended", sa.display_name, {
        "username": user.username, "display_name": user.display_name,
    }, user_id=sa.id)
    db.commit()
    return {"ok": True, "message": f"User {user.display_name} suspended."}


@router.post("/admin/api/users/{user_id}/unsuspend")
def unsuspend_user(user_id: str, request: Request, db: Session = Depends(get_db)):
    """VF-312: toggle suspended user back to active."""
    sa = _require_sa(request, db)
    if not sa:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="SA elevation required.")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="User not found.")
    if user.status != "suspended":
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="User is not suspended.")

    user.status = "active"
    _audit(db, "user_unsuspended", sa.display_name, {
        "username": user.username, "display_name": user.display_name,
    }, user_id=sa.id)
    db.commit()
    return {"ok": True, "message": f"User {user.display_name} re-enabled."}


@router.post("/admin/api/users/{user_id}/restore")
def restore_user(user_id: str, request: Request, db: Session = Depends(get_db)):
    # RULE: Only SA can restore
    sa = _require_sa(request, db)
    if not sa:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="SA elevation required.")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="User not found.")
    if user.status != "deleted":
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="User is not deleted.")

    user.status = "active"
    user.deleted_at = None
    user.deleted_by = None
    _audit(db, "user_restored", sa.display_name, {
        "username": user.username, "display_name": user.display_name,
    })
    db.commit()
    return {"ok": True, "message": f"User {user.display_name} restored."}


@router.post("/admin/api/users/{user_id}/reset-password")
def reset_user_password(user_id: str, request: Request, db: Session = Depends(get_db)):
    sa = _require_sa(request, db)
    if not sa:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="SA elevation required.")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="User not found.")

    # Generate temp password
    import secrets, string
    alphabet = string.ascii_letters + string.digits + "!@#$%"
    temp_pw = ''.join(secrets.choice(alphabet) for _ in range(14))
    user.password_hash = _bcrypt.hashpw(temp_pw.encode(), _bcrypt.gensalt()).decode()
    user.must_change_password = True
    # WHY: Force password change on next login after admin reset
    _audit(db, "user_password_reset", sa.display_name, {
        "username": user.username, "display_name": user.display_name,
    })
    db.commit()
    return {"ok": True, "temp_password": temp_pw, "message": "Password reset. User must change on next login."}


class ChangeRoleBody(BaseModel):
    role: str


@router.post("/admin/api/users/{user_id}/change-role")
def change_user_role(user_id: str, body: ChangeRoleBody, request: Request, db: Session = Depends(get_db)):
    sa = _require_sa(request, db)
    if not sa:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="SA elevation required.")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="User not found.")
    # RULE: Cannot change SA role
    if user.role == "super_admin":
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="Cannot change Super Admin role.")
    # RULE: Cannot promote to super_admin
    if body.role == "super_admin":
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="Cannot create another Super Admin.")
    if body.role not in ("viewer", "user", "super_user"):
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail=f"Invalid role: {body.role}")
    # RULE: Cannot demote the last active super_user (would strand admin functions until SA recovery)
    if user.role == "super_user" and body.role != "super_user":
        active_su_count = db.query(User).filter(
            User.role == "super_user", User.status == "active"
        ).count()
        if active_su_count <= 1:
            from fastapi import HTTPException
            raise HTTPException(status_code=422, detail="Cannot demote the last active Super User. Promote another user to super_user first.")

    old_role = user.role
    user.role = body.role
    _audit(db, "user_role_changed", sa.display_name, {
        "username": user.username, "from": old_role, "to": body.role,
    })

    # VF-328: when demoting to viewer, auto-downgrade any existing project memberships
    # to read so stored state matches the global-role enforcement (projects.py:80).
    # Each downgrade audited individually for an honest trail.
    downgraded = []
    if body.role == "viewer" and old_role != "viewer":
        from app.models.project_member import ProjectMember
        memberships = db.query(ProjectMember).filter(
            ProjectMember.user_id == user.id,
            ProjectMember.role.in_(("write", "admin")),
        ).all()
        for m in memberships:
            prev = m.role
            m.role = "read"
            downgraded.append({"project_id": m.project_id, "from": prev})
            _audit(db, "project_member_role_downgraded", sa.display_name, {
                "username": user.username,
                "project_id": m.project_id,
                "member_id": m.id,
                "from": prev,
                "to": "read",
                "reason": "user demoted to viewer (global role)",
            })

    db.commit()
    msg = f"{user.display_name} role changed: {old_role} → {body.role}"
    if downgraded:
        msg += f". {len(downgraded)} project membership(s) downgraded to read."
    return {"ok": True, "message": msg, "memberships_downgraded": len(downgraded)}
