import json
import uuid
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, Request, Query, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
import bcrypt as _bcrypt

from app.db.session import get_db
from app.models.project import Project
from app.models.task import Task
from app.models.milestone import Milestone
from app.models.phase import Phase
from app.models.user import User
from app.models.session import UserSession
from app.models.activity import ActivityEvent

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

import re as _re
import html as _html

SESSION_COOKIE = "vf_session"


def _render_framing_html(text: str) -> str:
    """Tiny markdown→HTML for the wizard framing-gate scrollable mirror.
    Single source of truth: FRAMING_TEXT in onboard.py. Renders headers,
    paragraphs, ordered lists, bold/italic/code inline. No deps; no markdown
    library available in container.
    """
    lines = text.split("\n")
    out: list[str] = []
    in_list = False
    para_buf: list[str] = []

    def _inline(s: str) -> str:
        # s is already html-escaped at line level
        s = _re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = _re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = _re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", s)
        return s

    def _flush_para():
        if para_buf:
            joined = " ".join(para_buf).strip()
            if joined:
                out.append(f"<p>{_inline(joined)}</p>")
            para_buf.clear()

    def _close_list():
        nonlocal in_list
        if in_list:
            out.append("</ol>")
            in_list = False

    for raw in lines:
        line = _html.escape(raw.rstrip())
        if not line:
            _flush_para()
            _close_list()
            continue
        if line.startswith("# "):
            _flush_para(); _close_list()
            out.append(f"<h2>{_inline(line[2:].strip())}</h2>")
            continue
        if line.startswith("## "):
            _flush_para(); _close_list()
            out.append(f"<h3>{_inline(line[3:].strip())}</h3>")
            continue
        m = _re.match(r"^(\d+)\.\s+(.*)$", line)
        if m:
            _flush_para()
            if not in_list:
                out.append("<ol>")
                in_list = True
            out.append(f"<li>{_inline(m.group(2))}</li>")
            continue
        _close_list()
        para_buf.append(line)

    _flush_para()
    _close_list()
    return "\n".join(out)

# ── Validation helpers ──

ILLEGAL_PASSWORD_CHARS = set('<>{}()[]|\\`~')

def _validate_password(password: str) -> str | None:
    """Validate password complexity. Returns error message or None if valid."""
    if len(password) < 12:
        return "Password must be at least 12 characters."
    if not _re.search(r'[A-Z]', password):
        return "Password must contain at least one uppercase letter."
    if not _re.search(r'[a-z]', password):
        return "Password must contain at least one lowercase letter."
    if not _re.search(r'[0-9]', password):
        return "Password must contain at least one number."
    if not _re.search(r'[!@#$%^&*\-_=+.,;:?/]', password):
        return "Password must contain at least one symbol (!@#$%^&*-_=+.,;:?/)."
    if any(c in ILLEGAL_PASSWORD_CHARS for c in password):
        return f"Password contains illegal characters. Avoid: {''.join(sorted(ILLEGAL_PASSWORD_CHARS))}"
    return None

def _validate_username(username: str) -> str | None:
    """Validate username. Returns error message or None if valid."""
    if not username or len(username) > 10:
        return "Username must be 1-10 characters."
    if ' ' in username:
        return "Username cannot contain spaces."
    if not _re.match(r'^[a-z0-9_]+$', username):
        return "Username must be lowercase letters, numbers, or underscores only."
    return None
SA_COOKIE = "vf_sa_session"
SESSION_HOURS = 24
SA_SESSION_MINUTES = 30


def _get_session_user(request: Request, db: Session) -> User | None:
    """Get authenticated user from session cookie. Bumps last_activity_at as a heartbeat.

    VF-341: filter now also excludes soft-revoked rows (revoked_at IS NULL).
    Soft-revoke is the supported revoke path; auth check stays single-WHERE
    against expires_at + revoked_at per SESSION-LIFECYCLE-PROPOSAL §4.1.
    """
    session_id = request.cookies.get(SESSION_COOKIE)
    if not session_id:
        return None
    sess = db.query(UserSession).filter(
        UserSession.id == session_id,
        UserSession.session_type == "user",
        UserSession.expires_at > datetime.now(timezone.utc),
        UserSession.revoked_at.is_(None),
    ).first()
    if not sess:
        return None
    user = db.query(User).filter(User.id == sess.user_id, User.status == "active").first()
    # WHY: Heartbeat for session activity tracking — used by health page
    if user:
        try:
            sess.last_activity_at = datetime.now(timezone.utc)
            db.commit()
        except Exception:
            db.rollback()
    return user


def _require_auth(request: Request, db: Session) -> User:
    """Get user or None — let route decide whether to redirect."""
    return _get_session_user(request, db)


def _client_info(request: Request) -> dict:
    """Extract client info from request for audit logging."""
    # WHY: Capture IP, browser, device for security audit trail
    ua = request.headers.get("user-agent", "")[:300]
    ip = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
    return {"ip": ip, "user_agent": ua}


def _audit_login(db: Session, action: str, details: dict):
    """Log a login-related audit event (system-level, no project)."""
    db.add(ActivityEvent(
        project_id=None, task_id=None,
        actor_type="system", action=action,
        details=json.dumps(details),
    ))
    db.commit()


def _require_login(request: Request, db: Session) -> User | None:
    """Check session — return user or None (caller redirects to login).
    Returns None if must_change_password set (caller redirects to change-password)."""
    user = _get_session_user(request, db)
    if user and user.must_change_password:
        # WHY: Force password change before accessing any board page
        # Store flag so caller can redirect appropriately
        request.state.must_change_password = True
        return None
    return user


def _create_session(user: User, db: Session, request: Request, session_type: str = "user") -> str:
    """Create a session record, return session ID.

    VF-341: now reads slide_window_hours / elevation_ttl_minutes from
    SystemSetting (via session_policy) instead of the hardcoded constants
    above; the constants stay as fallback only and are no longer the
    source of truth. Also enforces concurrent_cap per (user, session_type)
    by auto-revoking the oldest active session when the cap would be hit.
    """
    from app.api.v2 import session_policy as _sp
    session_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    if session_type == "sa":
        expires = now + timedelta(minutes=_sp.elevation_ttl_minutes(db))
    else:
        expires = now + timedelta(hours=_sp.slide_window_hours(db))
    # VF-341 §4.4 + §6.5: enforce concurrent cap before insert. Per-type so
    # an SA elevation doesn't push the operator's board sessions over.
    _sp.enforce_concurrent_cap(db, user.id, session_type)
    sess = UserSession(
        id=session_id, user_id=user.id, session_type=session_type,
        expires_at=expires,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent", "")[:500],
    )
    db.add(sess)
    db.commit()
    return session_id

def _milestone_names(db: Session, project_id: str) -> dict[str, str]:
    """Read milestone label→name mapping from DB."""
    rows = db.query(Milestone.label, Milestone.name).filter(
        Milestone.project_id == project_id
    ).all()
    return {r[0]: r[1] for r in rows}


def _sidebar_projects(db: Session, user=None):
    """Return projects visible to the user, with per-user _pinned/_pin_order attached.

    GATE: SU/SA see all projects. Regular users see only projects they own
    or are a member of via project_members. Anonymous (no user) sees nothing.
    """
    q = db.query(Project).filter(Project.status == "active")
    if not user:
        return []
    if user.role == "super_user":
        result = q.order_by(Project.name).all()
    else:
        from app.models.project_member import ProjectMember as _PM
        member_ids = [m.project_id for m in db.query(_PM.project_id).filter(_PM.user_id == user.id).all()]
        q = q.filter((Project.owner_id == user.id) | (Project.id.in_(member_ids)))
        result = q.order_by(Project.name).all()
    # Attach per-user pin state
    from app.models.user_project_pin import UserProjectPin
    pin_map = {pid: po for pid, po in db.query(UserProjectPin.project_id, UserProjectPin.pin_order).filter(UserProjectPin.user_id == user.id).all()}
    for p in result:
        p._pinned = p.id in pin_map
        p._pin_order = pin_map.get(p.id, 0)
    return result


def _parse_ua(ua: str) -> str:
    """Turn a User-Agent string into 'Browser on OS'."""
    if not ua:
        return "unknown browser"
    ua_l = ua.lower()
    # Browser
    if "edg/" in ua_l:
        browser = "Edge"
    elif "chrome/" in ua_l and "chromium" not in ua_l:
        browser = "Chrome"
    elif "firefox/" in ua_l:
        browser = "Firefox"
    elif "safari/" in ua_l and "chrome" not in ua_l:
        browser = "Safari"
    elif "curl/" in ua_l:
        browser = "curl"
    else:
        browser = "browser"
    # OS
    if "windows nt" in ua_l:
        os_name = "Windows"
    elif "iphone" in ua_l or "ipad" in ua_l:
        os_name = "iOS"
    elif "android" in ua_l:
        os_name = "Android"
    elif "mac os" in ua_l:
        os_name = "macOS"
    elif "linux" in ua_l:
        os_name = "Linux"
    else:
        os_name = ""
    return f"{browser} on {os_name}".strip(" on")


def _strip_owner_prefix(label: str) -> str:
    """human:Parvez Khan -> Parvez Khan, agent:test-agent -> test-agent."""
    if not label:
        return ""
    if ":" in label:
        return label.split(":", 1)[1].strip()
    return label


def _humanize_event(action: str, details: dict) -> str:
    """Render an activity event as a human sentence. Returns just the predicate
    (what happened) — the actor name is rendered separately by the template."""
    if not isinstance(details, dict):
        details = {}
    d = details

    # ── Auth events ──
    if action in ("login_success", "sa_login_success"):
        ua = _parse_ua(d.get("user_agent", ""))
        ip = d.get("ip", "")
        prefix = "elevated to Super Admin" if action == "sa_login_success" else "signed in"
        bits = [prefix, f"from {ua}" if ua else ""]
        if ip:
            bits.append(f"({ip})")
        return " ".join(b for b in bits if b)
    if action in ("login_failed", "sa_login_failed"):
        ua = _parse_ua(d.get("user_agent", ""))
        ip = d.get("ip", "")
        attempted = d.get("attempted_email") or d.get("attempted_username") or "unknown"
        bits = [f"failed sign-in attempt as {attempted}"]
        if ua:
            bits.append(f"from {ua}")
        if ip:
            bits.append(f"({ip})")
        return " ".join(bits)

    # ── Task events ──
    if action == "status_changed":
        s_from = (d.get("from") or "").replace("_", " ")
        s_to = (d.get("to") or "").replace("_", " ")
        reason = d.get("reason", "")
        base = f"moved task from {s_from} to {s_to}"
        return f"{base} — {reason}" if reason else base
    if action == "owner_changed":
        return f"reassigned task from {_strip_owner_prefix(d.get('from', '?'))} to {_strip_owner_prefix(d.get('to', '?'))}"
    if action == "phase_changed":
        reason = d.get("reason", "")
        base = f"moved task from phase {d.get('from', '?')} to {d.get('to', '?')}"
        return f"{base} — {reason}" if reason else base
    if action == "title_changed":
        return f"renamed task from \"{d.get('from', '?')}\" to \"{d.get('to', '?')}\""
    if action == "priority_changed":
        return f"changed priority from {d.get('from', '?')} to {d.get('to', '?')}"
    if action == "description_changed":
        return "updated task description"
    if action == "blocked_by_changed":
        if d.get("to"):
            return f"blocked task by {d.get('to')}"
        return "cleared blocked-by"
    if action == "start_date_changed":
        return f"set start date to {d.get('to') or 'none'}"
    if action == "due_date_changed":
        return f"set due date to {d.get('to') or 'none'}"
    if action == "task_created":
        return f"created task \"{d.get('title', '?')}\""

    # ── Note events ──
    if action == "note_superseded":
        nn = d.get("note_num", "?")
        return f"superseded note #{nn}: {d.get('reason', '')}"
    if action == "note_revert_supersede":
        nn = d.get("note_num", "?")
        return f"reverted supersede on note #{nn}: {d.get('reason', '')}"

    # ── Project lifecycle ──
    if action == "project_renamed":
        return f"renamed project from \"{d.get('from', '?')}\" to \"{d.get('to', '?')}\""
    if action == "project_archived":
        return f"archived project ({d.get('type', 'archived')}: {d.get('reason', '')})"
    if action == "project_completed":
        return f"marked project complete: {d.get('summary', '')}"
    if action == "project_reopened":
        return f"reopened project: {d.get('reopen_reason', d.get('reason', ''))}"
    if action == "resume_updated":
        return "updated project resume"

    # ── Agent lifecycle ──
    if action == "agent_created":
        return f"created agent {d.get('agent_name', '?')} on {d.get('project', '?')}"
    if action == "agent_revoked":
        return f"revoked agent {d.get('agent_name', '?')}"
    if action == "agent_restored":
        return f"restored agent {d.get('agent_name', '?')}"
    if action == "agent_token_cycled":
        return f"cycled token for {d.get('agent_name', '?')}"

    # ── User admin ──
    if action == "user_created":
        return f"created user {d.get('username', d.get('display_name', '?'))} ({d.get('role', '?')})"
    if action == "user_soft_deleted":
        return f"soft-deleted user {d.get('username', '?')}"
    if action == "user_restored":
        return f"restored user {d.get('username', '?')}"
    if action == "user_password_reset":
        return f"reset password for {d.get('username', '?')}"
    if action == "user_role_changed":
        return f"changed role for {d.get('username', '?')} from {d.get('from', '?')} to {d.get('to', '?')}"
    if action == "user_updated":
        return f"updated profile for {d.get('username', '?')}"

    # ── Fallback: pretty-print action with key details ──
    pretty = action.replace("_", " ")
    interesting = {k: v for k, v in d.items() if k not in ("actor", "user_agent", "ip", "body_preview") and v}
    if interesting:
        bits = ", ".join(f"{k}: {v}" for k, v in interesting.items())
        return f"{pretty} — {bits}"
    return pretty


def _user_can_access_project(user, project, db: Session) -> bool:
    """GATE: Membership check. SU/SA always pass. Owners always pass. Members pass."""
    if not user or not project:
        return False
    if user.role == "super_user":
        return True
    if project.owner_id == user.id:
        return True
    from app.models.project_member import ProjectMember as _PM
    return db.query(_PM).filter(
        _PM.project_id == project.id,
        _PM.user_id == user.id,
    ).first() is not None


# ── Login / Logout ──

class LoginBody(BaseModel):
    email: str
    password: str


@router.get("/ui/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    # Already logged in? Redirect to home
    user = _get_session_user(request, db)
    if user:
        return RedirectResponse(url="/ui/", status_code=302)
    return templates.TemplateResponse("ui/login.html", {"request": request})


@router.post("/ui/login")
def login_submit(body: LoginBody, request: Request, response: Response, db: Session = Depends(get_db)):
    # Find user by username (primary), email (secondary)
    login_input = body.email.strip().lower()
    user = db.query(User).filter(
        (User.username == login_input) | (User.email == login_input)
    ).first()
    ci = _client_info(request)
    if not user:
        _audit_login(db, "login_failed", {**ci, "username": login_input, "reason": "user_not_found"})
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if user.status != "active":
        # VF-312: status-aware message so suspended users get a clear instruction, not a vague
        # Account is not active line. Deleted keeps the generic framing (same surface, the
        # operator cannot currently restore mid-flight anyway).
        if user.status == "suspended":
            reason = "account_suspended"
            detail_msg = "Your account is disabled. Contact your admin."
        else:
            reason = "account_inactive"
            detail_msg = "Account is not active."
        _audit_login(db, "login_failed", {**ci, "username": login_input, "reason": reason})
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail=detail_msg)
    if not _bcrypt.checkpw(body.password.encode(), user.password_hash.encode()):
        _audit_login(db, "login_failed", {**ci, "username": login_input, "reason": "bad_password"})
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # GATE (VF-309): Super Admin cannot log into the board — admin portal only.
    # See 0-MD/0-Documentation/public/identity-roles.md §2 / §5.1.
    # VF-317: return a structured detail so the frontend can render a clickable CTA
    # chip ("Open Admin Panel →") instead of a plain error message.
    if user.role == "super_admin":
        _audit_login(db, "login_blocked_sa", {**ci, "username": user.username, "display_name": user.display_name})
        from fastapi import HTTPException
        raise HTTPException(
            status_code=403,
            detail={
                "code": "SA_LOGIN_BLOCKED",
                "message": "The board is for collaborators (Super Users, Users, Viewers). Super Admin lives on the admin console.",
                "admin_login_url": "/admin/login",
            },
        )

    _audit_login(db, "login_success", {**ci, "username": user.username, "display_name": user.display_name, "role": user.role})
    session_id = _create_session(user, db, request, "user")
    # GATE: Force password change if flagged by admin reset
    redirect_to = "/ui/"
    if user.must_change_password:
        redirect_to = "/ui/must-change-password"
    response = JSONResponse(content={"ok": True, "user": user.display_name, "redirect": redirect_to})
    response.set_cookie(
        key=SESSION_COOKIE, value=session_id,
        httponly=True, samesite="lax", path="/",
        max_age=SESSION_HOURS * 3600,
    )
    return response


class ChangePasswordBody(BaseModel):
    current_password: str
    new_password: str


@router.post("/ui/change-password")
def change_password(body: ChangePasswordBody, request: Request, db: Session = Depends(get_db)):
    user = _get_session_user(request, db)
    if not user:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not _bcrypt.checkpw(body.current_password.encode(), user.password_hash.encode()):
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    pw_error = _validate_password(body.new_password)
    if pw_error:
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail=pw_error)
    user.password_hash = _bcrypt.hashpw(body.new_password.encode(), _bcrypt.gensalt()).decode()
    # WHY: Clear forced change flag after successful password update
    user.must_change_password = False
    db.commit()
    return {"ok": True, "message": "Password changed"}


@router.get("/ui/must-change-password", response_class=HTMLResponse)
def must_change_password_page(request: Request, db: Session = Depends(get_db)):
    user = _get_session_user(request, db)
    if not user:
        return RedirectResponse(url="/ui/login", status_code=302)
    if not user.must_change_password:
        return RedirectResponse(url="/ui/", status_code=302)
    return templates.TemplateResponse("ui/change_password.html", {"request": request, "user": user})


# ── User-facing agent token management (no SA elevation needed) ──

def _user_can_manage_agent(user, agent, db, *, admin_only: bool = False) -> None:
    """GATE: Check user can manage (cycle/revoke/restore) this agent (v3 / VF-315).

    v1.1 tightened rule: only the agent's creator and SU/SA can cycle/revoke. Only SU/SA
    can restore. Project owners (PO) and project-admin members can NOT manage other
    users' agents — they are collab roles, not sysadmin. If a PO wants an ex-member's
    tools gone, they remove the human member and the cascade in members.remove_member
    handles the fallout. See user-agent-model.md §4.1 (v1.1).

    admin_only=True  → SU/SA only (restore; 'restore is sysadmin recovery').
    admin_only=False → SU/SA + the agent's own creator (cycle/revoke own).
    """
    from fastapi import HTTPException
    if user.role == "super_user":
        return
    if not admin_only and agent.created_by == user.id:
        return
    if admin_only:
        raise HTTPException(status_code=403,
            detail="Restoring a revoked agent is a sysadmin action. Must be SU or SA.")
    raise HTTPException(status_code=403,
        detail="Must be the agent's creator or SU/SA. Project owners and admin members "
               "cannot manage other users' agents (v1.1).")


@router.post("/ui/api/agents/{agent_id}/cycle")
def user_cycle_agent(agent_id: str, request: Request, db: Session = Depends(get_db)):
    """Cycle agent token. v3: creator, project admin/owner, or SU/SA. Operator never changes."""
    user = _get_session_user(request, db)
    if not user:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Not authenticated")
    from app.models.agent import Agent
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Agent not found")
    _user_can_manage_agent(user, agent, db)
    if agent.status != "active":
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="Can only cycle active agents.")

    import secrets, hashlib
    raw_token = "vf_" + secrets.token_hex(20)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    prefix = raw_token[:8]
    agent.api_token_hash = token_hash
    agent.token_prefix = prefix
    # VF-341 §4.5 + VF-342: cycle = fresh TTL clock. Eternal status from
    # creation time is dropped on cycle; to keep an agent eternal the operator
    # must revoke + create-new with the eternal toggle. Mirrors admin.py:cycle.
    # VF-306: reset api_call_count window so the post-cycle counter starts at 0.
    from app.api.v2 import session_policy as _sp
    now = datetime.now(timezone.utc)
    agent.api_call_count = 0
    agent.api_call_count_since = now
    agent.expires_at = now + timedelta(days=_sp.token_ttl_days(db))

    # VF-303: short-lived nonce for same-origin download
    from app.api.v2.admin import _stash_token_download
    download_nonce = _stash_token_download(db, agent.id, raw_token)

    db.add(ActivityEvent(
        project_id=agent.project_id, task_id=None,
        actor_type="human", actor_user_id=user.id,
        action="agent_token_cycled",
        details=json.dumps({"agent_name": agent.name, "actor": user.display_name,
                            "self_cycle": agent.created_by == user.id}),
    ))
    db.commit()
    return {"id": agent.id, "name": agent.name, "token": raw_token,
            "token_display": f"{prefix}...{token_hash[-3:]}",
            "download_url": f"/ui/api/agents/{agent.id}/token-file?nonce={download_nonce}"}


class UserCreateAgentBody(BaseModel):
    name: str
    project_slug: str
    model_type: str = "claude"
    description: str = ""
    # VF-342: eternal toggle, default OFF. Eternal = expires_at NULL (no TTL).
    eternal: bool = False


@router.post("/ui/api/agents", status_code=201)
def user_create_agent(body: UserCreateAgentBody, request: Request, db: Session = Depends(get_db)):
    """Create agent — for project owners and SU. No SA elevation needed."""
    user = _get_session_user(request, db)
    if not user:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Not authenticated")
    from app.models.agent import Agent
    from app.models.project_member import ProjectMember as _PM

    project = db.query(Project).filter(Project.slug == body.project_slug).first()
    if not project:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Project not found")

    # GATE: Any project member with write+ role can create their own agents (v3)
    # See 0-MD/0-Documentation/public/user-agent-model.md §4
    from app.api.v2.projects import _require_write
    _require_write(request, db, project.id)

    slug = f"{body.project_slug}-{body.name.lower().replace(' ', '-')}"
    if db.query(Agent).filter(Agent.slug == slug).first():
        from fastapi import HTTPException
        raise HTTPException(status_code=409, detail=f"Agent '{body.name}' already exists on this project.")

    raw_token = "vf_" + secrets.token_hex(20)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    prefix = raw_token[:8]

    # VF-341 §4.5 + VF-342: stamp expires_at unless eternal explicitly opted in.
    from app.api.v2 import session_policy as _sp
    token_expires = None if body.eternal else (
        datetime.now(timezone.utc) + timedelta(days=_sp.token_ttl_days(db))
    )
    agent = Agent(
        id=str(uuid.uuid4()),
        name=body.name, slug=slug, description=body.description,
        status="active",
        project_id=project.id, created_by=user.id,
        model_type=body.model_type,
        api_token_hash=token_hash, token_prefix=prefix,
        expires_at=token_expires,
    )
    db.add(agent)
    db.add(_PM(project_id=project.id, agent_id=agent.id, role="write"))

    # VF-303: short-lived nonce for same-origin token download
    from app.api.v2.admin import _stash_token_download
    download_nonce = _stash_token_download(db, agent.id, raw_token)

    db.add(ActivityEvent(
        project_id=project.id, task_id=None, actor_type="human",
        actor_user_id=user.id,  # v3: dual preservation (FK + snapshot)
        action="agent_created",
        details=json.dumps({"agent_name": body.name, "project": body.project_slug,
                            "actor": user.display_name, "eternal": body.eternal}),
    ))
    db.commit()
    return {"id": agent.id, "name": agent.name, "slug": slug, "token": raw_token,
            "token_display": f"{prefix}...{token_hash[-3:]}", "project": body.project_slug,
            "eternal": body.eternal,
            "download_url": f"/ui/api/agents/{agent.id}/token-file?nonce={download_nonce}"}


@router.get("/ui/api/agents/{agent_id}/token-file")
def agent_token_file(agent_id: str, nonce: str, request: Request, db: Session = Depends(get_db),
                     format: str = "token"):
    """VF-303: one-time plaintext agent-token download as an attachment.
    Gated only by the nonce (24-byte urlsafe, single-use, 5-min TTL). Same-origin
    GET so browsers fast-path the download without SmartScreen/Safe-Browsing
    reputation scans that penalise blob: URLs.

    VF-380: `format` query param selects the body shape:
      - "token" (default, legacy) — raw token, filename `agent-token.txt`
      - "agent-config"            — env-var file, filename `.agent-config`
        body: `VIBEFORGE_TOKEN=…\nVIBEFORGE_API=https://host/api/v2\n`
    """
    from app.models.agent_token_download import AgentTokenDownload
    from fastapi import HTTPException
    from fastapi.responses import PlainTextResponse
    from datetime import datetime as _dt, timezone as _tz

    row = db.query(AgentTokenDownload).filter(
        AgentTokenDownload.nonce == nonce,
        AgentTokenDownload.agent_id == agent_id,
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Invalid download link.")
    now = _dt.now(_tz.utc)
    if row.consumed_at is not None:
        raise HTTPException(status_code=410, detail="Download link already used.")
    if row.expires_at < now:
        raise HTTPException(status_code=410, detail="Download link expired.")

    token = row.token_plaintext
    # Single-use semantics: mark consumed and wipe the plaintext now that it has
    # been served. Expired/consumed rows can be pruned by a periodic sweep.
    row.consumed_at = now
    row.token_plaintext = ""
    db.commit()

    if format == "agent-config":
        scheme = request.url.scheme
        host = request.url.netloc
        if scheme == "http" and not host.startswith(("localhost", "127.0.0.1")):
            scheme = "https"
        api_url = f"{scheme}://{host}/api/v2"
        body = f"VIBEFORGE_TOKEN={token}\nVIBEFORGE_API={api_url}\n"
        filename = ".agent-config"
    else:
        body = token + "\n"
        filename = "agent-token.txt"

    return PlainTextResponse(
        content=body,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/ui/api/agents/{agent_id}/revoke")
def user_revoke_agent(agent_id: str, request: Request, db: Session = Depends(get_db)):
    """Revoke agent. v3: creator (self-revoke), project admin/owner, or SU/SA."""
    user = _get_session_user(request, db)
    if not user:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Not authenticated")
    from app.models.agent import Agent
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Agent not found")
    _user_can_manage_agent(user, agent, db)
    if agent.status == "revoked":
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="Agent is already revoked.")
    from datetime import datetime, timezone
    agent.status = "revoked"
    agent.revoked_at = datetime.now(timezone.utc)
    agent.revoked_by = user.id
    agent.api_token_hash = None
    agent.token_prefix = None
    db.add(ActivityEvent(
        project_id=agent.project_id, task_id=None, actor_type="human",
        actor_user_id=user.id,
        action="agent_revoked",
        details=json.dumps({"agent_name": agent.name, "actor": user.display_name,
                            "self_revoke": agent.created_by == user.id}),
    ))
    db.commit()
    return {"ok": True, "message": f"Agent {agent.name} revoked."}


@router.post("/ui/api/agents/{agent_id}/restore")
def user_restore_agent(agent_id: str, request: Request, db: Session = Depends(get_db)):
    """Restore revoked agent. v3: admin-only action (project admin/owner, or SU/SA). Self-restore NOT allowed."""
    user = _get_session_user(request, db)
    if not user:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Not authenticated")
    from app.models.agent import Agent
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Agent not found")
    _user_can_manage_agent(user, agent, db, admin_only=True)
    if agent.status != "revoked":
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="Agent is not revoked.")
    agent.status = "active"
    agent.revoked_at = None
    agent.revoked_by = None
    db.add(ActivityEvent(
        project_id=agent.project_id, task_id=None, actor_type="human",
        actor_user_id=user.id,
        action="agent_restored",
        details=json.dumps({"agent_name": agent.name, "actor": user.display_name}),
    ))
    db.commit()
    return {"ok": True, "message": f"Agent {agent.name} restored. Cycle token to issue new credentials."}


@router.get("/ui/api/agents/{agent_id}/onboard-prompt")
def user_onboard_prompt(agent_id: str, request: Request, db: Session = Depends(get_db)):
    """Generate onboarding prompt — for project owners and SU."""
    user = _get_session_user(request, db)
    if not user:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Not authenticated")
    from app.models.agent import Agent
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Agent not found")
    is_su = user.role == "super_user"
    owns_project = agent.project_id and db.query(Project).filter(
        Project.id == agent.project_id, Project.owner_id == user.id).first()
    if not is_su and not owns_project:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="You must own this project or be a Super User.")
    proj = db.query(Project).filter(Project.id == agent.project_id).first() if agent.project_id else None
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
   - Create .agent-config with: VIBEFORGE_API={base}/api/v2 VIBEFORGE_TOKEN=<token> VIBEFORGE_PROJECT={proj.slug if proj else '{{project_slug}}'}
   - Add .agent-config AND agent-token.txt to .gitignore.
   - Delete the agent-token.txt file (ONLY that — NOT .agent-config). Never display tokens.

2. Verify identity: source .agent-config && curl -sL -H "Authorization: Bearer $VIBEFORGE_TOKEN" "$VIBEFORGE_API/me"

3. Fetch your project contract: curl -sL -H "Authorization: Bearer $VIBEFORGE_TOKEN" "{base}/agentnotes/{proj.slug if proj else '$VIBEFORGE_PROJECT'}"
   This contains all API endpoints, rules, and workflows. Follow it.

4. Write AGENTS.md (or CLAUDE.md) from the agents_md_template field in the contract.

5. Run checktasks — fetch tasks and begin work. If no tasks exist, you are the onboarding partner.
- Follow the full contract from /agentnotes."""
    return {"prompt": prompt}


@router.get("/ui/api/prefs")
def get_prefs(request: Request, db: Session = Depends(get_db)):
    user = _get_session_user(request, db)
    if not user:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Not authenticated")
    from app.models.user_preferences import UserPreferences
    row = db.query(UserPreferences).filter(UserPreferences.user_id == user.id).first()
    return json.loads(row.prefs_json) if row else {}


class PrefsBody(BaseModel):
    prefs: dict


@router.put("/ui/api/prefs")
def put_prefs(body: PrefsBody, request: Request, db: Session = Depends(get_db)):
    user = _get_session_user(request, db)
    if not user:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Not authenticated")
    from app.models.user_preferences import UserPreferences
    row = db.query(UserPreferences).filter(UserPreferences.user_id == user.id).first()
    if row:
        row.prefs_json = json.dumps(body.prefs)
    else:
        db.add(UserPreferences(user_id=user.id, prefs_json=json.dumps(body.prefs)))
    db.commit()
    return {"ok": True}


# ── Health: live sessions + agent fleet (SU/SA only) ─────────────
def _parse_ua_short(ua: str) -> str:
    """Tiny UA parser for the health page rows."""
    if not ua:
        return ""
    ua_l = ua.lower()
    if "edg/" in ua_l: b = "Edge"
    elif "chrome/" in ua_l and "chromium" not in ua_l: b = "Chrome"
    elif "firefox/" in ua_l: b = "Firefox"
    elif "safari/" in ua_l and "chrome" not in ua_l: b = "Safari"
    elif "curl/" in ua_l: b = "curl"
    else: b = "browser"
    if "windows nt" in ua_l: o = "Win"
    elif "iphone" in ua_l or "ipad" in ua_l: o = "iOS"
    elif "android" in ua_l: o = "Android"
    elif "mac os" in ua_l: o = "macOS"
    elif "linux" in ua_l: o = "Linux"
    else: o = ""
    return f"{b}/{o}" if o else b


@router.post("/ui/api/heartbeat")
def heartbeat(request: Request, db: Session = Depends(get_db)):
    """Browser-side heartbeat — bumps session.last_activity_at AND slides
    expires_at when tab is visible. Called every 30s by base.html JS.

    VF-341 §4.2 + §6.4: heartbeat is now where the sliding extension
    happens. Active operators never get logged out mid-work; abandoned
    sessions auto-die in slide_window time; even continuously-active
    sessions rotate every absolute_cap days (the min() bound).

    Filter mirrors _get_session_user (revoked rows can't slide).
    """
    session_id = request.cookies.get(SESSION_COOKIE)
    if not session_id:
        return {"ok": False}
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    sess = db.query(UserSession).filter(
        UserSession.id == session_id,
        UserSession.expires_at > now,
        UserSession.revoked_at.is_(None),
    ).first()
    if not sess:
        return {"ok": False}
    sess.last_activity_at = now
    # Slide only board sessions; elevation cookies have their own TTL/renewal.
    if sess.session_type == "user":
        from app.api.v2 import session_policy as _sp
        _sp.slide_session_expiry(sess, now=now)
    db.commit()
    return {"ok": True, "expires_at": sess.expires_at.isoformat()}


# ─────────────────────────────────────────────────────────────────────────────
# Health data collectors — pure data fns shared across two auth planes.
#
# WHY two planes:
#   - SU (Board) reaches the data via /ui/api/health/*  (vf_session, path=/)
#   - SA (Portal) reaches the data via /admin/portal/api/health/*
#       (vf_sa_session, path=/admin/) — identity-roles.md §3 fences SA cookie
#       to /admin/, so the JSON surface MUST mirror under /admin/portal/api/.
#       The shared helpers below let both wrappers stay thin without the SA
#       cookie ever leaking onto a /ui/* path.
# ─────────────────────────────────────────────────────────────────────────────

def _collect_board_activity(db: Session) -> dict:
    from app.models.activity import ActivityEvent
    from app.models.agent import Agent as _Ag
    rows = db.query(ActivityEvent).order_by(ActivityEvent.created_at.desc()).limit(50).all()
    agent_names = {a.id: a.name for a in db.query(_Ag).all()}
    user_names = {u.id: u.display_name for u in db.query(User).all()}
    out = []
    for evt in rows:
        try:
            details = json.loads(evt.details) if evt.details else {}
        except (ValueError, TypeError):
            details = {}
        actor = details.get("actor") if isinstance(details, dict) else None
        if not actor:
            if evt.actor_type == "agent" and evt.actor_token_id:
                actor = agent_names.get(evt.actor_token_id, "Agent (unknown)")
            elif evt.actor_type == "human" and evt.actor_user_id:
                actor = user_names.get(evt.actor_user_id, "Unknown")
            elif evt.actor_type == "system":
                actor = "System"
            else:
                actor = "Unknown"
        out.append({
            "time": evt.created_at.isoformat() if evt.created_at else None,
            "actor": actor,
            "actor_type": evt.actor_type,
            "summary": _humanize_event(evt.action, details if isinstance(details, dict) else {}),
        })
    return {"events": out}


def _collect_sessions(db: Session) -> dict:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    rows = (db.query(UserSession, User)
        .join(User, UserSession.user_id == User.id)
        .filter(UserSession.expires_at > now)
        .all())
    by_user: dict = {}
    for sess, u in rows:
        if u.id not in by_user:
            parts = (u.display_name or "").split()
            initials = (parts[0][0] + parts[-1][0]).upper() if len(parts) > 1 else (u.display_name or "?")[:2].upper()
            by_user[u.id] = {
                "user_id": u.id,
                "name": u.display_name,
                "username": u.username,
                "initials": initials,
                "role": u.role,
                "tab_count": 0,
                "has_sa": False,
                "browsers": set(),
                "ips": set(),
                "last_activity_at": None,
                "oldest_session_at": None,
            }
        entry = by_user[u.id]
        entry["tab_count"] += 1
        if sess.session_type == "sa":
            entry["has_sa"] = True
        if sess.user_agent:
            entry["browsers"].add(_parse_ua_short(sess.user_agent))
        if sess.ip_address:
            entry["ips"].add(sess.ip_address)
        la = sess.last_activity_at or sess.created_at
        if la and (entry["last_activity_at"] is None or la > entry["last_activity_at"]):
            entry["last_activity_at"] = la
        if sess.created_at and (entry["oldest_session_at"] is None or sess.created_at < entry["oldest_session_at"]):
            entry["oldest_session_at"] = sess.created_at

    out = []
    for entry in by_user.values():
        out.append({
            "user_id": entry["user_id"],
            "name": entry["name"],
            "username": entry["username"],
            "initials": entry["initials"],
            "role": entry["role"],
            "tab_count": entry["tab_count"],
            "has_sa": entry["has_sa"],
            "browser": " · ".join(sorted(entry["browsers"])) if entry["browsers"] else "",
            "ip": " · ".join(sorted(entry["ips"])) if entry["ips"] else "",
            "last_activity_at": entry["last_activity_at"].isoformat() if entry["last_activity_at"] else None,
            "session_started_at": entry["oldest_session_at"].isoformat() if entry["oldest_session_at"] else None,
        })
    out.sort(key=lambda x: x["last_activity_at"] or "", reverse=True)
    return {"count": len(out), "sessions": out}


def _collect_agent_fleet(db: Session) -> dict:
    from app.models.agent import Agent as _Ag
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    agents = db.query(_Ag).order_by(_Ag.status, _Ag.name).all()
    out = []
    for a in agents:
        proj_slug = None
        proj_name = None
        if a.project_id:
            p = db.query(Project.slug, Project.name).filter(Project.id == a.project_id).first()
            if p:
                proj_slug, proj_name = p[0], p[1]
        token_age_days = None
        if a.created_at:
            token_age_days = (now - a.created_at).days
        last_seen_iso = a.last_seen_at.isoformat() if a.last_seen_at else None
        out.append({
            "id": a.id,
            "name": a.name,
            "slug": a.slug,
            "status": a.status,
            "model_type": a.model_type,
            "model_name": a.model_name,
            "project_slug": proj_slug,
            "project_name": proj_name,
            "last_seen_at": last_seen_iso,
            "token_age_days": token_age_days,
            "has_token": bool(a.api_token_hash),
        })
    return {"count": len(out), "agents": out}


def _collect_auth_stats(db: Session) -> dict:
    from app.models.activity import ActivityEvent
    from datetime import datetime, timezone, timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    def cnt(action):
        return db.query(ActivityEvent).filter(
            ActivityEvent.action == action,
            ActivityEvent.created_at >= cutoff,
        ).count()
    return {
        "signins_success": cnt("login_success"),
        "signins_failed": cnt("login_failed"),
        "sa_elevations": cnt("sa_login_success"),
        "sa_failed": cnt("sa_login_failed"),
        "token_cycles": cnt("agent_token_cycled"),
        "audit_events_24h": db.query(ActivityEvent).filter(ActivityEvent.created_at >= cutoff).count(),
    }


def _collect_db_stats(db: Session) -> dict:
    from app.models.activity import ActivityEvent
    from app.models.agent import Agent as _Ag
    from app.models.task_note import TaskNote as _TN
    return {
        "activity_events": db.query(ActivityEvent).count(),
        "tasks": db.query(Task).count(),
        "task_notes": db.query(_TN).count(),
        "users": db.query(User).count(),
        "agents": db.query(_Ag).count(),
        "projects": db.query(Project).count(),
    }


# ── /ui/api/health/* — Board surface, SU only (vf_session) ──

def _gate_health_su(request: Request, db: Session):
    """SU-only gate for /ui/api/health/*. SA never holds vf_session
    (identity-roles.md §3) — SA reaches the same data via /admin/portal/api/health/*."""
    user = _get_session_user(request, db)
    if not user or user.role != "super_user":
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Super user role required.")


@router.get("/ui/api/health/board-activity")
def health_board_activity(request: Request, db: Session = Depends(get_db)):
    _gate_health_su(request, db)
    return _collect_board_activity(db)


@router.get("/ui/api/health/sessions")
def health_sessions(request: Request, db: Session = Depends(get_db)):
    _gate_health_su(request, db)
    return _collect_sessions(db)


@router.get("/ui/api/health/agent-fleet")
def health_agent_fleet(request: Request, db: Session = Depends(get_db)):
    _gate_health_su(request, db)
    return _collect_agent_fleet(db)


@router.get("/ui/api/health/auth-stats")
def health_auth_stats(request: Request, db: Session = Depends(get_db)):
    _gate_health_su(request, db)
    return _collect_auth_stats(db)


@router.get("/ui/api/health/db-stats")
def health_db_stats(request: Request, db: Session = Depends(get_db)):
    _gate_health_su(request, db)
    return _collect_db_stats(db)


# ── Help / About placeholders ──
# Architecture / contract / proposed / product doc routes were REMOVED 2026-04-08
# (see VF-248). Engineering docs live in 0-MD/ and are read via local library
# bundles (0-MD/library/*.html) or git checkout. The board UI does not surface
# them. The agent contract endpoint at /agentnotes is the only doc-adjacent
# surface still served by the app — that's a programmatic JSON contract,
# not in-shell documentation reading.


@router.get("/ui/docs/help", response_class=HTMLResponse)
def docs_help(request: Request, db: Session = Depends(get_db)):
    """Help placeholder. Content TBD — see VF-213."""
    if not _require_login(request, db):
        return RedirectResponse(url="/ui/login", status_code=302)
    user = _get_session_user(request, db)
    projects = _sidebar_projects(db, user)
    placeholder_body = (
        '<h1 id="help">Help</h1>'
        '<p><em>This page is a placeholder. Detailed help content is coming soon.</em></p>'
        '<h2 id="for-now">For now</h2>'
        '<ul>'
        '<li>Check the <a href="/ui/docs/about">About</a> page for project info.</li>'
        '<li>Browse the activity feed at <a href="/ui/activity">/ui/activity</a> to see what is happening on the board.</li>'
        '<li>Configure your appearance and account at <a href="/ui/config">/ui/config</a>.</li>'
        '<li>Engineering documentation lives locally with the codebase (see <code>0-MD/</code>) — not surfaced in the board UI by design. See VF-248 for the future architecture decision.</li>'
        '</ul>'
        '<p>Tracked as <strong>VF-213</strong>.</p>'
    )
    return templates.TemplateResponse(
        "ui/docs_simple.html",
        {
            "request": request,
            "active_nav": "docs_help",
            "projects": projects,
            "page_title": "Help",
            "body": placeholder_body,
            **_user_context(db, request),
        },
    )


@router.get("/ui/docs/about", response_class=HTMLResponse)
def docs_about(request: Request, db: Session = Depends(get_db)):
    """About placeholder. Content TBD — see VF-214."""
    if not _require_login(request, db):
        return RedirectResponse(url="/ui/login", status_code=302)
    user = _get_session_user(request, db)
    projects = _sidebar_projects(db, user)
    placeholder_body = (
        '<h1 id="about">About VibeForge+</h1>'
        '<p><em>This page is a placeholder. Detailed about content is coming soon.</em></p>'
        '<h2 id="what-it-is">What it is</h2>'
        '<p>VibeForge+ is a self-hosted project tracker designed for small teams doing real product work with AI agents as first-class collaborators. Humans and bots share the same board, the same notes, the same tasks — and the same effort accounting.</p>'
        '<h2 id="why">Why</h2>'
        '<p>Because pair-programming with AI is a new way of working, and existing tools don\'t handle it well. We needed a board that treats Claude and Codex as identities, not as anonymous API clients, and that can answer "how much real work went into this project?" honestly when both humans and agents contributed.</p>'
        '<h2 id="links">Links</h2>'
        '<ul>'
        '<li><a href="/ui/docs/help">Help</a> — how to use VibeForge+</li>'
        '<li><a href="/agentnotes">Agent contract</a> — programmatic contract for AI agents</li>'
        '</ul>'
        '<p>Tracked as <strong>VF-214</strong>.</p>'
    )
    return templates.TemplateResponse(
        "ui/docs_simple.html",
        {
            "request": request,
            "active_nav": "docs_about",
            "projects": projects,
            "page_title": "About",
            "body": placeholder_body,
            **_user_context(db, request),
        },
    )


@router.get("/ui/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    session_id = request.cookies.get(SESSION_COOKIE)
    if session_id:
        sess = db.query(UserSession).filter(UserSession.id == session_id).first()
        if sess:
            db.delete(sess)
            db.commit()
    response = RedirectResponse(url="/ui/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(SA_COOKIE, path="/")
    return response


@router.get("/ui/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    if not _require_login(request, db):
        if getattr(request.state, 'must_change_password', False):
            return RedirectResponse(url="/ui/must-change-password", status_code=302)
        return RedirectResponse(url="/ui/login", status_code=302)
    from app.models.agent import Agent
    from app.models.project_member import ProjectMember
    from app.models.activity import ActivityEvent
    from app.models.user_project_pin import UserProjectPin
    from sqlalchemy import func

    user = _get_session_user(request, db)
    base_q = db.query(Project)
    # GATE: Non-SU users only see projects they own or are members of
    if user and user.role != "super_user":
        member_pids = [m.project_id for m in db.query(ProjectMember.project_id).filter(ProjectMember.user_id == user.id).all()]
        base_q = base_q.filter((Project.owner_id == user.id) | (Project.id.in_(member_pids)))
    raw_projects = base_q.all()

    # Per-user pins
    pin_map: dict[str, int] = {}
    if user:
        for pid, po in db.query(UserProjectPin.project_id, UserProjectPin.pin_order).filter(UserProjectPin.user_id == user.id).all():
            pin_map[pid] = po
    for p in raw_projects:
        p._pinned = p.id in pin_map
        p._pin_order = pin_map.get(p.id, 0)

    # Sort: pinned first by pin_order, then unpinned by card_order, then alpha
    projects = sorted(
        raw_projects,
        key=lambda p: (
            0 if p._pinned else 1,
            p._pin_order if p._pinned else (1000 + (p.card_order or 0)),
            p.status,
            p.name,
        ),
    )
    sidebar_projects = [p for p in projects if p.status == "active"]

    # Task stats per project
    project_stats = {}
    for p in projects:
        rows = (
            db.query(Task.status, func.count())
            .filter(Task.project_id == p.id, Task.owner_label != "phase")
            .group_by(Task.status)
            .all()
        )
        stats = {r[0]: r[1] for r in rows}
        total = sum(stats.values())
        done = stats.get("done", 0)
        cancelled = stats.get("cancelled", 0)
        resolved = done + cancelled  # ITIL: both are terminal states
        active = stats.get("in_progress", 0) + stats.get("needs_review", 0) + stats.get("ready", 0)
        project_stats[p.id] = {
            "total": total, "done": done, "active": active,
            "backlog": stats.get("backlog", 0),
            "blocked": stats.get("blocked", 0),
            "cancelled": cancelled,
            "resolved": resolved,
            "pct": round(resolved / total * 100) if total > 0 else 0,
        }

    # Milestones per project
    project_milestones = {}
    all_ms = db.query(Milestone).order_by(Milestone.sort_order).all()
    for ms in all_ms:
        project_milestones.setdefault(ms.project_id, []).append(ms)

    # Members per project (users + agents)
    project_members = {}
    members_raw = db.query(ProjectMember).all()
    user_map = {u.id: u for u in db.query(User).all()}
    agent_map = {a.id: a for a in db.query(Agent).all()}
    for m in members_raw:
        entry = {"role": m.role, "type": "human", "name": "?", "initials": "?"}
        if m.user_id and m.user_id in user_map:
            u = user_map[m.user_id]
            entry["name"] = u.display_name
            parts = u.display_name.split()
            entry["initials"] = (parts[0][0] + parts[-1][0]).upper() if len(parts) > 1 else u.display_name[:2].upper()
            entry["type"] = "human"
        elif m.agent_id and m.agent_id in agent_map:
            a = agent_map[m.agent_id]
            entry["name"] = a.name
            entry["initials"] = a.name[:2].upper()
            entry["type"] = "agent"
        else:
            continue
        project_members.setdefault(m.project_id, []).append(entry)

    # Agents
    agents = db.query(Agent).filter(Agent.status == "active").all()

    # Recent activity (last 25, scoped to projects user can see) — enrich with actor name
    _home_agent_names = {a.id: a.name for a in db.query(Agent).all()}
    _home_user_names = {u.id: u.display_name for u in db.query(User).all()}
    _act_q = db.query(ActivityEvent)
    if user and user.role != "super_user":
        # GATE: Only events from projects the user can see
        visible_pids = [p.id for p in projects]
        if visible_pids:
            _act_q = _act_q.filter(ActivityEvent.project_id.in_(visible_pids))
        else:
            _act_q = _act_q.filter(ActivityEvent.project_id == None)  # noqa: E711
    _raw_activity = _act_q.order_by(ActivityEvent.created_at.desc()).limit(25).all()
    recent_activity = []
    for evt in _raw_activity:
        actor_name = None
        details_dict = {}
        try:
            details_dict = json.loads(evt.details) if evt.details else {}
            actor_name = details_dict.get("actor", details_dict.get("superseded_by", details_dict.get("reverted_by")))
        except (ValueError, TypeError):
            pass
        if not actor_name:
            if evt.actor_type == "agent" and evt.actor_token_id:
                actor_name = _home_agent_names.get(evt.actor_token_id, "Agent (unknown)")
            elif evt.actor_type == "human" and evt.actor_user_id:
                actor_name = _home_user_names.get(evt.actor_user_id, "Unknown")
            elif evt.actor_type == "system":
                actor_name = "System"
            elif evt.actor_type == "agent":
                actor_name = "Agent (unknown)"
            elif evt.actor_type == "human":
                actor_name = "Unknown"
            else:
                actor_name = "Unknown"
        evt._actor_name = actor_name
        evt._summary = _humanize_event(evt.action, details_dict)
        recent_activity.append(evt)

    # Global stats
    total_tasks = sum(s["total"] for s in project_stats.values())
    active_tasks = sum(s["active"] for s in project_stats.values())
    active_projects = sum(1 for p in projects if p.status == "active")

    return templates.TemplateResponse(
        "ui/home.html",
        {
            "request": request,
            "active_nav": "home",
            "projects": sidebar_projects,
            "all_projects": projects,
            "project_stats": project_stats,
            "project_milestones": project_milestones,
            "project_members": project_members,
            "agents": agents,
            "recent_activity": recent_activity,
            "global_stats": {
                "projects": active_projects,
                "total_tasks": total_tasks,
                "active_tasks": active_tasks,
                "agents_online": len(agents),
            },
            # VF-382: drawer hides "Resume Onboarding" affordance when wizard
            # is past the operator-tunable expiry window.
            "wizard_expiry_seconds": _wizard_expiry(db),
            **_user_context(db, request),
        },
    )


def _wizard_expiry(db: Session) -> int:
    """VF-382: thin wrapper around admin_experimental.get_wizard_expiry_seconds
    so home + wizard routes don't import the experimental module directly.
    """
    from app.api.v2.admin_experimental import get_wizard_expiry_seconds
    return get_wizard_expiry_seconds(db)


@router.get("/ui/config", response_class=HTMLResponse)
def config_view(request: Request, db: Session = Depends(get_db)):
    if not _require_login(request, db):
        if getattr(request.state, 'must_change_password', False):
            return RedirectResponse(url="/ui/must-change-password", status_code=302)
        return RedirectResponse(url="/ui/login", status_code=302)
    projects = _sidebar_projects(db, _get_session_user(request, db))
    current_user = _get_session_user(request, db)

    # WHY: Agents tab needs user's role, owned projects, and their agents
    from app.models.agent import Agent as _Ag
    user_agents = []
    can_manage_agents = False
    if current_user:
        is_su = current_user.role == "super_user"
        # Projects this user can manage agents on: owned OR write+ member (v3 permission relaxation)
        if is_su:
            owned_projects = db.query(Project).filter(Project.status == "active").order_by(Project.name).all()
        else:
            from app.models.project_member import ProjectMember as _PM
            member_project_ids = [pm.project_id for pm in db.query(_PM).filter(
                _PM.user_id == current_user.id,
                _PM.role.in_(("write", "admin")),
            ).all()]
            owned_projects = db.query(Project).filter(
                ((Project.owner_id == current_user.id) | (Project.id.in_(member_project_ids))),
                Project.status == "active",
            ).order_by(Project.name).all()
        can_manage_agents = len(owned_projects) > 0 or is_su

        # Agents for accessible projects. v3: non-SU users see only their own agents (created_by self).
        if owned_projects:
            proj_ids = [p.id for p in owned_projects]
            agent_q = db.query(_Ag).filter(_Ag.project_id.in_(proj_ids))
            if not is_su:
                agent_q = agent_q.filter(_Ag.created_by == current_user.id)
            agents_raw = agent_q.order_by(_Ag.name).all()
            now = datetime.now(timezone.utc)
            for a in agents_raw:
                proj = next((p for p in owned_projects if p.id == a.project_id), None)
                token_display = f"{a.token_prefix}...{a.api_token_hash[-3:]}" if a.api_token_hash and a.token_prefix else "no token"
                # VF-342: derive expiry surface — eternal / expired / red (<3d) / amber (<14d) / ok.
                # Active rows only — revoked agents have api_token_hash NULL'd already.
                expiry_state = "no_token"
                expires_in_days = None
                if a.status == "active" and a.api_token_hash:
                    if a.expires_at is None:
                        expiry_state = "eternal"
                    else:
                        delta_s = (a.expires_at - now).total_seconds()
                        if delta_s <= 0:
                            expiry_state = "expired"
                            expires_in_days = 0
                        else:
                            expires_in_days = int(delta_s // 86400)
                            if delta_s < 3 * 86400:
                                expiry_state = "red"
                            elif delta_s < 14 * 86400:
                                expiry_state = "amber"
                            else:
                                expiry_state = "ok"
                user_agents.append({
                    "id": a.id, "name": a.name, "slug": a.slug, "status": a.status,
                    "model_type": a.model_type, "model_name": a.model_name,
                    "token_display": token_display,
                    "has_token": bool(a.api_token_hash and a.token_prefix),
                    "project_slug": proj.slug if proj else "?",
                    "project_name": proj.name if proj else "?",
                    "expires_at": a.expires_at.isoformat() if a.expires_at else None,
                    "expiry_state": expiry_state,
                    "expires_in_days": expires_in_days,
                    "last_seen_at": a.last_seen_at.isoformat() if a.last_seen_at else None,
                })
    else:
        owned_projects = []

    return templates.TemplateResponse(
        "ui/config.html",
        {
            "request": request,
            "active_nav": "config",
            "projects": projects,
            "current_user": current_user,
            "owned_projects": owned_projects,
            "user_agents": user_agents,
            "can_manage_agents": can_manage_agents,
            **_user_context(db, request),
        },
    )


@router.get("/ui/my-tasks", response_class=HTMLResponse)
def my_tasks_view(request: Request, db: Session = Depends(get_db)):
    if not _require_login(request, db):
        if getattr(request.state, 'must_change_password', False):
            return RedirectResponse(url="/ui/must-change-password", status_code=302)
        return RedirectResponse(url="/ui/login", status_code=302)
    from app.models.activity import ActivityEvent
    from app.models.project_member import ProjectMember as _PM
    from app.models.user import User as _User
    from app.models.agent import Agent as _Agent

    user = _get_session_user(request, db)
    projects = _sidebar_projects(db, user)
    user_ctx = _user_context(db, request)
    is_viewer = bool(user and user.role == "viewer")

    # GATE: Restrict to projects user owns or is a member of (SU/SA see all)
    base_q = (
        db.query(Task, Project)
        .join(Project, Task.project_id == Project.id)
        .filter(
            Task.status.notin_(["done", "cancelled"]),
            Task.owner_label != "phase",
            Project.status == "active",
        )
    )
    if user and user.role != "super_user":
        member_pids = [m.project_id for m in db.query(_PM.project_id).filter(_PM.user_id == user.id).all()]
        base_q = base_q.filter((Project.owner_id == user.id) | (Project.id.in_(member_pids)))
    open_tasks = base_q.order_by(Task.task_number).all()

    # Build phase + milestone lookups
    phase_ids = {t.phase_id for t, _ in open_tasks if t.phase_id}
    phase_map = {}
    if phase_ids:
        from app.models.milestone import Milestone as MilestoneModel
        phases = (
            db.query(Phase.id, Phase.name, MilestoneModel.label, MilestoneModel.name)
            .outerjoin(MilestoneModel, Phase.milestone_id == MilestoneModel.id)
            .filter(Phase.id.in_(phase_ids))
            .all()
        )
        for pid, pname, mlabel, mname in phases:
            phase_map[pid] = {"phase_name": pname, "ms_label": mlabel or "", "ms_name": mname or ""}

    # Build per-project member name set for orphan detection
    visible_pids = {p.id for _, p in open_tasks}
    members_by_pid: dict[str, set[str]] = {}
    if visible_pids:
        for pid, dn in (
            db.query(_PM.project_id, _User.display_name)
            .join(_User, _PM.user_id == _User.id)
            .filter(_PM.project_id.in_(visible_pids))
            .all()
        ):
            members_by_pid.setdefault(pid, set()).add(dn)
        # Also include each project's owner display name
        owner_rows = (
            db.query(Project.id, _User.display_name)
            .join(_User, Project.owner_id == _User.id)
            .filter(Project.id.in_(visible_pids))
            .all()
        )
        for pid, dn in owner_rows:
            members_by_pid.setdefault(pid, set()).add(dn)

    me_name = user.display_name if user else ""

    def _initials(name: str) -> str:
        if not name:
            return "?"
        parts = [p for p in name.replace(":", " ").split() if p]
        if len(parts) >= 2:
            return (parts[0][0] + parts[-1][0]).upper()
        return name[:2].upper()

    def _classify(owner_label: str, project_id: str) -> tuple[str, str]:
        """Returns (bucket, display_name) where bucket is one of:
           'mine', 'agent', 'other', 'unassigned', 'orphan'."""
        ol = (owner_label or "").strip()
        ol_lower = ol.lower()
        if not ol or ol_lower == "human":
            return ("unassigned", "Unassigned")
        if ol_lower in ("agent", "claude", "codex") or ol_lower.startswith("agent:"):
            display = ol.split(":", 1)[1].strip() if ":" in ol else (ol if ol_lower not in ("agent",) else "Agent")
            return ("agent", display)
        if ol_lower.startswith("human:"):
            display = ol.split(":", 1)[1].strip()
        else:
            display = ol
        if display == me_name:
            return ("mine", display)
        # Check if display is a real member of this project
        members = members_by_pid.get(project_id, set())
        if display in members:
            return ("other", display)
        return ("orphan", display)

    mine: list = []
    agents_grouped: dict[str, list] = {}
    others_grouped: dict[str, list] = {}
    unassigned: list = []
    orphans: list = []

    for t, p in open_tasks:
        ph = phase_map.get(t.phase_id, {})
        bucket, display = _classify(t.owner_label, p.id)
        entry = {
            "task": t, "project": p,
            "short_id": f"{p.prefix}-{t.task_number}" if p.prefix and t.task_number else "",
            "phase_name": ph.get("phase_name", ""),
            "ms_label": ph.get("ms_label", ""),
            "ms_name": ph.get("ms_name", ""),
            "owner_display": display,
            "owner_initials": _initials(display),
            "bucket": bucket,
        }
        if bucket == "mine":
            mine.append(entry)
        elif bucket == "agent":
            agents_grouped.setdefault(display, []).append(entry)
        elif bucket == "other":
            others_grouped.setdefault(display, []).append(entry)
        elif bucket == "orphan":
            orphans.append(entry)
        else:
            unassigned.append(entry)

    # Sort agent + other groups alpha by name
    agents_groups = [{"name": k, "tasks": agents_grouped[k]} for k in sorted(agents_grouped.keys())]
    others_groups = [{"name": k, "tasks": others_grouped[k]} for k in sorted(others_grouped.keys())]

    return templates.TemplateResponse(
        "ui/my_tasks.html",
        {
            "request": request,
            "active_nav": "my_tasks",
            "projects": projects,
            "mine": mine,
            "agents_groups": agents_groups,
            "others_groups": others_groups,
            "unassigned": unassigned,
            "orphans": orphans,
            "is_viewer": is_viewer,
            **user_ctx,
        },
    )


@router.get("/ui/activity", response_class=HTMLResponse)
def activity_view(request: Request, db: Session = Depends(get_db)):
    if not _require_login(request, db):
        if getattr(request.state, 'must_change_password', False):
            return RedirectResponse(url="/ui/must-change-password", status_code=302)
        return RedirectResponse(url="/ui/login", status_code=302)
    from app.models.activity import ActivityEvent
    from app.models.agent import Agent
    from app.models.project_member import ProjectMember as _PM

    user = _get_session_user(request, db)
    projects = _sidebar_projects(db, user)

    # GATE: Restrict activity feed to projects user can see
    base_q = (
        db.query(ActivityEvent, Project)
        .join(Project, ActivityEvent.project_id == Project.id)
    )
    if user and user.role != "super_user":
        member_pids = [m.project_id for m in db.query(_PM.project_id).filter(_PM.user_id == user.id).all()]
        base_q = base_q.filter((Project.owner_id == user.id) | (Project.id.in_(member_pids)))
    raw_events = base_q.order_by(ActivityEvent.created_at.desc()).limit(100).all()

    # Enrich with task data where available
    task_ids = {e.task_id for e, _ in raw_events if e.task_id}
    task_map = {}
    if task_ids:
        for t in db.query(Task).filter(Task.id.in_(task_ids)).all():
            proj = db.query(Project.prefix).filter(Project.id == t.project_id).first()
            short_id = f"{proj[0]}-{t.task_number}" if proj and proj[0] and t.task_number else ""
            ph_name = ""
            if t.phase_id:
                ph = db.query(Phase.name).filter(Phase.id == t.phase_id).first()
                if ph: ph_name = ph[0]
            task_map[t.id] = {"title": t.title, "short_id": short_id, "status": t.status, "phase": ph_name}

    # Build agent name lookup for actor resolution
    from app.models.agent import Agent as _Agent
    _agent_names = {a.id: a.name for a in db.query(_Agent).all()}
    _user_names = {u.id: u.display_name for u in db.query(User).all()}

    def _resolve_event_actor(evt):
        """Resolve actor display name from event. Returns 'Unknown' if not stamped."""
        # Try details JSON first
        try:
            d = json.loads(evt.details) if evt.details else {}
            name = d.get("actor", d.get("superseded_by", d.get("reverted_by")))
            if name:
                return name
        except (ValueError, TypeError):
            pass
        # Resolve from actor_user_id or actor_token_id
        if evt.actor_type == "agent" and evt.actor_token_id:
            return _agent_names.get(evt.actor_token_id, "Agent")
        if evt.actor_type == "human" and evt.actor_user_id:
            return _user_names.get(evt.actor_user_id, "Human")
        # WHY: No more arbitrary "first user" fallback — that misattributed actions to random users.
        # If the event predates actor stamping, show Unknown rather than pick a name.
        if evt.actor_type == "agent":
            return "Agent (unknown)"
        if evt.actor_type == "human":
            return "Unknown"
        return evt.actor_type

    events = []
    for evt, project in raw_events:
        task_ctx = task_map.get(evt.task_id, {})
        try:
            details_dict = json.loads(evt.details) if evt.details else {}
        except (ValueError, TypeError):
            details_dict = {}
        events.append({
            "event": evt,
            "project": project,
            "task": task_ctx,
            "actor_name": _resolve_event_actor(evt),
            "summary": _humanize_event(evt.action, details_dict),
        })

    agents = db.query(Agent).filter(Agent.status == "active").all()

    return templates.TemplateResponse(
        "ui/activity.html",
        {
            "request": request,
            "active_nav": "activity",
            "projects": projects,
            "events": events,
            "agents": agents,
            **_user_context(db, request),
        },
    )


@router.get("/api/v2/activity-count")
def activity_count(db: Session = Depends(get_db)):
    """Quick count of recent activity — used by pages to detect new updates."""
    from app.models.activity import ActivityEvent
    from sqlalchemy import func
    count = db.query(func.count(ActivityEvent.id)).scalar()
    return {"count": count}


@router.get("/ui/health", response_class=HTMLResponse)
def health_view(request: Request, db: Session = Depends(get_db)):
    if not _require_login(request, db):
        if getattr(request.state, 'must_change_password', False):
            return RedirectResponse(url="/ui/must-change-password", status_code=302)
        return RedirectResponse(url="/ui/login", status_code=302)
    # GATE: SU only. SA cannot hold vf_session (identity-roles.md §2,§3) and
    # never traverses /ui/*. SA reaches the same data via /admin/portal/health/*
    # which renders portal-native tiles fed by /admin/portal/api/health/*.
    user = _get_session_user(request, db)
    if not user or user.role != "super_user":
        return RedirectResponse(url="/ui/", status_code=302)
    projects = _sidebar_projects(db, user)
    return templates.TemplateResponse(
        "ui/health.html",
        {
            "request": request,
            "active_nav": "health",
            "projects": projects,
            **_user_context(db, request),
        },
    )




# ────────────────────────────────────────────────────────────────────
# VF-380 — Proper Onboard Wizard (Phase 1)
# Customer-facing onboard ceremony. Mission-Control launched, sidebar-free
# fullscreen surface. Stages 1-3 ephemeral (browser-only); Stage 3 commit
# calls _create_project_record (canonical, see VF-353 IC-020) + inline Agent
# create + token stash via _stash_token_download. Stages 4-6 persisted; the
# watch view (Stage 6) polls GET /api/v2/projects/{slug}/onboard-state every 3s.
#
# Sits alongside /ui/test-wizard until proven, then test wizard retires per
# the proposal's 4-phase migration plan (see PROPER-ONBOARD-WIZARD-LIFECYCLE.md).
# ────────────────────────────────────────────────────────────────────
def _render_onboard_wizard(request: Request, db: Session, user, project):
    from app.api.v2.onboard import FRAMING_TEXT
    scheme = request.url.scheme
    host = request.url.netloc
    if scheme == "http" and not host.startswith(("localhost", "127.0.0.1")):
        scheme = "https"
    board_root_url = f"{scheme}://{host}"
    board_api_url = f"{board_root_url}/api/v2"

    operator_name = (user.display_name or user.username or "you") if user else "you"
    framing_html = _render_framing_html(FRAMING_TEXT.replace("{human_name}", operator_name))

    ctx = {
        "request": request,
        "mode": "resume" if project else "create",
        "human_name": operator_name,
        "framing_html": framing_html,
        "board_root_url": board_root_url,
        "board_api_url": board_api_url,
        # VF-382: post-completion expiry window (seconds). Wizard JS uses it
        # to render an "expired" state on resume past completed_at + this.
        "wizard_expiry_seconds": _wizard_expiry(db),
        # VF-390: SSR-inject the user's ui_prefs (theme/hue) so the wizard
        # inherits the app-wide vf-theme rather than hard-coding dark.
        **_user_context(db, request),
    }
    if project:
        from app.models.agent import Agent
        agent = db.query(Agent).filter(
            Agent.project_id == project.id,
            Agent.status == "active",
        ).first()
        # Template references {{ project.name }} in the <title> + breadcrumb.
        # Pass the SQLAlchemy object directly (Jinja attribute access works).
        ctx["project"] = project
        ctx["project_json"] = json.dumps({
            "slug": project.slug,
            "name": project.name,
            "prefix": project.prefix,
            "description": project.description or "",
        })
        ctx["agent_json"] = json.dumps({
            "id": agent.id if agent else None,
            "name": agent.name if agent else "",
            "vendor": ((agent.model_type if agent else "claude") or "claude"),
        })
        ctx["onboard_state_json"] = json.dumps(project.onboard_state or {})
    return templates.TemplateResponse("ui/onboard.html", ctx)


@router.get("/ui/onboard", response_class=HTMLResponse)
def onboard_wizard_new(request: Request, db: Session = Depends(get_db)):
    """Ephemeral pre-commit wizard launch — Mission Control's 'New Project Wizard' target."""
    if not _require_login(request, db):
        if getattr(request.state, 'must_change_password', False):
            return RedirectResponse(url="/ui/must-change-password", status_code=302)
        return RedirectResponse(url="/ui/login", status_code=302)
    user = _get_session_user(request, db)
    return _render_onboard_wizard(request, db, user, project=None)


@router.get("/ui/onboard/{slug}", response_class=HTMLResponse)
def onboard_wizard_resume(slug: str, request: Request, db: Session = Depends(get_db)):
    """Resume mode — opens the wizard at the watch view for an in-progress project."""
    if not _require_login(request, db):
        if getattr(request.state, 'must_change_password', False):
            return RedirectResponse(url="/ui/must-change-password", status_code=302)
        return RedirectResponse(url="/ui/login", status_code=302)
    user = _get_session_user(request, db)
    project = db.query(Project).filter(Project.slug == slug).first()
    if not project:
        return RedirectResponse(url="/ui/", status_code=302)
    return _render_onboard_wizard(request, db, user, project=project)


class OnboardWizardCommit(BaseModel):
    project_name: str = Field(..., min_length=1, max_length=200)
    project_slug: str = Field(..., min_length=1, max_length=80)
    project_prefix: str = Field(..., min_length=2, max_length=5)
    project_description: str = Field("", max_length=2000)
    agent_name: str = Field(..., min_length=1, max_length=80)
    agent_vendor: str = Field(..., min_length=1, max_length=20)
    agent_description: str = Field("", max_length=2000)


@router.post("/ui/api/onboard/commit")
def onboard_wizard_commit(body: OnboardWizardCommit, request: Request, db: Session = Depends(get_db)):
    """VF-380 Phase 1 — Stage 3 commit. Creates project + agent + token in one
    atomic flush. Project goes through _create_project_record (canonical, see
    VF-353 IC-020); Agent + token mirror test_wizard.issue_token's inline-Agent-
    create pattern (until that gets its own canonical helper).
    """
    from fastapi import HTTPException
    if not _require_login(request, db):
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = _get_session_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Session not resolved")

    from app.api.v2.projects import _create_project_record
    from app.api.v2.admin import _stash_token_download
    from app.models.agent import Agent
    from app.models.project_member import ProjectMember as _PM

    project = _create_project_record(
        db, user,
        name=body.project_name,
        slug=body.project_slug,
        prefix=body.project_prefix.upper(),
        description=body.project_description,
        # VF-384: also seed resume_summary so the project drawer's main text
        # area shows what the user typed in Stage 1 instead of the canned
        # "New project created." default. Drawer surfaces resume_summary, not
        # description; both fields end up identical at create time, but the
        # user can edit either later via the drawer.
        resume_summary=body.project_description,
        via="onboard_wizard",
    )

    raw_token = "vf_" + secrets.token_hex(20)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    prefix = raw_token[:8]

    agent_slug = f"{project.slug}-{body.agent_name.lower().replace(' ', '-')}"
    agent = Agent(
        id=str(uuid.uuid4()),
        name=body.agent_name,
        slug=agent_slug,
        description=body.agent_description or f"Agent for {project.name} (created via onboard wizard)",
        status="active",
        project_id=project.id,
        created_by=user.id,
        model_type=body.agent_vendor,
        api_token_hash=token_hash,
        token_prefix=prefix,
        expires_at=None,
    )
    db.add(agent)
    db.add(_PM(project_id=project.id, agent_id=agent.id, role="write"))
    db.flush()  # populate agent.id for token stash

    download_nonce = _stash_token_download(db, agent.id, raw_token)

    db.add(ActivityEvent(
        id=str(uuid.uuid4()),
        project_id=project.id, task_id=None, actor_type="human",
        actor_user_id=user.id,
        action="onboard_wizard_committed",
        details=json.dumps({
            "project_slug": project.slug,
            "project_name": project.name,
            "agent_name": body.agent_name,
            "agent_vendor": body.agent_vendor,
            "actor": user.display_name,
            "via": "onboard_wizard",
        }),
    ))
    db.commit()

    return {
        "project": {
            "id": project.id,
            "slug": project.slug,
            "name": project.name,
            "prefix": project.prefix,
        },
        "agent": {
            "id": agent.id,
            "name": agent.name,
            "vendor": body.agent_vendor,
        },
        "token_display": f"{prefix}...{token_hash[-3:]}",
        "download_url": f"/ui/api/agents/{agent.id}/token-file?nonce={download_nonce}",
        "wizard_resume_url": f"/ui/onboard/{project.slug}",
    }


@router.get("/ui/api/onboard/agent-status")
def onboard_wizard_agent_status(agent_id: str, request: Request, db: Session = Depends(get_db)):
    """VF-380 Phase 1 — lightweight contact signal for the wizard's Stage 5
    auto-advance. Returns api_call_count + last_seen_at so the wizard can
    detect "agent has contacted the board" earlier than the first substep
    stamp (which is the /onboard-state signal Stage 6 watches).
    """
    from fastapi import HTTPException
    if not _require_login(request, db):
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = _get_session_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Session not resolved")
    from app.models.agent import Agent
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    _user_can_manage_agent(user, agent, db)
    return {
        "agent_id": agent.id,
        "api_call_count": int(agent.api_call_count or 0),
        "last_seen_at": agent.last_seen_at.isoformat() if agent.last_seen_at else None,
        "last_contract_read_at": agent.last_contract_read_at.isoformat() if agent.last_contract_read_at else None,
        "status": agent.status,
    }


class OnboardWizardReissue(BaseModel):
    agent_id: str = Field(..., min_length=1)


@router.post("/ui/api/onboard/reissue-token")
def onboard_wizard_reissue_token(body: OnboardWizardReissue, request: Request, db: Session = Depends(get_db)):
    """VF-380 Phase 1 — Stage 4 token re-download. The token-file endpoint is
    single-use (VF-303); on retry / refresh / browser-mangled-filename failures,
    cycle the agent's token and hand back a fresh download nonce. Mirrors
    test_wizard.issue_token's cycle-on-reissue pattern.
    """
    from fastapi import HTTPException
    if not _require_login(request, db):
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = _get_session_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Session not resolved")

    from app.api.v2.admin import _stash_token_download
    from app.models.agent import Agent
    agent = db.query(Agent).filter(Agent.id == body.agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    # GATE: only the project admin/owner (or SU/SA) can re-issue.
    _user_can_manage_agent(user, agent, db)

    raw_token = "vf_" + secrets.token_hex(20)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    prefix = raw_token[:8]
    agent.api_token_hash = token_hash
    agent.token_prefix = prefix
    agent.api_call_count = 0
    agent.status = "active"

    download_nonce = _stash_token_download(db, agent.id, raw_token)
    db.add(ActivityEvent(
        id=str(uuid.uuid4()),
        project_id=agent.project_id, task_id=None, actor_type="human",
        actor_user_id=user.id,
        action="onboard_wizard_token_reissued",
        details=json.dumps({
            "agent_id": agent.id, "agent_name": agent.name,
            "actor": user.display_name, "via": "onboard_wizard",
        }),
    ))
    db.commit()

    return {
        "token_display": f"{prefix}...{token_hash[-3:]}",
        "download_url": f"/ui/api/agents/{agent.id}/token-file?nonce={download_nonce}",
    }


# VF-335: Proxy Admin graduated to /admin/portal/. Hard 301 keeps any
# pre-graduation bookmark alive while routing operators to the canonical
# surface. The /ui/admin/proxy/change-cert sub-redirect lives in
# admin_portal.py (redirect_change_cert).
@router.get("/ui/admin/proxy", response_class=HTMLResponse)
@router.get("/ui/admin/proxy/", response_class=HTMLResponse)
def admin_proxy_view():
    return RedirectResponse(url="/admin/portal/", status_code=301)


@router.get("/ui/projects/{slug}", response_class=HTMLResponse)
def project_view(slug: str, request: Request, db: Session = Depends(get_db)):
    if not _require_login(request, db):
        if getattr(request.state, 'must_change_password', False):
            return RedirectResponse(url="/ui/must-change-password", status_code=302)
        return RedirectResponse(url="/ui/login", status_code=302)
    user = _get_session_user(request, db)
    project = db.query(Project).filter(Project.slug == slug).first()
    if not project or not _user_can_access_project(user, project, db):
        return RedirectResponse(url="/ui/", status_code=302)
    return RedirectResponse(url=f"/ui/projects/{slug}/gantt", status_code=302)


@router.get("/ui/projects/{slug}/board", response_class=HTMLResponse)
def board_view(
    slug: str,
    request: Request,
    milestone: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    if not _require_login(request, db):
        if getattr(request.state, 'must_change_password', False):
            return RedirectResponse(url="/ui/must-change-password", status_code=302)
        return RedirectResponse(url="/ui/login", status_code=302)
    user = _get_session_user(request, db)
    projects = _sidebar_projects(db, user)
    project = db.query(Project).filter(Project.slug == slug).first()
    # GATE: per-project visibility
    if project and not _user_can_access_project(user, project, db):
        return RedirectResponse(url="/ui/", status_code=302)

    # Collect milestones for filter chips from DB
    milestones = []  # list of (label, name)
    ms_names = {}
    if project:
        ms_names = _milestone_names(db, project.id)
        rows = (
            db.query(Task.milestone_label)
            .filter(Task.project_id == project.id, Task.milestone_label.isnot(None), Task.milestone_label != "")
            .distinct()
            .order_by(Task.milestone_label)
            .all()
        )
        milestones = [(r[0], ms_names.get(r[0], r[0])) for r in rows]

    active_milestone_name = ms_names.get(milestone, milestone) if milestone else None

    return templates.TemplateResponse(
        "ui/board.html",
        {
            "request": request,
            "active_nav": "project",
            "active_project": slug,
            "projects": projects,
            "project": project,
            "milestones": milestones,
            "active_milestone": milestone,
            "active_milestone_name": active_milestone_name,
            **_user_context(db, request),
        },
    )


@router.get("/ui/projects/{slug}/gantt", response_class=HTMLResponse)
def gantt_view(
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
):
    if not _require_login(request, db):
        if getattr(request.state, 'must_change_password', False):
            return RedirectResponse(url="/ui/must-change-password", status_code=302)
        return RedirectResponse(url="/ui/login", status_code=302)
    user = _get_session_user(request, db)
    projects = _sidebar_projects(db, user)
    project = db.query(Project).filter(Project.slug == slug).first()
    # GATE: per-project visibility
    if project and not _user_can_access_project(user, project, db):
        return RedirectResponse(url="/ui/", status_code=302)

    gantt_milestones = []
    phase_tasks = {}  # phase_id -> [task, ...]
    ms_phases = {}    # milestone_id -> [phase, ...]

    if project:
        # Get all milestones sorted
        from app.models.milestone import Milestone as MilestoneModel
        milestones_db = (
            db.query(MilestoneModel)
            .filter(MilestoneModel.project_id == project.id)
            .order_by(MilestoneModel.sort_order)
            .all()
        )
        gantt_milestones = [
            {"id": m.id, "label": m.label, "name": m.name, "status": m.status or "active"}
            for m in milestones_db
        ]

        # Get all phases grouped by milestone
        phases_db = (
            db.query(Phase)
            .filter(Phase.project_id == project.id)
            .order_by(Phase.sort_order)
            .all()
        )
        for ph in phases_db:
            mid = ph.milestone_id or 0
            ms_phases.setdefault(mid, []).append(
                {"id": ph.id, "name": ph.name, "milestone_id": ph.milestone_id}
            )

        # Get all tasks grouped by phase
        tasks_db = (
            db.query(Task)
            .filter(Task.project_id == project.id, Task.owner_label != "phase")
            .order_by(Task.sort_order)
            .all()
        )
        for t in tasks_db:
            pid = t.phase_id or 0
            phase_tasks.setdefault(pid, []).append(t)

    return templates.TemplateResponse(
        "ui/gantt.html",
        {
            "request": request,
            "active_nav": "project",
            "active_project": slug,
            "projects": projects,
            "project": project,
            "gantt_milestones": gantt_milestones,
            "ms_phases": ms_phases,
            "phase_tasks": phase_tasks,
            "milestones": [(m["label"], m["name"]) for m in gantt_milestones],
            "active_milestone": None,
            "slug": slug,
            **_user_context(db, request),
        },
    )


# --- User display preferences API ---
# Uses first active user until proper session auth is wired

def _get_default_user(db: Session, request: Request = None) -> User | None:
    """Get current user — from session if request provided, fallback to first active."""
    if request:
        user = _get_session_user(request, db)
        if user:
            return user
    return db.query(User).filter(User.status == "active").first()


def _user_context(db: Session, request: Request = None) -> dict:
    """Get current user context for templates. Includes ui_prefs JSON for SSR theme injection."""
    user = _get_default_user(db, request)
    prefs_json = "{}"
    if user:
        from app.models.user_preferences import UserPreferences
        row = db.query(UserPreferences).filter(UserPreferences.user_id == user.id).first()
        if row and row.prefs_json:
            prefs_json = row.prefs_json
        name = user.display_name or user.email.split('@')[0].title() or 'Admin'
        parts = name.split()
        initials = (parts[0][0] + parts[-1][0]).upper() if len(parts) > 1 else name[:2].upper()
        return {
            "current_user_name": name,
            "current_user_initials": initials,
            "current_user_role": user.role or "user",
            "ui_prefs_json": prefs_json,
        }
    return {
        "current_user_name": "Admin",
        "current_user_initials": "AD",
        "current_user_role": "user",
        "ui_prefs_json": "{}",
    }


# WHY: Old /api/v2/users/me/prefs stub removed — was session-blind (used _get_default_user)
# and referenced non-existent user.ui_prefs column. Replaced by /ui/api/prefs (session-aware).
