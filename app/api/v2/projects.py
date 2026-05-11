import json
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from datetime import date, datetime
import hashlib

from app.db.session import get_db
from app.models.project import Project
from app.models.phase import Phase
from app.models.milestone import Milestone
from app.models.task import Task
from app.models.task_note import TaskNote
from app.models.activity import ActivityEvent
from app.models.agent import Agent
from app.api.v2.events import broadcast


def _cookie_user(request: Request, db: Session):
    """Resolve the cookie-auth User object (or None). Used to stamp activity events."""
    if not request:
        return None
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return None
    from app.models.user import User
    from app.models.session import UserSession
    session_id = request.cookies.get("vf_session")
    if not session_id:
        return None
    from datetime import datetime, timezone
    sess = db.query(UserSession).filter(
        UserSession.id == session_id,
        UserSession.session_type == "user",
        UserSession.expires_at > datetime.now(timezone.utc),
    ).first()
    if not sess:
        return None
    return db.query(User).filter(User.id == sess.user_id, User.status == "active").first()


def _human_project_role(request: Request, db: Session, project_id: str) -> str | None:
    """Return effective role for cookie-auth user on a project.

    Returns one of: 'su' (super_user only — SA is not a board participant), 'owner', 'admin', 'write',
    'read', or None (no access). For agent callers, returns None — agents are
    handled by _resolve_actor and have implicit write within their project scope.
    For viewers (global role=viewer), the per-project role is forced to 'read'
    regardless of project_member.role.
    """
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return None  # agent — not a human
    from app.models.user import User
    from app.models.session import UserSession
    from app.models.project_member import ProjectMember as _PM
    session_id = request.cookies.get("vf_session")
    if not session_id:
        return None
    from datetime import datetime, timezone
    sess = db.query(UserSession).filter(
        UserSession.id == session_id,
        UserSession.session_type == "user",
        UserSession.expires_at > datetime.now(timezone.utc),
    ).first()
    if not sess:
        return None
    user = db.query(User).filter(User.id == sess.user_id, User.status == "active").first()
    if not user:
        return None
    if user.role == "super_user":
        return "su"
    project = db.query(Project).filter(Project.id == project_id).first()
    if project and project.owner_id == user.id:
        return "owner"
    member = db.query(_PM).filter(_PM.project_id == project_id, _PM.user_id == user.id).first()
    if not member:
        return None
    # GATE: viewer global role downgrades any membership to read
    if user.role == "viewer":
        return "read"
    return member.role  # 'admin' / 'write' / 'read'


def _require_write(request: Request, db: Session, project_id: str) -> None:
    """GATE: Caller must be a valid agent (scoped) or human with write+/owner/su role.
    Raises 401/403. Trusts _resolve_actor's hard validation rather than the bare Bearer header (VF-217 fix).
    """
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        # Force token validation via _resolve_actor — raises 401 on invalid token, 403 cross-project.
        # If we reach the next line, the agent is real and scoped.
        _resolve_actor(request, db, project_id=project_id)
        return
    role = _human_project_role(request, db, project_id)
    if role in ("su", "owner", "admin", "write"):
        return
    if role == "read":
        raise HTTPException(status_code=403, detail="Read-only access — write requires write/admin/owner role.")
    raise HTTPException(status_code=403, detail="You are not a member of this project.")


def _require_admin(request: Request, db: Session, project_id: str) -> None:
    """GATE: Project-admin operations (rename, archive, manage members)."""
    role = _human_project_role(request, db, project_id)
    if role in ("su", "owner", "admin"):
        return
    # WHY: Tell the user *who* to ask. Resolve owner + project admins so the message is actionable.
    from app.models.user import User
    from app.models.project_member import ProjectMember as _PM
    project = db.query(Project).filter(Project.id == project_id).first()
    asks = []
    if project and project.owner_id:
        owner = db.query(User).filter(User.id == project.owner_id, User.status == "active").first()
        if owner:
            asks.append(owner.display_name)
    admin_users = (
        db.query(User).join(_PM, _PM.user_id == User.id)
        .filter(_PM.project_id == project_id, _PM.role == "admin", User.status == "active")
        .all()
    )
    for u in admin_users:
        if u.display_name not in asks:
            asks.append(u.display_name)
    if asks:
        msg = f"Only the project owner or an admin can do this. Ask {' or '.join(asks)} to perform this action, or request admin role from a Super User."
    else:
        msg = "Only the project owner or a project admin can do this. Request admin role from a Super User."
    raise HTTPException(status_code=403, detail=msg)


def _check_human_project_access(request: Request, db: Session, project_id: str) -> None:
    """GATE: For human (cookie-auth) callers, enforce project membership.
    Bearer callers are force-validated via _resolve_actor (raises 401 on invalid, 403 cross-project).
    No silent anonymous fallback (VF-217 fix).
    """
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        _resolve_actor(request, db, project_id=project_id)
        return
    from app.models.user import User
    from app.models.session import UserSession
    from app.models.project_member import ProjectMember as _PM
    session_id = request.cookies.get("vf_session")
    if not session_id:
        return  # anon — for read paths this is fine; write paths went through _require_write first
    from datetime import datetime, timezone
    sess = db.query(UserSession).filter(
        UserSession.id == session_id,
        UserSession.session_type == "user",
        UserSession.expires_at > datetime.now(timezone.utc),
    ).first()
    if not sess:
        return
    user = db.query(User).filter(User.id == sess.user_id, User.status == "active").first()
    if not user:
        return
    if user.role == "super_user":
        return
    project = db.query(Project).filter(Project.id == project_id).first()
    if project and project.owner_id == user.id:
        return
    member = db.query(_PM).filter(_PM.project_id == project_id, _PM.user_id == user.id).first()
    if member:
        return
    raise HTTPException(status_code=403, detail="You are not a member of this project.")


def _resolve_actor(request: Request, db: Session, project_id: str | None = None) -> tuple[str, str]:
    """Detect if request is from agent (Bearer token) or human (UI).
    Returns (actor_type, actor_name).
    GATE: If Bearer header present, token MUST validate as a real active agent — else 401.
    GATE: If project_id provided, agent must be scoped to that project — else 403.
    GATE: If no Bearer header, must have a valid session cookie — else 401.
    No silent fallback to first-active-user. (VF-217)
    """
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
        if not token:
            raise HTTPException(status_code=401, detail="Empty bearer token")
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        agent = db.query(Agent).filter(Agent.api_token_hash == token_hash, Agent.status == "active").first()
        if not agent:
            # GATE: Invalid token must NOT silently fall through to anonymous human (VF-217)
            raise HTTPException(status_code=401, detail="Invalid or revoked token")
        # VF-341 §4.5: token TTL enforcement. Same shape as revoked — 401, distinct
        # detail so the operator knows reissuance via Agents workspace is the fix.
        if agent.expires_at is not None:
            from datetime import datetime as _dt, timezone as _tz
            if agent.expires_at <= _dt.now(_tz.utc):
                raise HTTPException(status_code=401, detail="Agent token expired; reissue via Agents workspace.")
        # RULE: Agent scope enforcement — reject if wrong project
        if project_id and agent.project_id and agent.project_id != project_id:
            raise HTTPException(status_code=403, detail="Agent is not scoped to this project.")
        # WHY: Heartbeat — mark agent as recently seen for fleet status (VF-200)
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        agent.last_seen_at = now
        # VF-306: bare-minimum cumulative API-call counter. Resets on token cycle.
        agent.api_call_count = (agent.api_call_count or 0) + 1

        # GATE: Drift v4.1 freeze — if agent is currently escalated, writes are
        # 403'd until a human clears the flag via POST /tasks/{id}/clear-drift.
        # v4.1: state lives in drift_escalations table (ended_at IS NULL = active).
        #
        # VF-306 (PK 2026-04-28): drift_gate_enabled now controls ONLY this 403
        # freeze response. Eval prompts, attempt logging, escalation creation,
        # API-call counter — all keep running regardless of the toggle. This
        # lets the operator run the gate in "observation mode" (toggle OFF)
        # to see what would have happened without actually blocking agents.
        from app.api.v2.admin_experimental import get_bool as _get_bool
        _drift_gate_on = _get_bool(db, "drift_gate_enabled", True)
        if _drift_gate_on and request.method in ("POST", "PATCH", "PUT", "DELETE"):
            from app.api.v2.drift_gate import is_agent_frozen
            if is_agent_frozen(agent.id, db):
                raise HTTPException(status_code=403, detail=json.dumps({
                    "code": "BOARD_GATE_FROZEN",
                    "detail": "Your session is paused pending human review. Wait for a human to clear the drift flag on the ticket that triggered this state before attempting further writes.",
                    "gate_reason": "drift_eval_stuck",
                    "human_visible": True,
                }))

        # GATE: Onboard gate (VF-353 — CUSTOMER-ONBOARD-PROPOSAL §3.3) — block
        # agent writes when the project's onboard workflow has not registered a
        # agent_md_hash. Fires only on mutations + only when project_id is in
        # context. Exempts the onboard-state endpoints themselves so the agent
        # can register hashes without bootstrapping deadlock.
        #
        # The shape mirrors the drift gate (422 BOARD_GATE_TRIGGERED) so the
        # agent's existing recovery flow handles it without learning a new code.
        if request.method in ("POST", "PATCH", "PUT", "DELETE") and project_id:
            _ob_path = request.url.path or ""
            # Exempt: onboard-state endpoints (else bootstrap deadlock), AND
            # phases/milestones endpoints (those ARE onboard work — wave-2.0
            # plan_hash substep creates bookend phases + Documentation Complete
            # milestone before /complete registers agent_md_hash). Tasks + notes
            # + everything else stays gated — those are post-onboard work.
            _exempt = ("/onboard-state" in _ob_path
                       or _ob_path.endswith("/phases") or "/phases/" in _ob_path
                       or _ob_path.endswith("/milestones") or "/milestones/" in _ob_path)
            if not _exempt:
                _ob_proj = db.query(Project).filter(Project.id == project_id).first()
                _ob_state = (_ob_proj.onboard_state or {}) if _ob_proj else {}
                if not (_ob_state.get("agent_md_hash") and _ob_state.get("completed_at")):
                    # Compute the next missing step as a hint to the agent.
                    # Use the canonical ONBOARD_STEP_ORDER from onboard.py so
                    # this hint never drifts from server enforcement (wave 2.0
                    # reorder bug — was hardcoded with old order pre-2.13.2).
                    from app.api.v2.onboard import ONBOARD_STEP_ORDER as _ob_order_full
                    # Drop first_close_complete (post-/complete substep, not a
                    # write-gate hint target).
                    _ob_order = [s for s in _ob_order_full if s != "first_close_complete"]
                    _ob_next = next(
                        (s for s in _ob_order if not _ob_state.get(s)),
                        "agent_md_hash",
                    )
                    raise HTTPException(status_code=422, detail=json.dumps({
                        "code": "BOARD_GATE_TRIGGERED",
                        "gate_reason": "onboard_incomplete",
                        "detail": (
                            "This project's first-onboard workflow is incomplete. "
                            "Run the onboard sequence (fetch /api/v2/onboard/framing, "
                            "surface to the human, fetch /onboard/scaffold to "
                            "materialise the doc-tree, set doc complexity, write the "
                            "plan, build your agent discipline manifest — CLAUDE.md "
                            "if you're Claude, AGENTS.md (all caps) for most others "
                            "— then register hash via /onboard-state/complete) "
                            "before attempting writes."
                        ),
                        "next_step": _ob_next,
                        "onboard_state_endpoint": f"/api/v2/projects/{_ob_proj.slug}/onboard-state" if _ob_proj else None,
                        "human_visible": True,
                    }))

        # GATE: Drift v4 — interval check (agent must re-read contract after interval),
        # nonce check (proves agent parsed the contract response), and session-state
        # self-eval with pivot + escalation (see app/api/v2/drift_gate.py).
        # Fires on mutations only. Runs once per request (state flag on request).
        # VF-306: window is now operator-tunable (default 3600s, slider sets it
        # to {20m, 30m, 60m, 120m}). Read on every request — change takes
        # effect on the next gate firing without a restart.
        from app.api.v2.admin_experimental import get_drift_window_seconds
        DRIFT_REFRESH_INTERVAL = get_drift_window_seconds(db)
        _already_checked = getattr(request.state, '_drift_checked', False)
        if not _already_checked and request.method in ("POST", "PATCH", "PUT", "DELETE"):
            request.state._drift_checked = True
            last_read = agent.last_contract_read_at
            if last_read is not None:
                age = (now - last_read).total_seconds()
                if age > DRIFT_REFRESH_INTERVAL:
                    raise HTTPException(status_code=422, detail=json.dumps({
                        "code": "BOARD_GATE_TRIGGERED",
                        "detail": "Context drift detected. Re-read your project contract, then retry.",
                        "gate_reason": "contract_drift",
                        "refresh_endpoint": "/agentnotes",
                        "human_visible": True
                    }))
                # GATE: Nonce echo-back — proves agent parsed the contract response
                nonce_header = request.headers.get("X-Refresh-Nonce", "")
                if nonce_header and agent.refresh_nonce and nonce_header != agent.refresh_nonce:
                    raise HTTPException(status_code=422, detail=json.dumps({
                        "code": "BOARD_GATE_TRIGGERED",
                        "detail": "Your project contract appears outdated. Re-read the contract to get current rules and context.",
                        "gate_reason": "stale_nonce",
                        "refresh_endpoint": "/agentnotes",
                        "human_visible": True
                    }))
                # v4 self-eval — session-state pool + dedup + pivot + escalation.
                # VF-306: runs UNCONDITIONALLY now (used to be guarded by
                # _drift_gate_on). Logging eval attempts + escalation state is
                # what feeds the per-agent telemetry tables; the toggle only
                # affects whether an escalation translates to a 403 response
                # at the top of this function on subsequent requests.
                #
                # IC-018 (R2.5) + IC-025 (R2.6): suppress drift-eval while the
                # agent's project is in active onboard window AND for a 30-min
                # grace period after /complete lands. Cross-vendor confirmed
                # friction (Claude run 1 + Codex run 2 both fought the board
                # during phase/milestone creation; Claude Desktop run 3 hit
                # drift-eval on the FIRST mutation after /complete — pre-IC-025
                # behaviour fired aggressively the moment the suppression
                # cleared). An agent in onboard window or just-completed-onboard
                # has near-zero context decay possible by definition, so
                # drift-eval has no signal to detect — only noise that erodes
                # the agent's faith in the gate.
                _proj = db.query(Project).filter(Project.id == agent.project_id).first()
                _onboard_state = (_proj.onboard_state or {}) if _proj else {}
                _onboard_complete = bool(
                    _onboard_state.get("agent_md_hash")
                    and _onboard_state.get("completed_at")
                )
                _post_complete_grace = False
                if _onboard_complete:
                    try:
                        from datetime import datetime as _dt
                        _completed_at = _dt.fromisoformat(_onboard_state["completed_at"])
                        _grace_seconds = (now - _completed_at).total_seconds()
                        # 30-minute grace post-/complete; lets the agent settle
                        # into work (post real activity) before drift starts
                        # firing. After 30 min the cycle reads as a normal
                        # working session and decay becomes plausible again.
                        _post_complete_grace = 0 <= _grace_seconds < 1800
                    except (TypeError, ValueError):
                        _post_complete_grace = False
                if _onboard_complete and not _post_complete_grace:
                    from app.api.v2.drift_gate import check_drift_eval
                    check_drift_eval(agent, request, db)
                # else: skip drift-eval — agent in onboard window or 30-min
                # post-complete grace; no decay possible.
            # else: last_contract_read_at is None — agent has never read /agentnotes.
            # Don't gate. The drift gate only activates AFTER the agent's first
            # contract read (GET /agentnotes sets the timestamp).

        try:
            db.commit()
        except Exception:
            db.rollback()
        return ("agent", agent.name)

    # Cookie-auth path: human from UI session
    from app.models.user import User
    from app.models.session import UserSession
    session_id = request.cookies.get("vf_session")
    if session_id:
        from datetime import datetime, timezone
        sess = db.query(UserSession).filter(
            UserSession.id == session_id,
            UserSession.session_type == "user",
            UserSession.expires_at > datetime.now(timezone.utc),
        ).first()
        if sess:
            user = db.query(User).filter(User.id == sess.user_id, User.status == "active").first()
            if user:
                return ("human", user.display_name)

    # No valid auth at all — reject. NO MORE first-active-user fallback (VF-217).
    raise HTTPException(status_code=401, detail="Authentication required")

router = APIRouter()

import re
import uuid
from app.models.milestone import Milestone
from app.models.project_member import ProjectMember


# ── Agent self-discovery ──
# FLOW: Agent calls this to verify its token, identity, and scope
@router.get("/api/v2/me")
def agent_me(request: Request, db: Session = Depends(get_db)):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")
    token = auth[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Bearer token required")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    agent = db.query(Agent).filter(Agent.api_token_hash == token_hash).first()
    if not agent:
        raise HTTPException(status_code=401, detail="Invalid token")
    if agent.status == "revoked":
        raise HTTPException(status_code=401, detail="Agent revoked",
                            headers={"X-Agent-Status": "revoked"})
    if agent.status != "active":
        raise HTTPException(status_code=403, detail=f"Agent is {agent.status}")
    # WHY: Heartbeat for fleet status (VF-200)
    from datetime import datetime, timezone
    agent.last_seen_at = datetime.now(timezone.utc)
    try:
        db.commit()
    except Exception:
        db.rollback()
    # WHY: Lightweight heartbeat — agent verifies identity and scope
    project_info = None
    if agent.project_id:
        proj = db.query(Project.slug, Project.name).filter(Project.id == agent.project_id).first()
        if proj:
            project_info = {"slug": proj[0], "name": proj[1]}
    # WHY: Bootstrap hints — agent knows its state without multiple API calls
    bootstrap = {"status": "ready"}
    next_action = "checktasks"
    my_tasks = {"total": 0, "in_progress": 0, "ready": 0, "needs_review": 0}
    reviewers = []

    if agent.project_id:
        from sqlalchemy import func
        # Task counts for this agent
        agent_tasks = (db.query(Task.status, func.count(Task.id))
            .filter(Task.project_id == agent.project_id)
            .filter(Task.owner_label.ilike(f"%{agent.name}%") | Task.owner_label.ilike("%agent%"))
            .group_by(Task.status)
            .all())
        for status, count in agent_tasks:
            if status in my_tasks:
                my_tasks[status] = count
            my_tasks["total"] += count

        # Total project tasks
        total_project_tasks = db.query(func.count(Task.id)).filter(Task.project_id == agent.project_id).scalar() or 0

        # Human reviewers on this project
        from app.models.user import User as _U
        from app.models.project_member import ProjectMember as _PM
        human_members = (db.query(_U.display_name, _U.username)
            .join(_PM, _PM.user_id == _U.id)
            .filter(_PM.project_id == agent.project_id, _U.status == "active")
            .all())
        reviewers = [{"name": m[0], "username": m[1]} for m in human_members]

        # Determine next action
        if total_project_tasks == 0:
            next_action = "onboard — no tasks exist. Help human plan the project."
            bootstrap["status"] = "empty_project"
        elif my_tasks["in_progress"] > 0:
            next_action = "continue — you have tasks in progress."
        elif my_tasks["ready"] > 0:
            next_action = "pick_up — you have ready tasks to start."
        else:
            next_action = "ask_human — no tasks assigned to you."

    # WHY: token_prefix omitted — unnecessary exposure even though not secret (Codex feedback VF-186)
    return {
        "id": agent.id,
        "name": agent.name,
        "slug": agent.slug,
        "status": agent.status,
        "model_type": agent.model_type,
        "model_name": agent.model_name,
        "project": project_info,
        "bootstrap": bootstrap,
        "my_tasks": my_tasks,
        "next_action": next_action,
        "reviewers": reviewers,
    }


# ── Project CRUD ──

class ProjectCreate(BaseModel):
    name: str
    slug: str | None = None
    prefix: str | None = None
    description: str = ""
    root_path: str = ""
    docs_path: str = ""
    project_url: str = ""
    resume_summary: str = ""


class ProjectUpdate(BaseModel):
    # VF-357: extra='forbid' rejects unknown fields with a structured 422
    # (was: silent drop on 200). The exception handler in app/main.py
    # translates Pydantic's extra_forbidden into our standard 422 shape
    # with code/detail/agent_remedy per the pinned 422-recoverable principle.
    model_config = ConfigDict(extra='forbid')
    name: str | None = None
    description: str | None = None
    root_path: str | None = None
    docs_path: str | None = None
    project_url: str | None = None
    resume_summary: str | None = None
    # VF-355: rename + audit-bearing field changes carry a reason (>=10 chars,
    # mirroring VF-304's audit-mutation pattern). Required when name changes;
    # optional otherwise (description/path edits emit ActivityEvents either
    # way, just without operator rationale if reason is omitted).
    reason: str | None = None


class ResumeUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')  # VF-357
    resume_summary: str


def _slugify(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', value.strip().lower()).strip('-')


def _create_project_record(
    db: Session,
    creator_user,
    *,
    name: str,
    slug: str | None = None,
    prefix: str | None = None,
    description: str = "",
    root_path: str = "",
    docs_path: str = "",
    project_url: str = "",
    resume_summary: str = "",
    via: str = "api",
) -> Project:
    """Canonical project-creation pathway. SINGLE SOURCE OF TRUTH for the
    Project + Triage phase + ProjectMember(admin) + ActivityEvent stamping
    that every project on the board needs.

    IC-020 (R2.6 / VF-353, Claude Desktop's review): the test wizard reset
    endpoint previously created a Project row directly, bypassing this
    helper. Consequence: SU had no ProjectMember row, so the agent couldn't
    transition tasks to needs_review (owner_label='human:<name>' requires the
    human to be a member); and the Project had no `prefix`, so first task
    creation returned `short_id: null` (IC-022). Both fixed by routing all
    project creation through this helper.

    Caller is responsible for db.commit(). Helper does db.flush() so
    `project.id` is populated for chained ops (e.g. additional ActivityEvent
    rows for caller-specific context).
    """
    final_slug = slug or _slugify(name)
    if not final_slug:
        raise HTTPException(status_code=422, detail="Slug cannot be empty")
    if db.query(Project).filter(Project.slug == final_slug).first():
        raise HTTPException(status_code=409, detail=f"Project '{final_slug}' already exists")

    final_prefix = prefix or name.replace(' ', '')[:3].upper() or "VF"

    from sqlalchemy import func
    max_num = db.query(func.max(Project.project_number)).scalar() or 0
    next_num = max_num + 1

    project = Project(
        id=str(uuid.uuid4()),
        slug=final_slug,
        name=name,
        description=description or f"{name} tracked in VibeForge+.",
        root_path=root_path,
        docs_path=docs_path,
        project_url=project_url,
        resume_summary=resume_summary or "New project created.",
        prefix=final_prefix,
        project_number=next_num,
        owner_id=creator_user.id,
        created_by_user_id=creator_user.id,
    )
    db.add(project)
    db.flush()  # populate project.id for chained ops

    # Triage phase — default catch-all that the contract refers to.
    db.add(Phase(id=str(uuid.uuid4()), project_id=project.id, name="Triage", sort_order=0))

    # Creator as admin member — single source of truth for membership.
    # This is the row that lets needs_review work for agents on day one.
    db.add(ProjectMember(id=str(uuid.uuid4()), project_id=project.id,
                         user_id=creator_user.id, role="admin"))

    db.add(ActivityEvent(
        id=str(uuid.uuid4()), project_id=project.id, task_id=None,
        actor_type="human", actor_user_id=creator_user.id,
        action="project_created",
        details=json.dumps({"name": name, "slug": final_slug,
                            "creator": creator_user.display_name,
                            "actor": creator_user.display_name,
                            "via": via}),
    ))
    return project


@router.get("/api/v2/projects/")
@router.get("/api/v2/projects")
def list_projects(db: Session = Depends(get_db)):
    projects = db.query(Project).order_by(Project.name).all()
    return [
        {
            "id": p.id, "slug": p.slug, "name": p.name, "description": p.description,
            "status": p.status, "root_path": p.root_path, "docs_path": p.docs_path,
            "project_url": p.project_url, "resume_summary": p.resume_summary,
            "project_number": p.project_number, "prefix": p.prefix,
            "full_id": f"PRJ{p.project_number:05d}" if p.project_number else None,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        }
        for p in projects
    ]


@router.post("/api/v2/projects/", status_code=201)
@router.post("/api/v2/projects", status_code=201)
def create_project(body: ProjectCreate, request: Request = None, db: Session = Depends(get_db)):
    # GATE: project creation requires a resolved creator (VF-286 invariant — no orphan projects).
    # Bearer/agent callers and unauthenticated requests are rejected. First-boot SA wizard
    # does not go through this endpoint.
    creator_user = _cookie_user(request, db) if request else None
    if not creator_user:
        raise HTTPException(
            status_code=401,
            detail="Project creation requires an authenticated human session. "
                   "Agents cannot create projects.",
        )
    project = _create_project_record(
        db, creator_user,
        name=body.name,
        slug=body.slug,
        prefix=body.prefix,
        description=body.description,
        root_path=body.root_path,
        docs_path=body.docs_path,
        project_url=body.project_url,
        resume_summary=body.resume_summary,
        via="api",
    )
    db.commit()
    return {"id": project.id, "slug": project.slug, "name": project.name, "status": project.status}


@router.patch("/api/v2/projects/{slug}")
def update_project(slug: str, body: ProjectUpdate, request: Request = None, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.slug == slug).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if request:
        _require_admin(request, db, project.id)

    # VF-355: who is doing this — needed for ActivityEvent.actor_user_id so the
    # lifecycle drawer renders display_name instead of "Unknown". Same lookup
    # also feeds _append_lifecycle's actor= kwarg so the JSON lifecycle_log
    # carries the operator's display_name (the JS render reads from there too).
    actor_user = _cookie_user(request, db) if request else None
    actor_user_id = actor_user.id if actor_user else None
    actor_label = actor_user.display_name if actor_user else "Human"

    # VF-355: name change requires operator rationale (>= 10 chars). Mirrors
    # VF-304 audit-mutation pattern. The reason flows into the lifecycle log
    # AND the ActivityEvent.details so audit shows WHY, not just WHAT.
    if body.name is not None and body.name != project.name:
        reason = (body.reason or "").strip()
        if len(reason) < 10:
            raise HTTPException(status_code=422, detail={
                "code": "RENAME_REASON_REQUIRED",
                "detail": "Project rename requires reason (>=10 chars) so the audit row carries the WHY, not just the WHAT.",
                "agent_remedy": "Re-issue PATCH with body.reason set to a sentence (>=10 chars) explaining why the rename is happening. The reason is recorded on the lifecycle log and the ActivityEvent.",
                "human_visible": True,
            })
        old_name = project.name
        project.name = body.name
        _append_lifecycle(project, "renamed", reason, actor=actor_label)
        db.add(ActivityEvent(
            id=str(uuid.uuid4()), project_id=project.id, task_id=None,
            actor_type="human", actor_user_id=actor_user_id,
            action="project_renamed",
            details=json.dumps({"from": old_name, "to": body.name, "reason": reason}),
        ))

    # VF-355: audit-bearing edits to description / paths emit ActivityEvents
    # for symmetry with rename. Reason is optional here; from->to is captured
    # in details either way. Skipped on resume_summary (continuous-typing
    # surface — would flood the feed without proportional audit value).
    def _audit_field(field_name: str, old_value, new_value):
        if old_value == new_value:
            return
        details = {"field": field_name, "from": old_value or "", "to": new_value or ""}
        if body.reason and body.reason.strip():
            details["reason"] = body.reason.strip()
        db.add(ActivityEvent(
            id=str(uuid.uuid4()), project_id=project.id, task_id=None,
            actor_type="human", actor_user_id=actor_user_id,
            action=f"project_{field_name}_changed",
            details=json.dumps(details),
        ))

    if body.description is not None and body.description != project.description:
        _audit_field("description", project.description, body.description)
        project.description = body.description
    if body.root_path is not None and body.root_path != project.root_path:
        _audit_field("root_path", project.root_path, body.root_path)
        project.root_path = body.root_path
    if body.docs_path is not None and body.docs_path != project.docs_path:
        _audit_field("docs_path", project.docs_path, body.docs_path)
        project.docs_path = body.docs_path
    if body.project_url is not None and body.project_url != project.project_url:
        _audit_field("project_url", project.project_url, body.project_url)
        project.project_url = body.project_url
    if body.resume_summary is not None: project.resume_summary = body.resume_summary
    db.commit()
    return {"id": project.id, "slug": project.slug, "name": project.name, "status": project.status}


# ── Pin / Unpin ──

class PinBody(BaseModel):
    pinned: bool
    pin_order: int | None = None


@router.post("/api/v2/projects/{slug}/pin")
def pin_project(slug: str, body: PinBody, request: Request = None, db: Session = Depends(get_db)):
    """Per-user project pin. Personal preference — no role gate, but caller must
    be a member of the project (so it doesn't leak project IDs)."""
    project = db.query(Project).filter(Project.slug == slug).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if request:
        _check_human_project_access(request, db, project.id)
    user = _cookie_user(request, db) if request else None
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    from app.models.user_project_pin import UserProjectPin
    existing = db.query(UserProjectPin).filter(
        UserProjectPin.user_id == user.id,
        UserProjectPin.project_id == project.id,
    ).first()

    if body.pinned:
        # Max 5 pinned per user
        if not existing:
            count = db.query(UserProjectPin).filter(UserProjectPin.user_id == user.id).count()
            if count >= 5:
                raise HTTPException(status_code=422, detail="Maximum 5 pinned projects per user")
            order = body.pin_order
            if order is None:
                max_row = db.query(UserProjectPin.pin_order).filter(UserProjectPin.user_id == user.id).order_by(UserProjectPin.pin_order.desc()).first()
                order = (max_row[0] + 1) if max_row else 1
            db.add(UserProjectPin(user_id=user.id, project_id=project.id, pin_order=order))
        else:
            if body.pin_order is not None:
                existing.pin_order = body.pin_order
        db.commit()
        return {"slug": project.slug, "pinned": True, "pin_order": (existing.pin_order if existing else order)}
    else:
        if existing:
            db.delete(existing)
            db.commit()
        return {"slug": project.slug, "pinned": False, "pin_order": 0}


class CardReorderBody(BaseModel):
    slugs: list[str]  # ordered list of project slugs


@router.post("/api/v2/projects/reorder-cards")
def reorder_cards(body: CardReorderBody, db: Session = Depends(get_db)):
    """Persist card_order for active project grid on MC Home."""
    for i, slug in enumerate(body.slugs):
        project = db.query(Project).filter(Project.slug == slug).first()
        if project:
            project.card_order = i
    db.commit()
    return {"ok": True, "count": len(body.slugs)}


@router.put("/api/v2/projects/{slug}/resume")
def update_resume(slug: str, body: ResumeUpdate, request: Request = None, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.slug == slug).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if request:
        _resolve_actor(request, db, project_id=project.id)
        _require_write(request, db, project.id)
    project.resume_summary = body.resume_summary
    db.add(ActivityEvent(
        id=str(uuid.uuid4()), project_id=project.id, task_id=None,
        actor_type="agent", action="resume_updated",
        details=json.dumps({"summary": body.resume_summary[:100]}),
    ))
    db.commit()
    return {"id": project.id, "slug": project.slug, "resume_summary": project.resume_summary}


# ── Milestone + Phase CRUD ──

class MilestoneCreate(BaseModel):
    name: str
    label: str = ""
    sort_order: int = 0
    status: str = "active"


class PhaseCreate(BaseModel):
    name: str
    milestone_id: str | None = None
    sort_order: int = 0


@router.get("/api/v2/projects/{slug}/milestones")
@router.get("/api/v2/projects/{slug}/milestones/")
def list_milestones(slug: str, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.slug == slug).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    milestones = db.query(Milestone).filter(Milestone.project_id == project.id).order_by(Milestone.sort_order).all()
    return [{"id": m.id, "label": m.label, "name": m.name, "sort_order": m.sort_order, "status": m.status} for m in milestones]


@router.post("/api/v2/projects/{slug}/milestones", status_code=201)
@router.post("/api/v2/projects/{slug}/milestones/", status_code=201)
def create_milestone(slug: str, body: MilestoneCreate, request: Request = None, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.slug == slug).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if request:
        _resolve_actor(request, db, project_id=project.id)
        _require_write(request, db, project.id)
    # IC-017 (R2.5): label defaults to full `name` when not explicitly set.
    # Was `body.label or body.name[:3].upper()` which produced "DOC" from
    # "Documentation Complete" — surprising in UI ("DOC Documentation Complete"
    # in gantt header). Caller can still pass an explicit short label if they
    # want one (e.g. body.label="DOC"); default is faithful to the name.
    m = Milestone(
        id=str(uuid.uuid4()), project_id=project.id,
        name=body.name, label=body.label or body.name,
        sort_order=body.sort_order, status=body.status,
    )
    db.add(m)
    # IC-015 (R2.5): auto-General-phase REMOVED. Was unconditionally adding
    # `Phase(name="General", milestone_id=m.id)` here — pollutes phase list,
    # creates appearance of agent-violated-IC-005 when agent did the right
    # thing, and biased our Codex run #2 diagnosis. Callers that genuinely
    # want a default phase can POST /phases explicitly.
    db.commit()
    return {"id": m.id, "name": m.name, "label": m.label}


@router.get("/api/v2/projects/{slug}/phases")
@router.get("/api/v2/projects/{slug}/phases/")
def list_phases(slug: str, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.slug == slug).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    phases = db.query(Phase).filter(Phase.project_id == project.id).order_by(Phase.sort_order).all()
    result = []
    for p in phases:
        ml = ""
        if p.milestone_id:
            ms = db.query(Milestone).filter(Milestone.id == p.milestone_id).first()
            ml = ms.label if ms else ""
        result.append({"id": p.id, "name": p.name, "milestone_id": p.milestone_id, "milestone_label": ml, "sort_order": p.sort_order})
    return result


@router.post("/api/v2/projects/{slug}/phases", status_code=201)
@router.post("/api/v2/projects/{slug}/phases/", status_code=201)
def create_phase(slug: str, body: PhaseCreate, request: Request = None, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.slug == slug).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if request:
        _resolve_actor(request, db, project_id=project.id)
        _require_write(request, db, project.id)
    p = Phase(
        id=str(uuid.uuid4()), project_id=project.id,
        name=body.name, milestone_id=body.milestone_id, sort_order=body.sort_order,
    )
    db.add(p)
    db.commit()
    return {"id": p.id, "name": p.name, "milestone_id": p.milestone_id}


# ─── VF-356 (R2.7) ─── Phases PATCH + DELETE-rejection ────────────────────
# Was: phases write-once. No PATCH, no DELETE. Onboard-correct phase creation
# produced unusable Gantt because the Documentation Complete milestone could
# only link to the FINAL bookend phase per step 5; the rest stayed unlinked
# and there was no API recovery path. See CUSTOMER-ONBOARD-FINDINGS IC-028
# + CUSTOMER-ONBOARD-EXTERNAL-REVIEW-2 §6.1 + §3.9 for the severity rationale
# (Gantt is the human's primary vibe-enforcement instrument; bugs that
# pollute it degrade the framework's load-bearing surface).
#
# Now: PATCH supports name + milestone_id + sort_order with required reason
# (>=10 chars) per VF-355 audit-quality discipline. DELETE explicitly
# rejected with a 422 that carries agent_remedy per the pinned
# 422-recoverable principle (memory feedback_422_recoverable_from_response_alone).

class PhaseUpdate(BaseModel):
    """PATCH body for phases. All field updates require `reason` (>=10 chars).

    Note: `reason` is `Optional` at the Pydantic layer so the handler can emit
    our standard 422 shape (code + detail + agent_remedy) rather than Pydantic's
    default missing-field shape. Validates >=10 chars in the handler with full
    context per the pinned 422-recoverable principle."""
    model_config = ConfigDict(extra='forbid')  # VF-357: reject undocumented fields with structured 422
    name: str | None = None
    milestone_id: str | None = None  # to clear, send empty string ""
    sort_order: int | None = None
    reason: str | None = None  # validated in handler — see PhaseUpdate docstring


@router.patch("/api/v2/phases/{phase_id}")
def update_phase(phase_id: str, body: PhaseUpdate, request: Request = None, db: Session = Depends(get_db)):
    """VF-356: phases mutability via PATCH (was write-once).

    Audit trail per VF-355 audit-quality discipline:
      - actor_user_id captured on the ActivityEvent (cookie session resolves it).
      - reason >=10 chars required and stored alongside the change in the event details.
      - changes dict captures from->to per field for downstream agents reading
        the project's audit trail.

    Per the pinned 422-recoverable principle (PK 2026-05-01 pm citing review
    §3.7.3): every error response carries code + detail + agent_remedy + the
    human_visible flag, so the agent can recover from the response alone."""
    phase = db.query(Phase).filter(Phase.id == phase_id).first()
    if not phase:
        raise HTTPException(status_code=404, detail="Phase not found")
    if request:
        _resolve_actor(request, db, project_id=phase.project_id)
        _require_write(request, db, phase.project_id)

    # Reason min length per audit-quality discipline (matches blocked_by_reason VF-304 pattern).
    reason_clean = (body.reason or "").strip()
    if len(reason_clean) < 10:
        raise HTTPException(status_code=422, detail=json.dumps({
            "code": "REASON_REQUIRED",
            "detail": "Phase PATCH requires `reason` field (>=10 chars) explaining why the change is being made. Reason is captured in the audit trail and surfaced in the project lifecycle for downstream agents reading project history.",
            "agent_remedy": "Re-send the PATCH with a `reason` field describing the operator intent (e.g. 'reassigning to milestone B per project re-shape', 'rename for clarity after planning revision').",
            "human_visible": True,
        }))

    has_change = body.name is not None or body.milestone_id is not None or body.sort_order is not None
    if not has_change:
        raise HTTPException(status_code=422, detail=json.dumps({
            "code": "NO_MUTATION_FIELDS",
            "detail": "PATCH body must include at least one of: `name`, `milestone_id`, `sort_order`.",
            "agent_remedy": "Include the field(s) you want to change. To clear `milestone_id` (unlink phase from milestone), send empty string \"\".",
            "human_visible": True,
        }))

    # Validate milestone_id if set: must exist + belong to same project.
    new_milestone_id = body.milestone_id
    if new_milestone_id is not None:
        if new_milestone_id == "":
            new_milestone_id = None  # explicit clear via empty string
        else:
            ms = db.query(Milestone).filter(Milestone.id == new_milestone_id).first()
            if not ms:
                raise HTTPException(status_code=422, detail=json.dumps({
                    "code": "MILESTONE_NOT_FOUND",
                    "detail": f"Milestone '{new_milestone_id}' not found.",
                    "agent_remedy": "GET /api/v2/projects/{slug}/milestones to enumerate available milestones for this project; pass a valid `id` (not `label` or `name`).",
                    "human_visible": True,
                }))
            if ms.project_id != phase.project_id:
                raise HTTPException(status_code=422, detail=json.dumps({
                    "code": "MILESTONE_WRONG_PROJECT",
                    "detail": f"Milestone '{new_milestone_id}' belongs to a different project. Phases can only link to milestones within the same project.",
                    "agent_remedy": "Pick a milestone from the same project as the phase, or create a new milestone on this project first via POST /api/v2/projects/{slug}/milestones.",
                    "human_visible": True,
                }))

    # Capture from->to per changed field for the audit row.
    changes = {}
    if body.name is not None and body.name != phase.name:
        changes["name"] = {"from": phase.name, "to": body.name}
        phase.name = body.name
    if body.milestone_id is not None and new_milestone_id != phase.milestone_id:
        changes["milestone_id"] = {"from": phase.milestone_id, "to": new_milestone_id}
        phase.milestone_id = new_milestone_id
    if body.sort_order is not None and body.sort_order != phase.sort_order:
        changes["sort_order"] = {"from": phase.sort_order, "to": body.sort_order}
        phase.sort_order = body.sort_order

    if not changes:
        return {
            "id": phase.id, "name": phase.name,
            "milestone_id": phase.milestone_id, "sort_order": phase.sort_order,
            "no_op": True,
            "note": "PATCH body fields all matched existing values; no changes applied (audit row not stamped).",
        }

    actor_user = _cookie_user(request, db) if request else None
    actor_user_id = actor_user.id if actor_user else None
    db.add(ActivityEvent(
        id=str(uuid.uuid4()), project_id=phase.project_id, task_id=None,
        actor_type="human" if actor_user_id else "agent",
        actor_user_id=actor_user_id,
        action="phase_updated",
        details=json.dumps({
            "phase_id": phase.id, "phase_name": phase.name,
            "changes": changes, "reason": reason_clean,
        }),
    ))
    db.commit()
    return {
        "id": phase.id, "name": phase.name,
        "milestone_id": phase.milestone_id, "sort_order": phase.sort_order,
        "applied_changes": changes,
    }


@router.delete("/api/v2/phases/{phase_id}")
def delete_phase(phase_id: str, request: Request = None, db: Session = Depends(get_db)):
    """VF-356: explicit DELETE-rejection. Phases are append-only by design;
    they hold task FKs and the cascade behaviour (orphan tasks vs. cascade
    delete) is the wrong choice for either default. The 422 carries
    agent_remedy that names what to do instead — per the pinned principle:
    the response is itself the recovery path, no human escalation needed.

    Existence check first so the agent gets a meaningful 404 if the phase
    id is wrong (cheaper signal than the 422 explanation when the phase is
    just gone)."""
    phase = db.query(Phase).filter(Phase.id == phase_id).first()
    if not phase:
        raise HTTPException(status_code=404, detail="Phase not found")
    raise HTTPException(status_code=422, detail=json.dumps({
        "code": "PHASE_NOT_DELETABLE",
        "detail": "Phases are append-only; DELETE is not supported. Phases hold task FKs, so deletion would either orphan tasks or cascade unexpectedly. The board treats phases as durable structural elements.",
        "agent_remedy": (
            "If a phase is no longer needed: "
            "(a) PATCH the phase via PATCH /api/v2/phases/{id} with `name` renamed to mark it deprecated (e.g. '[DEPRECATED] OldName'), "
            "(b) reassign tasks to a different phase via PATCH /api/v2/tasks/{id} with `phase_id` set, "
            "(c) when phase status='archived' is supported (future), set status to archive. "
            "Do NOT attempt DELETE."
        ),
        "human_visible": True,
    }))


class ArchiveRequest(BaseModel):
    reason_type: str  # "completed" or "abandoned"
    reason: str
    force: bool = False  # bypass open-task check (logged in lifecycle)


class ReopenRequest(BaseModel):
    reason: str


class MilestoneClose(BaseModel):
    reason: str = ""


def _append_lifecycle(project: Project, action: str, reason: str, actor: str = "human"):
    """Append an immutable entry to the project lifecycle log."""
    log = json.loads(project.lifecycle_log or "[]")
    log.append({
        "action": action,
        "reason": reason,
        "actor": actor,
        "at": datetime.now().isoformat(),
    })
    project.lifecycle_log = json.dumps(log)


@router.get("/api/v2/projects/{slug}/archive-summary")
def archive_summary(slug: str, db: Session = Depends(get_db)):
    """Pre-archive summary: open milestones, phases, tasks."""
    project = db.query(Project).filter(Project.slug == slug).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    open_tasks = db.query(Task).filter(
        Task.project_id == project.id,
        Task.status.notin_(["done", "cancelled"]),
        Task.owner_label != "phase",
    ).all()

    open_milestones = db.query(Milestone).filter(
        Milestone.project_id == project.id,
        Milestone.status != "done",
    ).all()

    # Group open tasks by status
    by_status = {}
    for t in open_tasks:
        by_status.setdefault(t.status, []).append({"id": t.id, "title": t.title})

    return {
        "can_archive": len(open_tasks) == 0,
        "open_task_count": len(open_tasks),
        "open_tasks_by_status": {s: len(ts) for s, ts in by_status.items()},
        "open_milestone_count": len(open_milestones),
        "open_milestones": [{"id": m.id, "label": m.label, "name": m.name} for m in open_milestones],
        "total_tasks": db.query(Task).filter(Task.project_id == project.id, Task.owner_label != "phase").count(),
        "done_tasks": db.query(Task).filter(Task.project_id == project.id, Task.status == "done").count(),
    }


class CompleteRequest(BaseModel):
    summary: str  # completion summary — what was achieved
    force: bool = False  # bypass milestone check


@router.post("/api/v2/projects/{slug}/complete")
def complete_project(slug: str, body: CompleteRequest, request: Request = None, db: Session = Depends(get_db)):
    """Mark project as completed. All tasks must be resolved, all milestones closed."""
    project = db.query(Project).filter(Project.slug == slug).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if request:
        _require_admin(request, db, project.id)
    if project.status != "active":
        raise HTTPException(status_code=422, detail=f"Project is {project.status}, not active")
    if not body.summary.strip():
        raise HTTPException(status_code=422, detail="Completion summary is required")

    open_tasks = db.query(Task).filter(
        Task.project_id == project.id,
        Task.status.notin_(["done", "cancelled"]),
        Task.owner_label != "phase",
    ).count()
    if open_tasks > 0:
        raise HTTPException(status_code=422,
            detail=f"Cannot complete: {open_tasks} open tasks remain.")

    open_milestones = db.query(Milestone).filter(
        Milestone.project_id == project.id,
        Milestone.status != "done",
    ).all()
    if open_milestones and not body.force:
        names = ", ".join(f"{m.label} ({m.name})" for m in open_milestones)
        raise HTTPException(status_code=422,
            detail=f"Cannot complete: {len(open_milestones)} open milestones: {names}")

    force_note = f" [FORCED — {len(open_milestones)} open milestones bypassed]" if open_milestones and body.force else ""
    project.status = "completed"
    project.resume_summary = body.summary
    _u = _cookie_user(request, db) if request else None
    _actor = _u.display_name if _u else "Human"
    _append_lifecycle(project, "completed", f"{body.summary}{force_note}", actor=_actor)
    db.add(ActivityEvent(
        id=str(uuid.uuid4()), project_id=project.id, task_id=None,
        actor_type="human", actor_user_id=(_u.id if _u else None),
        action="project_completed",
        details=json.dumps({"summary": body.summary[:200], "actor": _actor}),
    ))
    db.commit()
    return {"id": project.id, "slug": project.slug, "status": project.status}


@router.post("/api/v2/projects/{slug}/archive")
def archive_project(slug: str, body: ArchiveRequest, request: Request = None, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.slug == slug).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if request:
        _require_admin(request, db, project.id)
    if body.reason_type not in ("completed", "abandoned"):
        raise HTTPException(status_code=422, detail="reason_type must be 'completed' or 'abandoned'")
    if not body.reason.strip():
        raise HTTPException(status_code=422, detail="Reason is required")

    # Strict: block if open tasks (unless force bypass)
    open_tasks = db.query(Task).filter(
        Task.project_id == project.id,
        Task.status.notin_(["done", "cancelled"]),
        Task.owner_label != "phase",
    ).count()
    if open_tasks > 0 and not body.force:
        raise HTTPException(status_code=422,
            detail=f"Cannot archive: {open_tasks} open tasks. Resolve all tasks to done or cancelled first.")

    # Enforce reason_type from reality — open tasks = abandoned, no exceptions
    if open_tasks > 0:
        actual_reason = "abandoned"
    else:
        actual_reason = "completed"

    force_note = f" [FORCED — {open_tasks} open tasks bypassed]" if open_tasks > 0 and body.force else ""
    if actual_reason != body.reason_type:
        force_note += f" [reason_type corrected: {body.reason_type} → {actual_reason}]"
    project.status = "archived"
    project.archived_reason = actual_reason
    project.archived_at = datetime.now()
    _u = _cookie_user(request, db) if request else None
    _actor = _u.display_name if _u else "Human"
    _append_lifecycle(project, "archived", f"[{actual_reason}] {body.reason}{force_note}", actor=_actor)
    db.add(ActivityEvent(
        id=str(uuid.uuid4()), project_id=project.id, task_id=None,
        actor_type="human", actor_user_id=(_u.id if _u else None),
        action="project_archived",
        details=json.dumps({"type": actual_reason, "reason": body.reason, "actor": _actor}),
    ))
    db.commit()
    return {"id": project.id, "slug": project.slug, "status": project.status, "archived_reason": actual_reason}


@router.post("/api/v2/projects/{slug}/reopen")
def reopen_project(slug: str, body: ReopenRequest, request: Request = None, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.slug == slug).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if request:
        _require_admin(request, db, project.id)
    if project.status not in ("archived", "completed"):
        raise HTTPException(status_code=422, detail="Project is not archived or completed")
    if not body.reason.strip():
        raise HTTPException(status_code=422, detail="Reason is required")

    _u = _cookie_user(request, db) if request else None
    _actor = _u.display_name if _u else "Human"
    _append_lifecycle(project, "reopened", body.reason, actor=_actor)
    project.status = "active"
    project.archived_at = None
    # Keep archived_reason for history — lifecycle_log has the full trail
    db.add(ActivityEvent(
        id=str(uuid.uuid4()), project_id=project.id, task_id=None,
        actor_type="human", actor_user_id=(_u.id if _u else None),
        action="project_reopened",
        details=json.dumps({"reopen_reason": body.reason, "actor": _actor}),
    ))
    db.commit()
    return {"id": project.id, "slug": project.slug, "status": project.status}


# ── Project Dashboard (for drawer) ──

@router.get("/api/v2/projects/{slug}/dashboard")
def project_dashboard(slug: str, request: Request = None, db: Session = Depends(get_db)):
    """All data needed for the project detail drawer (PC-03 layout)."""
    from app.models.user import User
    from app.models.agent import Agent
    from sqlalchemy import func

    project = db.query(Project).filter(Project.slug == slug).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    # GATE: Cross-project leak fix — agent scoped, humans must be members (VF-193)
    if request:
        _resolve_actor(request, db, project_id=project.id)
        _check_human_project_access(request, db, project.id)

    # Task stats
    rows = (
        db.query(Task.status, func.count())
        .filter(Task.project_id == project.id, Task.owner_label != "phase")
        .group_by(Task.status)
        .all()
    )
    stats = {r[0]: r[1] for r in rows}
    total = sum(stats.values())
    done = stats.get("done", 0)
    cancelled = stats.get("cancelled", 0)
    resolved = done + cancelled

    # Milestones with task counts
    milestones_db = (
        db.query(Milestone)
        .filter(Milestone.project_id == project.id)
        .order_by(Milestone.sort_order)
        .all()
    )
    ms_data = []
    for ms in milestones_db:
        ms_tasks = (
            db.query(Task.status)
            .join(Phase, Task.phase_id == Phase.id)
            .filter(Phase.milestone_id == ms.id, Task.owner_label != "phase")
            .all()
        )
        ms_total = len(ms_tasks)
        ms_resolved = sum(1 for t in ms_tasks if t[0] in ("done", "cancelled"))
        ms_data.append({
            "id": ms.id, "label": ms.label, "name": ms.name,
            "status": ms.status or "active",
            "done": ms_resolved, "total": ms_total,
        })

    # Members
    members_raw = db.query(ProjectMember).filter(ProjectMember.project_id == project.id).all()
    user_map = {u.id: u for u in db.query(User).all()}
    agent_map = {a.id: a for a in db.query(Agent).all()}
    members = []
    admin_contacts: list[str] = []
    # Project owner first in admin contacts
    if project.owner_id and project.owner_id in user_map:
        admin_contacts.append(user_map[project.owner_id].display_name)
    for m in members_raw:
        if m.user_id and m.user_id in user_map:
            u = user_map[m.user_id]
            members.append({"id": m.id, "name": u.display_name, "role": m.role, "type": "human",
                            "user_id": u.id, "username": u.username})
            if m.role == "admin" and u.display_name not in admin_contacts:
                admin_contacts.append(u.display_name)
        elif m.agent_id and m.agent_id in agent_map:
            a = agent_map[m.agent_id]
            members.append({"id": m.id, "name": a.name, "role": m.role, "type": "agent",
                            "agent_id": a.id, "agent_slug": a.slug})

    # Resolve caller's effective role on this project for UI gating
    my_role = _human_project_role(request, db, project.id) if request else None

    # Per-user pin (independent of legacy Project.pinned)
    from app.models.user_project_pin import UserProjectPin
    my_pinned = False
    _u_for_pin = _cookie_user(request, db) if request else None
    if _u_for_pin:
        my_pinned = db.query(UserProjectPin).filter(
            UserProjectPin.user_id == _u_for_pin.id,
            UserProjectPin.project_id == project.id,
        ).first() is not None

    return {
        "slug": project.slug,
        "name": project.name,
        "status": project.status,
        "description": project.description or "",
        "resume_summary": project.resume_summary or "",
        "root_path": project.root_path or "",
        "docs_path": project.docs_path or "",
        "project_url": project.project_url or "",
        "project_number": project.project_number,
        "prefix": project.prefix or "",
        "owner_id": project.owner_id,  # VF-286: UI needs to distinguish owner-row from admin-member
        "full_id": f"PRJ{project.project_number:05d}" if project.project_number else None,
        "tasks": {
            "total": total,
            "done": done,
            "cancelled": cancelled,
            "resolved": resolved,
            "backlog": stats.get("backlog", 0),
            "in_progress": stats.get("in_progress", 0),
            "needs_review": stats.get("needs_review", 0),
            "blocked": stats.get("blocked", 0),
            "pct": round(resolved / total * 100) if total > 0 else 0,
            "ready_to_complete": resolved == total and total > 0,
        },
        "milestones": ms_data,
        "members": members,
        "admin_contacts": admin_contacts,
        "my_role": my_role,
        "pinned": my_pinned,
        "lifecycle_log": json.loads(project.lifecycle_log or "[]"),
    }


# Milestone lifecycle
@router.post("/api/v2/milestones/{milestone_id}/close")
def close_milestone(milestone_id: str, body: MilestoneClose, db: Session = Depends(get_db)):
    ms = db.query(Milestone).filter(Milestone.id == milestone_id).first()
    if not ms:
        raise HTTPException(status_code=404, detail="Milestone not found")

    # Strict: block if incomplete tasks
    ms_phases = db.query(Phase.id).filter(Phase.milestone_id == ms.id).all()
    phase_ids = [p[0] for p in ms_phases]
    if phase_ids:
        open_tasks = db.query(Task).filter(
            Task.phase_id.in_(phase_ids),
            Task.status.notin_(["done", "cancelled"]),
        ).count()
        if open_tasks > 0:
            raise HTTPException(status_code=422,
                detail=f"Cannot close milestone: {open_tasks} incomplete tasks. Resolve all tasks first.")

    ms.status = "done"
    db.add(ActivityEvent(
        id=str(uuid.uuid4()), project_id=ms.project_id, task_id=None,
        actor_type="human", action="milestone_closed",
        details=json.dumps({"milestone": ms.name, "label": ms.label, "reason": body.reason}),
    ))
    db.commit()
    return {"id": ms.id, "name": ms.name, "status": ms.status}


@router.post("/api/v2/milestones/{milestone_id}/reopen")
def reopen_milestone(milestone_id: str, body: ReopenRequest, db: Session = Depends(get_db)):
    ms = db.query(Milestone).filter(Milestone.id == milestone_id).first()
    if not ms:
        raise HTTPException(status_code=404, detail="Milestone not found")
    if not body.reason.strip():
        raise HTTPException(status_code=422, detail="Reason is required")

    ms.status = "active"
    db.add(ActivityEvent(
        id=str(uuid.uuid4()), project_id=ms.project_id, task_id=None,
        actor_type="human", action="milestone_reopened",
        details=json.dumps({"milestone": ms.name, "reason": body.reason}),
    ))
    db.commit()
    return {"id": ms.id, "name": ms.name, "status": ms.status}


# --- Schemas ---

class TaskOut(BaseModel):
    id: str
    project_id: str
    task_number: int | None = None
    short_id: str | None = None
    full_id: str | None = None
    title: str
    short_description: str = ""
    description: str
    status: str
    priority: str
    owner_label: str
    sort_order: int
    external_number: int | None
    parent_task_id: str | None
    milestone_label: str | None
    start_date: date | None = None
    due_date: date | None = None
    phase_id: str | None = None
    phase_label: str | None = None
    task_type: str | None = None
    blocked_by_task_id: str | None = None
    abandoned_note: str = ""
    has_active_drift_flag: bool = False
    created_at: datetime
    updated_at: datetime


VALID_TASK_TYPES = {"feature", "bug", "chore", "spike", "verification"}


class TaskCreate(BaseModel):
    title: str
    short_description: str = ""
    description: str = ""
    status: str = "backlog"
    priority: str = "medium"
    owner_label: str = "agent"
    milestone_label: str | None = None
    task_type: str | None = None
    phase_id: str | None = None
    # IC-036 wave-1.8.4 (PHASE_REQUIRED_ON_CREATE): deliberate-Triage escape
    # hatch for agents. If agent omits phase_id (or it resolves to default
    # Triage), server requires transition_note >=30 chars explaining why
    # Triage is the deliberate placement. Same anti-sycophancy floor as
    # docs_state's "skipped + 30-char rationale" pattern. Humans not gated.
    transition_note: str | None = None


class TaskPatch(BaseModel):
    # VF-357 (was IC-029): extra='forbid' rejects unknown fields with a
    # structured 422 (was: silent drop on 200 — Claude Code review §6.2,
    # 'worst kind of API gap'). Sending milestone_label or any non-listed
    # field now returns 422 with `code: FIELD_NOT_ALLOWED_ON_PATCH` and
    # an agent_remedy pointing at the supersede-and-recreate alternative
    # OR the sibling phase PATCH for milestone reassignment (VF-356).
    # Translation lives in app/main.py exception handler.
    model_config = ConfigDict(extra='forbid')
    status: str | None = None
    priority: str | None = None
    title: str | None = None
    short_description: str | None = None
    description: str | None = None
    owner_label: str | None = None
    sort_order: int | None = None
    abandoned_note: str | None = None
    start_date: date | None = None
    due_date: date | None = None
    phase_id: str | None = None
    phase_change_reason: str | None = None
    blocked_by_task_id: str | None = None
    blocked_by_reason: str | None = None  # VF-304: required when mutating blocked_by (set/change/clear). Min 10 chars.
    task_type: str | None = None
    transition_note: str | None = None  # mandatory note on status change
    # R2.7 wave 1.8.1: docs assessment on needs_review transition (agents only).
    # docs_state: which of the five outcomes describes what happened to docs in
    #   this work cycle. needed | exists | updated | created | skipped.
    # docs_note: ≥30 chars regardless of state — anti-sycophancy brake against
    #   reflexive 'n/a'. Server validates on agent transition to needs_review;
    #   422 returns standard envelope (code=DOCS_ASSESSMENT_REQUIRED) and the
    #   handler auto-posts a structured TaskNote so the assessment lands in the
    #   visible audit feed, not just the PATCH payload. See contract.py
    #   task_discipline.rules + 0-MD/proposed/DOCS-STATE-ASSESSMENT.md for
    #   rationale, escalation path, and toggle plan.
    docs_state: str | None = None
    docs_note: str | None = None


VALID_DOCS_STATES = {"needed", "exists", "updated", "created", "skipped"}
VALID_STATUSES = {"backlog", "ready", "in_progress", "needs_review", "blocked", "done", "cancelled"}
VALID_PRIORITIES = {"low", "medium", "high", "critical"}

# ITIL-aligned state transitions: {from_status: {allowed_to_statuses}}
# Permissive forward flow, restricted backward flow
VALID_TRANSITIONS = {
    "backlog":      {"ready", "in_progress", "needs_review", "cancelled"},
    "ready":        {"in_progress", "needs_review", "backlog", "cancelled"},
    "in_progress":  {"needs_review", "blocked", "done", "ready", "cancelled"},
    "needs_review": {"in_progress", "done", "ready", "blocked", "cancelled"},  # blocked: human can block during review
    "blocked":      {"in_progress", "ready", "backlog", "cancelled"},
    "done":         {"in_progress", "ready"},  # reopen
    "cancelled":    {"backlog", "ready", "in_progress"},  # reopen
}


# --- Project tasks endpoints ---

# WHY: Both with and without trailing slash — prevents 307 redirect that breaks HTTPS
@router.get("/api/v2/projects/{slug}/tasks/search")
def search_tasks(
    slug: str,
    q: str,
    limit: int = 20,
    request: Request = None,
    db: Session = Depends(get_db),
):
    """VF-305: global task search for the kanban pop-up.

    Ranks matches into three buckets — exact/prefix task_number first, then
    title substring, then description substring. Within each bucket, newest
    `updated_at` first. Total cap `limit` (max 50). Case-insensitive. Scoped
    to this project. Includes done/cancelled tasks so users can find history.

    Each row carries a `match_field` of "number" / "title" / "description" so
    the client knows which bucket it belongs to and how to render (snippet
    highlight for description matches, inline highlight for title).
    """
    project = db.query(Project).filter(Project.slug == slug).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if request:
        _resolve_actor(request, db, project_id=project.id)
        _check_human_project_access(request, db, project.id)

    limit = max(1, min(50, int(limit)))
    q_str = (q or "").strip()
    if not q_str:
        return {"q": "", "total": 0, "rows": []}

    like = f"%{q_str}%"
    q_like_num = f"%{q_str}%"  # substring match on stringified task_number

    base = db.query(Task).filter(Task.project_id == project.id)

    # Bucket 1: exact or substring task_number match (user typed a number)
    num_rows: list[Task] = []
    if q_str.isdigit():
        from sqlalchemy import cast as _cast, String as _String
        num_rows = (base
                    .filter(_cast(Task.task_number, _String).ilike(q_like_num))
                    .order_by(Task.updated_at.desc())
                    .limit(limit)
                    .all())
    num_ids = {t.id for t in num_rows}

    # Bucket 2: title substring (excluding already-matched by number)
    title_rows: list[Task] = []
    if len(num_rows) < limit:
        tq = base.filter(Task.title.ilike(like))
        if num_ids:
            tq = tq.filter(~Task.id.in_(num_ids))
        title_rows = (tq
                      .order_by(Task.updated_at.desc())
                      .limit(limit - len(num_rows))
                      .all())
    title_ids = num_ids | {t.id for t in title_rows}

    # Bucket 3: description substring (excluding already-matched)
    desc_rows: list[Task] = []
    remaining = limit - len(num_rows) - len(title_rows)
    if remaining > 0:
        dq = base.filter(Task.description.ilike(like))
        if title_ids:
            dq = dq.filter(~Task.id.in_(title_ids))
        desc_rows = (dq
                     .order_by(Task.updated_at.desc())
                     .limit(remaining)
                     .all())

    prefix = project.prefix or "VF"
    proj_num = project.project_number

    def _row(t: Task, field: str) -> dict:
        return {
            "id": t.id,
            "task_number": t.task_number,
            "short_id": f"{prefix}-{t.task_number}" if t.task_number else None,
            "full_id": (f"PRJ{proj_num:05d}-TSK{t.task_number:05d}"
                        if proj_num and t.task_number else None),
            "title": t.title,
            "description": (t.description or "")[:600],  # trimmed for transport; client snippets further
            "status": t.status,
            "priority": t.priority,
            "owner_label": t.owner_label or "",
            "match_field": field,
            "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        }

    rows = (
        [_row(t, "number") for t in num_rows]
        + [_row(t, "title") for t in title_rows]
        + [_row(t, "description") for t in desc_rows]
    )
    return {"q": q_str, "total": len(rows), "rows": rows}


@router.get("/api/v2/projects/{slug}/tasks", response_model=list[TaskOut])
@router.get("/api/v2/projects/{slug}/tasks/", response_model=list[TaskOut])
def list_tasks(
    slug: str,
    request: Request = None,
    status: str | None = None,
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter(Project.slug == slug).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    # GATE: Agent scope enforcement + human membership check
    if request:
        _resolve_actor(request, db, project_id=project.id)
        _check_human_project_access(request, db, project.id)

    q = db.query(Task).filter(Task.project_id == project.id)
    if status:
        if status not in VALID_STATUSES:
            raise HTTPException(status_code=422, detail=f"Invalid status: {status}")
        q = q.filter(Task.status == status)

    tasks = q.order_by(Task.sort_order, Task.created_at).all()
    return [_task_out(t, db) for t in tasks]


@router.post("/api/v2/projects/{slug}/tasks", response_model=TaskOut, status_code=201)
@router.post("/api/v2/projects/{slug}/tasks/", response_model=TaskOut, status_code=201)
def create_task(
    slug: str,
    body: TaskCreate,
    request: Request = None,
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter(Project.slug == slug).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.status in ("archived", "completed"):
        raise HTTPException(status_code=422, detail=f"Project is {project.status}. Reopen to make changes.")

    if body.status not in VALID_STATUSES:
        raise HTTPException(status_code=422, detail=f"Invalid status: {body.status}")
    if body.priority not in VALID_PRIORITIES:
        raise HTTPException(status_code=422, detail=f"Invalid priority: {body.priority}")

    # GATE: Agent scope enforcement — agent must belong to this project
    actor_type, actor_name = _resolve_actor(request, db, project_id=project.id) if request else ("human", "Human")
    if actor_type == "agent" and body.status in ("done", "cancelled"):
        raise HTTPException(status_code=422, detail=f"Agents cannot create tasks with status '{body.status}'.")
    # GATE: Human caller needs write access on this project
    if request:
        _require_write(request, db, project.id)

    max_sort = db.query(Task.sort_order).filter(Task.project_id == project.id).order_by(Task.sort_order.desc()).first()
    next_sort = (max_sort[0] + 10 if max_sort else 10)

    # Phase: use body.phase_id if provided, otherwise auto-assign
    phase_id = body.phase_id if body.phase_id else None
    milestone_id = None
    if phase_id:
        # Validate the phase exists and belongs to this project
        valid = db.query(Phase).filter(Phase.id == phase_id, Phase.project_id == project.id).first()
        if not valid:
            raise HTTPException(status_code=422, detail=f"phase_id '{phase_id}' not found in this project.")
    elif body.milestone_label:
        ms = db.query(Milestone).filter(
            Milestone.project_id == project.id,
            Milestone.label == body.milestone_label,
        ).first()
        if ms:
            milestone_id = ms.id
        misc_phase = db.query(Phase).filter(
            Phase.project_id == project.id,
            Phase.milestone_id == (ms.id if ms else None),
            Phase.name == "Miscellaneous",
        ).first()
        if misc_phase:
            phase_id = misc_phase.id
    else:
        triage_phase = db.query(Phase).filter(
            Phase.project_id == project.id,
            Phase.name == "Triage",
        ).first()
        if triage_phase:
            phase_id = triage_phase.id

    # IC-036 wave-1.8.4: PHASE_REQUIRED_ON_CREATE gate. Cross-vendor evidence
    # (Claude Code's prior Flight Tracker run + Codex's current run) shows
    # agents skip Ticket Discipline rule #3 under task pressure (set
    # milestone_label correctly but skip phase_id; server silently defaults
    # to Triage; PK manually re-categorises). Same family of audit-quality
    # required-field enforcement as transition_note >=40 / docs_state >=30.
    # Helper-not-babysitter check: PASSES (consequence real and recurring;
    # gate substitutes for missing agent consequence-loop; deliberate-Triage
    # escape hatch keeps sketching workflow possible). Humans not gated -
    # humans set phases via UI workflows; rule targets agents specifically.
    if actor_type == "agent":
        # Resolve which phase the task is actually landing in. Could be from
        # explicit body.phase_id, milestone-Misc lookup, or the default Triage.
        _final_phase = db.query(Phase).filter(Phase.id == phase_id).first() if phase_id else None
        _is_triage = _final_phase is not None and _final_phase.name == "Triage"
        _no_phase = _final_phase is None
        _deliberate_triage_note = (body.transition_note or "").strip()
        if (_is_triage or _no_phase) and len(_deliberate_triage_note) < 30:
            # Build the available-phases hint for the agent_remedy
            _avail = db.query(Phase.id, Phase.name).filter(Phase.project_id == project.id).all()
            _avail_summary = ", ".join(f"{p.name}" for p in _avail[:8]) if _avail else "(none yet; create one first via POST /projects/{slug}/phases)"
            import json as _json
            raise HTTPException(status_code=422, detail=_json.dumps({
                "code": "PHASE_REQUIRED_ON_CREATE",
                "detail": (
                    "Task creation without an explicit non-Triage phase_id falls into the default "
                    "Triage phase, which is a catch-all not a destination."
                ),
                "human_visible": True,
                "agent_remedy": (
                    "GET /api/v2/projects/" + slug + "/phases to enumerate phases + their milestones, "
                    "then POST again with phase_id set to the matching phase. If Triage IS the "
                    "deliberate placement (e.g. genuinely unsorted intake), include "
                    "transition_note (>=30 chars) explaining why Triage is the correct call here. "
                    "Available phases on this project: " + _avail_summary + "."
                ),
                "available_phases_endpoint": "/api/v2/projects/" + slug + "/phases",
            }))

    # Plain-text gate on title/short_description/description (HTML belongs in notes)
    for fld_name, fld_val, msg in (
        ("title", body.title, "title is plain text — HTML tags not allowed."),
        ("short_description", body.short_description, "short_description is plain text — HTML tags not allowed."),
        ("description", body.description, "description is plain text — HTML tags not allowed. Captured reasoning, lists, sections, and rich content go on the task as a note (POST /tasks/{id}/notes), not in description."),
    ):
        if fld_val and re.search(r'<[a-zA-Z][^>]*>', fld_val):
            raise HTTPException(status_code=422, detail=msg)

    # Auto-assign task_number
    from sqlalchemy import func as sa_func
    max_num = db.query(sa_func.max(Task.task_number)).filter(Task.project_id == project.id).scalar()
    next_num = (max_num or 0) + 1

    task = Task(
        project_id=project.id,
        title=body.title,
        short_description=body.short_description[:120] if body.short_description else "",
        description=body.description,
        status=body.status,
        priority=body.priority,
        owner_label=body.owner_label,
        milestone_label=body.milestone_label,
        milestone_id=milestone_id,
        phase_id=phase_id,
        task_type=body.task_type if body.task_type in VALID_TASK_TYPES else None,
        sort_order=next_sort,
        task_number=next_num,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    broadcast(project.slug, "task.created", {"task_id": task.id, "status": task.status})
    return _task_out(task, db)


@router.get("/api/v2/tasks/{task_id}/relationships")
def task_relationships(task_id: str, request: Request = None, db: Session = Depends(get_db)):
    """VF-304: relationship view for the task drawer's Related tab.

    Returns:
      - blocked_by: {task details, linked_at, linked_by, reason} or None
      - blocks: list of tasks that point at this task as their blocker (reverse)
    """
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if request:
        _resolve_actor(request, db, project_id=task.project_id)
        _check_human_project_access(request, db, task.project_id)

    proj = db.query(Project).filter(Project.id == task.project_id).first()
    prefix = (proj.prefix or "VF") if proj else "VF"
    proj_num = proj.project_number if proj else None

    def _row(t: Task) -> dict:
        return {
            "id": t.id,
            "task_number": t.task_number,
            "short_id": f"{prefix}-{t.task_number}" if t.task_number else None,
            "full_id": (f"PRJ{proj_num:05d}-TSK{t.task_number:05d}"
                        if proj_num and t.task_number else None),
            "title": t.title,
            "description": (t.description or "")[:600],
            "status": t.status,
            "priority": t.priority,
            "owner_label": t.owner_label or "",
            "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        }

    # Blocked-by view (with last-mutation audit for the reason + actor)
    blocked_by_view = None
    if task.blocked_by_task_id:
        blocker = db.query(Task).filter(Task.id == task.blocked_by_task_id).first()
        last_audit = (db.query(ActivityEvent)
            .filter(ActivityEvent.task_id == task.id,
                    ActivityEvent.action.in_(["blocked_by_set", "blocked_by_changed"]))
            .order_by(ActivityEvent.created_at.desc())
            .first())
        audit_details = {}
        if last_audit and last_audit.details:
            try:
                audit_details = json.loads(last_audit.details)
            except (ValueError, TypeError):
                audit_details = {}
        if blocker:
            blocked_by_view = {
                **_row(blocker),
                "linked_at": last_audit.created_at.isoformat() if last_audit else None,
                "linked_by": audit_details.get("actor"),
                "reason": audit_details.get("reason"),
            }

    # Blocks view (reverse) — tasks whose blocked_by_task_id == this task.id
    blockers = (db.query(Task)
                .filter(Task.blocked_by_task_id == task.id)
                .order_by(Task.updated_at.desc())
                .all())
    blocks_view = [_row(t) for t in blockers]

    # Related view — loose many-to-many, stored canonically (lower id in a_id)
    from app.models.task_relationship import TaskRelationship
    related_rows = (db.query(TaskRelationship)
                    .filter((TaskRelationship.a_id == task.id) | (TaskRelationship.b_id == task.id),
                            TaskRelationship.kind == "related")
                    .order_by(TaskRelationship.created_at.desc())
                    .all())
    related_view = []
    for rel in related_rows:
        other_id = rel.b_id if rel.a_id == task.id else rel.a_id
        other = db.query(Task).filter(Task.id == other_id).first()
        if not other:
            continue
        related_view.append({
            **_row(other),
            "linked_at": rel.created_at.isoformat() if rel.created_at else None,
            "linked_by_id": rel.created_by,
            "reason": rel.reason or "",
            "_rel_id": rel.id,
        })

    return {
        "task_id": task.id,
        "blocked_by": blocked_by_view,
        "blocks": blocks_view,
        "related": related_view,
    }


class ReverseBlockerBody(BaseModel):
    target_task_id: str
    reason: str


@router.post("/api/v2/tasks/{task_id}/blocks")
def set_blocks_target(task_id: str, body: ReverseBlockerBody,
                       request: Request = None, db: Session = Depends(get_db)):
    """VF-304: reverse-blocked-by — set the TARGET task's blocked_by_task_id = this task.

    Effectively says "this task blocks the target". Permission-checked on the
    target's project (caller needs write access). Rejects if target already
    has a blocker (preserves 1:1 invariant — caller should offer Related as
    alternative). Shared reason required.
    """
    source = db.query(Task).filter(Task.id == task_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="Task not found")
    target = db.query(Task).filter(Task.id == body.target_task_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Target task not found")
    import json as _json
    if source.id == target.id:
        raise HTTPException(status_code=422,
            detail={"code": "BLOCKED_BY_SELF", "message": "A task cannot block itself."})
    reason = (body.reason or "").strip()
    if len(reason) < 10:
        raise HTTPException(status_code=422,
            detail={"code": "BLOCKED_BY_REASON_REQUIRED",
                    "message": "A reason (minimum 10 characters) is required."})
    if target.blocked_by_task_id:
        # Resolve existing blocker for the UI hint
        existing = db.query(Task).filter(Task.id == target.blocked_by_task_id).first()
        existing_proj = db.query(Project).filter(Project.id == existing.project_id).first() if existing else None
        existing_short = (f"{existing_proj.prefix}-{existing.task_number}"
                          if existing and existing_proj and existing_proj.prefix else None)
        raise HTTPException(status_code=422,
            detail={
                "code": "BLOCKED_BY_TARGET_HAS_BLOCKER",
                "message": (f"{target.title!r} is already blocked by "
                            f"{existing_short or existing.id if existing else 'another task'}. "
                            "Clear that blocker first, or link them as Related instead."),
                "target_id": target.id,
                "existing_blocker_id": target.blocked_by_task_id,
                "existing_blocker_short": existing_short,
            })
    # Cycle check from the target's perspective — walking from source backward
    # must not arrive at target.
    visited = set()
    cursor = source.blocked_by_task_id
    hops = 0
    while cursor and cursor not in visited and hops < 50:
        visited.add(cursor)
        if cursor == target.id:
            raise HTTPException(status_code=422,
                detail={"code": "BLOCKED_BY_CYCLE",
                        "message": "Setting this would create a dependency cycle."})
        cursor = db.query(Task.blocked_by_task_id).filter(Task.id == cursor).scalar()
        hops += 1

    if request:
        _resolve_actor(request, db, project_id=target.project_id)
        _require_write(request, db, target.project_id)

    actor_type, actor_name = ("human", "Human")
    if request:
        actor_type, actor_name = _resolve_actor(request, db, project_id=target.project_id)

    target.blocked_by_task_id = source.id
    # Short-id helper
    def _short(tid):
        if not tid: return None
        row = db.query(Task.task_number, Project.prefix).join(
            Project, Project.id == Task.project_id).filter(Task.id == tid).first()
        return f"{row[1]}-{row[0]}" if row and row[0] else None

    src_short = _short(source.id)
    tgt_short = _short(target.id)
    # Audit on the TARGET (the task whose blocked_by field just changed)
    db.add(ActivityEvent(
        project_id=target.project_id, task_id=target.id, actor_type=actor_type,
        action="blocked_by_set",
        details=_json.dumps({
            "from": None, "to": source.id,
            "from_short": None, "to_short": src_short,
            "reason": reason, "actor": actor_name,
            "via": "reverse_blocks",
        }),
    ))
    # VF-304 dual-write: mirror event on the SOURCE so its history shows the link was added.
    db.add(ActivityEvent(
        project_id=source.project_id, task_id=source.id, actor_type=actor_type,
        action="blocks_added",
        details=_json.dumps({
            "from": None, "to": source.id,  # source is the blocker now
            "from_short": None, "to_short": src_short,
            "target_task_id": target.id, "target_short": tgt_short,
            "reason": reason, "actor": actor_name,
            "via": "reverse_blocks",
        }),
    ))
    db.commit()
    return {"ok": True, "target_id": target.id, "blocked_by": source.id}


class RelatedLinkBody(BaseModel):
    other_task_id: str
    reason: str


@router.post("/api/v2/tasks/{task_id}/related")
def add_related(task_id: str, body: RelatedLinkBody,
                request: Request = None, db: Session = Depends(get_db)):
    """VF-304: add a loose 'related' link between this task and the target.
    Stored canonically (a_id = lower UUID). Required reason. Idempotent on pair.
    """
    from app.models.task_relationship import TaskRelationship
    from datetime import datetime as _dt, timezone as _tz
    import json as _json

    source = db.query(Task).filter(Task.id == task_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="Task not found")
    other = db.query(Task).filter(Task.id == body.other_task_id).first()
    if not other:
        raise HTTPException(status_code=404, detail="Target task not found")
    if source.id == other.id:
        raise HTTPException(status_code=422,
            detail={"code": "RELATED_SELF", "message": "A task cannot be related to itself."})
    reason = (body.reason or "").strip()
    if len(reason) < 10:
        raise HTTPException(status_code=422,
            detail={"code": "RELATED_REASON_REQUIRED",
                    "message": "A reason (minimum 10 characters) is required."})

    if request:
        _resolve_actor(request, db, project_id=source.project_id)
        _require_write(request, db, source.project_id)

    a_id, b_id = (source.id, other.id) if source.id < other.id else (other.id, source.id)
    existing = db.query(TaskRelationship).filter(
        TaskRelationship.a_id == a_id, TaskRelationship.b_id == b_id,
        TaskRelationship.kind == "related",
    ).first()
    if existing:
        raise HTTPException(status_code=409,
            detail={"code": "RELATED_ALREADY_LINKED",
                    "message": "These tasks are already related.",
                    "relationship_id": existing.id})

    actor_type, actor_name = ("human", "Human")
    actor_user_id = None
    if request:
        actor_type, actor_name = _resolve_actor(request, db, project_id=source.project_id)
        caller = _cookie_user(request, db)
        actor_user_id = caller.id if caller else None

    rel = TaskRelationship(
        id=str(uuid.uuid4()), a_id=a_id, b_id=b_id, kind="related",
        reason=reason, created_at=_dt.now(_tz.utc), created_by=actor_user_id,
    )
    db.add(rel)
    # Audit on both tasks so each task's history shows the link
    for tid, pid in [(source.id, source.project_id), (other.id, other.project_id)]:
        db.add(ActivityEvent(
            project_id=pid, task_id=tid, actor_type=actor_type,
            action="related_added",
            details=_json.dumps({
                "other_task_id": other.id if tid == source.id else source.id,
                "relationship_id": rel.id, "reason": reason, "actor": actor_name,
            }),
        ))
    db.commit()
    return {"ok": True, "relationship_id": rel.id}


class RelatedRemoveBody(BaseModel):
    reason: str


@router.delete("/api/v2/tasks/{task_id}/related/{rel_id}")
def remove_related(task_id: str, rel_id: str, body: RelatedRemoveBody = None,
                   request: Request = None, db: Session = Depends(get_db)):
    """VF-304: remove a related link. Required reason (same agent-first strictness)."""
    from app.models.task_relationship import TaskRelationship
    import json as _json
    source = db.query(Task).filter(Task.id == task_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="Task not found")
    rel = db.query(TaskRelationship).filter(TaskRelationship.id == rel_id).first()
    if not rel:
        raise HTTPException(status_code=404, detail="Relationship not found")
    if rel.a_id != source.id and rel.b_id != source.id:
        raise HTTPException(status_code=422,
            detail="Relationship does not belong to this task.")
    reason = (body.reason if body else "") or ""
    reason = reason.strip()
    if len(reason) < 10:
        raise HTTPException(status_code=422,
            detail={"code": "RELATED_REASON_REQUIRED",
                    "message": "A reason (minimum 10 characters) is required to clear a relationship."})

    if request:
        _require_write(request, db, source.project_id)

    actor_type, actor_name = ("human", "Human")
    if request:
        actor_type, actor_name = _resolve_actor(request, db, project_id=source.project_id)

    other_id = rel.b_id if rel.a_id == source.id else rel.a_id
    other = db.query(Task).filter(Task.id == other_id).first()

    for tid, pid in [(source.id, source.project_id),
                     (other.id, other.project_id) if other else (None, None)]:
        if not tid: continue
        db.add(ActivityEvent(
            project_id=pid, task_id=tid, actor_type=actor_type,
            action="related_removed",
            details=_json.dumps({
                "other_task_id": other_id if tid == source.id else source.id,
                "relationship_id": rel.id, "reason": reason, "actor": actor_name,
            }),
        ))
    db.delete(rel)
    db.commit()
    return {"ok": True}


@router.get("/api/v2/tasks/{task_id}", response_model=TaskOut)
def get_task(task_id: str, request: Request = None, db: Session = Depends(get_db)):
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if request:
        _resolve_actor(request, db, project_id=task.project_id)
        _check_human_project_access(request, db, task.project_id)
    return _task_out(task, db)


@router.patch("/api/v2/tasks/{task_id}", response_model=TaskOut)
def patch_task(
    task_id: str,
    body: TaskPatch,
    request: Request = None,
    db: Session = Depends(get_db),
):
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    # Block edits on archived/completed projects
    proj = db.query(Project.status).filter(Project.id == task.project_id).first()
    if proj and proj[0] in ("archived", "completed"):
        raise HTTPException(status_code=422, detail=f"Project is {proj[0]}. Reopen to make changes.")

    import json as _json

    # Track what changed for audit
    old_status = task.status
    old_phase_id = task.phase_id
    old_title = task.title
    old_priority = task.priority
    old_owner = task.owner_label
    old_description = task.description
    old_blocked_by = task.blocked_by_task_id
    old_start_date = str(task.start_date) if task.start_date else None
    old_due_date = str(task.due_date) if task.due_date else None

    # GATE: Resolve actor + agent scope enforcement
    actor_type, actor_name = _resolve_actor(request, db, project_id=task.project_id) if request else ("human", "Human")
    # GATE: Human caller needs write access on this project
    if request:
        _require_write(request, db, task.project_id)

    # WHY: Stamp actor identity on every event this request creates so the activity feed
    # can attribute correctly. Resolve once, mix into every details payload below.
    _patch_user = _cookie_user(request, db) if request else None
    _patch_actor_user_id = _patch_user.id if _patch_user else None
    _patch_actor_token_id = None
    if actor_type == "agent":
        from app.models.agent import Agent as _AgentLookup
        # GATE: scope by project to avoid cross-project agent leak (VF-285).
        # Without this, Agent.name collisions across projects (e.g. two 'Claude'
        # agents on different projects) resolve to whichever row was inserted
        # first — stamping the wrong actor_token_id on activity_events.
        ag = (db.query(_AgentLookup)
              .filter(_AgentLookup.name == actor_name,
                      _AgentLookup.project_id == task.project_id)
              .first())
        if ag:
            _patch_actor_token_id = ag.id

    def _evt_details(d: dict) -> str:
        d = dict(d)
        d.setdefault("actor", actor_name)
        return _json.dumps(d)

    if body.status is not None:
        if body.status not in VALID_STATUSES:
            raise HTTPException(status_code=422, detail=f"Invalid status: {body.status}")
        if body.status == "cancelled" and not (body.abandoned_note or "").strip():
            raise HTTPException(status_code=422, detail="abandoned_note is required when cancelling a task")
        # State transition validation
        if body.status != old_status:
            allowed = VALID_TRANSITIONS.get(old_status, set())
            if body.status not in allowed:
                raise HTTPException(status_code=422,
                    detail=f"Invalid transition: {old_status} -> {body.status}. Allowed: {', '.join(sorted(allowed))}")
            # IC-TAN-003 (R2.5): Human-Closure Discipline — HARD rule.
            # Agents cannot transition tasks to `done` or `cancelled`. Closure
            # ceremony belongs to the human (operator-agency loop). Server-
            # enforced; no per-project opt-out, no agent-delegation flag.
            # Agent path: move to needs_review with a closure note + recommendation;
            # human evaluates + closes. Codex's external review identified the
            # closure ceremony as the load-bearing operator-agency loop:
            # making it optional invites collapse into machine-operated fiction.
            if actor_type == "agent" and body.status == "done":
                raise HTTPException(status_code=422, detail=_json.dumps({
                    "code": "HUMAN_CLOSURE_REQUIRED",
                    "detail": "Tasks must be moved to `done` by a human. Move to `needs_review` with a closure note + recommendation; let the human close. Per Human-Closure Discipline (OUR-block Ticket Discipline). The closure ceremony is the human-enforcement loop and cannot be delegated.",
                    "human_visible": True,
                    "agent_remedy": "PATCH the task to status=needs_review with owner_label='human:<reviewer>' and a completion note. Then notify the human.",
                }))
            if actor_type == "agent" and body.status == "cancelled":
                raise HTTPException(status_code=422, detail=_json.dumps({
                    "code": "HUMAN_CLOSURE_REQUIRED",
                    "detail": "Tasks must be moved to `cancelled` by a human. Move to `needs_review` with a note explaining why cancellation is recommended; human decides. Per Human-Closure Discipline (OUR-block Ticket Discipline).",
                    "human_visible": True,
                    "agent_remedy": "PATCH the task to status=needs_review with owner_label='human:<reviewer>' and an abandoned_note explaining the reasoning. Then notify the human.",
                }))
            # Agent moving to needs_review must EXPLICITLY reassign to a human
            # reviewer in this PATCH. CONTRACT_VERSION 2.14.1 (post-Codex live
            # gap): the prior gate accepted body.owner_label OR task.owner_label
            # OR "" — so an agent could skip owner_label in the PATCH body and
            # ride a stale human owner from a prior assignment. That defeats
            # the "active handoff" intent (every needs_review = explicit reassign,
            # so the agent thinks about WHO is reviewing). Tightened: require
            # body.owner_label present + format 'human:<non-empty Display Name>'.
            # The colon-required check also prevents bare "human" or accidental
            # "humanitarian" matches that the prior startswith("human") allowed.
            if actor_type == "agent" and body.status == "needs_review":
                supplied = (body.owner_label or "").strip()
                if not supplied:
                    raise HTTPException(status_code=422, detail={
                        "code": "NEEDS_REVIEW_OWNER_REQUIRED",
                        "message": "needs_review transitions require an explicit owner_label in this PATCH body. Don't rely on whoever was assigned before — actively reassign every time so the handoff is intentional.",
                        "agent_remedy": "Include owner_label='human:<Display Name>' in your PATCH body (e.g. 'human:Parvez Khan'). Resolve the reviewer from GET /projects/{slug}/members (filter type=human, take .name) and prepend 'human:'. Per Ticket Discipline in your discipline manifest.",
                        "human_visible": True,
                    })
                if not supplied.startswith("human:") or len(supplied) <= len("human:"):
                    raise HTTPException(status_code=422, detail={
                        "code": "NEEDS_REVIEW_OWNER_FORMAT",
                        "message": f"owner_label '{supplied}' is not a valid human reviewer reference. Format must be 'human:<Display Name>' (the 'human:' prefix is required and the name after the colon must be non-empty).",
                        "agent_remedy": "Use 'human:<Display Name>', e.g. 'human:Parvez Khan'. Bare 'human', 'agent:<name>', or any value not starting with 'human:' followed by a non-empty name will be rejected.",
                        "human_visible": True,
                    })
            # Agent cannot move needs_review → blocked (human-only decision)
            if actor_type == "agent" and old_status == "needs_review" and body.status == "blocked":
                raise HTTPException(status_code=422,
                    detail="Agents cannot block tasks in review. Flag the blocker in a note and let the human decide.")
            # Human must have a completion note in the current cycle before moving to done
            if actor_type == "human" and body.status == "done":
                # Find the last reopen event (from done or cancelled to any non-terminal status)
                last_reopen = (db.query(ActivityEvent.created_at)
                    .filter(ActivityEvent.task_id == task_id, ActivityEvent.action == "status_changed")
                    .filter(ActivityEvent.details.contains('"from": "done"') | ActivityEvent.details.contains('"from": "cancelled"'))
                    .order_by(ActivityEvent.created_at.desc())
                    .first())
                cycle_start = last_reopen[0] if last_reopen else task.created_at
                has_completion = (db.query(TaskNote)
                    .filter(TaskNote.task_id == task_id, TaskNote.is_completion_note == True,
                            TaskNote.superseded_at == None,
                            TaskNote.created_at >= cycle_start)
                    .first()) is not None
                if not has_completion:
                    raise HTTPException(status_code=422,
                        detail="A completion note is required before closing. Post a note and mark it as a completion note.")
            # On reopen from done: auto-supersede all active completion notes in the current cycle
            if old_status == "done" and body.status != "done":
                from datetime import datetime, timezone as _tz
                active_completions = (db.query(TaskNote)
                    .filter(TaskNote.task_id == task_id, TaskNote.is_completion_note == True,
                            TaskNote.superseded_at == None)
                    .all())
                for cn in active_completions:
                    cn.superseded_at = datetime.now(_tz.utc)
                    cn.superseded_by = actor_name
                    _proj_prefix = db.query(Project.prefix).filter(Project.id == task.project_id).scalar() or ""
                    _task_short_id = f"{_proj_prefix}-{task.task_number}" if _proj_prefix and task.task_number else str(task.task_number or "")
                    cn.superseded_reason = f"Superseded by reopen ({_task_short_id})"
                    db.add(ActivityEvent(
                        project_id=task.project_id, task_id=task.id, actor_type=actor_type,
                        action="note_superseded",
                        details=_json.dumps({"note_author": cn.author_name, "reason": cn.superseded_reason,
                                             "superseded_by": actor_name, "body_preview": cn.body[:200], "auto": True}),
                    ))
            # Mandatory transition note — no silent status moves
            if not (body.transition_note or "").strip() and not (body.abandoned_note or "").strip():
                raise HTTPException(status_code=422,
                    detail=f"transition_note is required when changing status ({old_status} -> {body.status})")
            # Note-fidelity gate (VF-255): structural quality check on agent transitions.
            # Scope: agent tokens only — humans should not be gated on board moves (friction).
            # Only needs_review and blocked. Cancelled is covered by abandoned_note.
            if actor_type == "agent" and body.status in ("needs_review", "blocked"):
                _note = (body.transition_note or "").strip()
                if len(_note) < 40:
                    raise HTTPException(status_code=422,
                        detail=f"transition_note too short for {body.status} (min 40 chars, got {len(_note)}). Describe what the human needs to look at and why.")
                # Reject duplicate of the previous transition note on this task
                _prev = (db.query(ActivityEvent.details)
                    .filter(ActivityEvent.task_id == task_id, ActivityEvent.action == "status_changed")
                    .order_by(ActivityEvent.created_at.desc())
                    .first())
                if _prev:
                    try:
                        _prev_reason = (_json.loads(_prev[0]) or {}).get("reason", "").strip()
                        if _prev_reason and _prev_reason == _note:
                            raise HTTPException(status_code=422,
                                detail="transition_note duplicates the previous transition note on this task. Write a fresh note describing the current state.")
                    except (ValueError, TypeError):
                        pass
                # blocked: must reference a blocker (either blocked_by_task_id set in this PATCH, already set, or note mentions a blocker keyword)
                if body.status == "blocked":
                    _has_blocker_id = bool(body.blocked_by_task_id or task.blocked_by_task_id)
                    _mentions_blocker = any(k in _note.lower() for k in ("block", "wait", "depend", "pending"))
                    if not _has_blocker_id and not _mentions_blocker:
                        raise HTTPException(status_code=422,
                            detail="blocked transition must set blocked_by_task_id or describe the blocker in the transition_note.")
            # R2.7 wave 1.8.1: docs_state assessment on agent → needs_review.
            # Soft default (PATCH-field, not gate-style). The natural moment a
            # human asks "have you left things with working docs?" is the
            # needs_review handoff; the field forces the agent to answer one of
            # five outcomes + a 30-char rationale REGARDLESS of state. The
            # 30-char floor on every state is the anti-sycophancy brake — agent
            # can't reflexively type "n/a" on `skipped` and move on.
            # Auto-posts a structured TaskNote into the audit feed below
            # (search 'docs_state' below) so the assessment is visible, not just
            # buried in the PATCH payload.
            # Escalation path (per 0-MD/proposed/DOCS-STATE-ASSESSMENT.md):
            # if compliance is low (less-capable model classes drift on it),
            # escalate to gate-style + per-project toggle. Built later as a
            # separate ticket.
            if actor_type == "agent" and body.status == "needs_review":
                _docs_state = (body.docs_state or "").strip().lower()
                _docs_note = (body.docs_note or "").strip()
                if not _docs_state or _docs_state not in VALID_DOCS_STATES:
                    raise HTTPException(status_code=422, detail=_json.dumps({
                        "code": "DOCS_ASSESSMENT_REQUIRED",
                        "detail": (
                            "docs_state is required when an agent transitions a task to "
                            "needs_review. The reviewer needs to know whether the work "
                            "left docs in a working state — silent absence is not an answer."
                        ),
                        "human_visible": True,
                        "agent_remedy": (
                            "Set docs_state to one of: 'needed' (docs need updating but you "
                            "haven't done it; explain why in docs_note), 'exists' (existing "
                            "docs already cover this; no change required), 'updated' (you "
                            "edited an existing doc — name which sections), 'created' (you "
                            "wrote a new doc — name the path), or 'skipped' (deliberately "
                            "not updating; explain the rationale). Also set docs_note "
                            "(>=30 chars) describing what you assessed, not just the choice."
                        ),
                    }))
                if len(_docs_note) < 30:
                    raise HTTPException(status_code=422, detail=_json.dumps({
                        "code": "DOCS_ASSESSMENT_REQUIRED",
                        "detail": (
                            f"docs_note too short for needs_review (min 30 chars, got {len(_docs_note)}). "
                            "The 30-char floor applies regardless of docs_state — including 'skipped' "
                            "and 'exists' — so the reviewer sees what you actually assessed, not just "
                            "your one-word verdict."
                        ),
                        "human_visible": True,
                        "agent_remedy": (
                            "Expand docs_note to >=30 chars. For 'updated': name the doc path + the "
                            "sections you changed. For 'created': name the new doc + audience class "
                            "(internal/public/proposed). For 'exists': name the doc that covers it. "
                            "For 'needed': name the doc gap + why you didn't fix it. For 'skipped': "
                            "explain why no doc work belongs in this scope."
                        ),
                    }))
        task.status = body.status
        # VF-351: terminal-status auto-clear of blocked_by_task_id. Done /
        # cancelled tasks aren't blocked by anything by definition; carrying
        # a stale blocker confuses the drawer UI ("Done but Blocked by …").
        # Historical record is preserved in the audit log + transition_note.
        if body.status in ("done", "cancelled") and task.blocked_by_task_id:
            _stale_blocker = task.blocked_by_task_id
            task.blocked_by_task_id = None
            try:
                from app.models.activity import ActivityEvent as _AE
                db.add(_AE(
                    project_id=task.project_id,
                    actor_type="agent",
                    action="blocked_by_auto_cleared",
                    details=(
                        f"task=" + str(task.id)
                        + " prior_blocker=" + str(_stale_blocker)
                        + " trigger=status_to_" + str(body.status)
                    ),
                ))
            except Exception:
                # Audit failure shouldn't fail the PATCH — the data fix is the priority.
                pass

        # Wave 2.0 (R2.7): first_close_complete substep stamping. When ANY
        # task in this project transitions to 'done' AND the project's
        # onboard_state has agent_md_hash registered (operationally complete)
        # but first_close_complete NOT YET stamped, stamp it now. This is
        # the customer's first close-ceremony completing the wave-2.0 onboard
        # full-completion requirement. Idempotent — only stamps once.
        if body.status == "done":
            try:
                _proj = db.query(Project).filter(Project.id == task.project_id).first()
                if _proj and _proj.onboard_state and _proj.onboard_state.get("agent_md_hash") \
                        and not _proj.onboard_state.get("first_close_complete"):
                    from datetime import datetime as _dt_now, timezone as _tz_now
                    _state = dict(_proj.onboard_state)
                    _state["first_close_complete"] = {
                        "stamped_at": _dt_now.now(_tz_now.utc).isoformat(),
                        "first_closed_task_id": task.id,
                        "actor": actor_name,
                        "actor_type": actor_type,
                    }
                    _proj.onboard_state = _state
                    flag_modified(_proj, "onboard_state")
                    # Honest telemetry: log the stamping
                    db.add(ActivityEvent(
                        project_id=_proj.id, task_id=task.id, actor_type=actor_type,
                        action="first_close_complete_stamped",
                        details=_json.dumps({
                            "first_closed_task_id": task.id,
                            "first_closed_task_short": (lambda r: f"{r[1]}-{r[0]}" if r and r[0] else None)(
                                db.query(Task.task_number, Project.prefix).join(Project, Project.id == Task.project_id).filter(Task.id == task.id).first()
                            ),
                            "actor": actor_name,
                            "trigger": "task_status_done",
                        }),
                    ))
            except Exception as _e:
                # Substep-stamping failure should not fail the PATCH.
                # Onboard full-completion is observability, not a write gate.
                import sys as _sys
                _sys.stderr.write(f"[first_close_complete stamp] failed: {_e!r}\n")
    if body.priority is not None:
        if body.priority not in VALID_PRIORITIES:
            raise HTTPException(status_code=422, detail=f"Invalid priority: {body.priority}")
        task.priority = body.priority
    if body.title is not None:
        if re.search(r'<[a-zA-Z][^>]*>', body.title or ""):
            raise HTTPException(status_code=422, detail="title is plain text — HTML tags not allowed. Use a note for rich content.")
        task.title = body.title
    if body.short_description is not None:
        if re.search(r'<[a-zA-Z][^>]*>', body.short_description or ""):
            raise HTTPException(status_code=422, detail="short_description is plain text — HTML tags not allowed. Use a note for rich content.")
        task.short_description = body.short_description[:120]
    if body.description is not None:
        if re.search(r'<[a-zA-Z][^>]*>', body.description or ""):
            raise HTTPException(status_code=422, detail="description is plain text — HTML tags not allowed. Captured reasoning, lists, sections, and rich content go on the task as a note (POST /tasks/{id}/notes), not in description.")
        task.description = body.description
    if body.owner_label is not None:
        task.owner_label = body.owner_label
    if body.sort_order is not None:
        task.sort_order = body.sort_order
    if body.abandoned_note is not None:
        task.abandoned_note = body.abandoned_note
    if body.start_date is not None:
        task.start_date = body.start_date
    if body.due_date is not None:
        task.due_date = body.due_date
    if body.phase_id is not None and body.phase_id != (old_phase_id or ''):
        if not (body.phase_change_reason or "").strip():
            raise HTTPException(status_code=422, detail="phase_change_reason is required when changing phase")
        task.phase_id = body.phase_id or None
    if body.blocked_by_task_id is not None:
        # VF-304: gate blocked_by mutations — reason required (set/change/clear),
        # cycle detection on non-null values. See user-agent-model.md future bump.
        new_bb_raw = (body.blocked_by_task_id or "").strip()
        new_bb = new_bb_raw or None
        if str(new_bb or '') != str(old_blocked_by or ''):
            # Reason is mandatory on any mutation of blocked_by.
            reason = (body.blocked_by_reason or "").strip()
            if len(reason) < 10:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "BLOCKED_BY_REASON_REQUIRED",
                        "message": (
                            "A reason (minimum 10 characters) is required when setting, "
                            "changing, or clearing blocked_by. Agents lack cross-session "
                            "recall — this captures the audit trail."
                        ),
                    },
                )
            # Cycle detection — walk the blocker chain from the proposed target.
            # If we reach the task we are modifying, that would create a cycle.
            if new_bb:
                if new_bb == task.id:
                    raise HTTPException(
                        status_code=422,
                        detail={"code": "BLOCKED_BY_SELF", "message": "A task cannot block itself."},
                    )
                visited = set()
                cursor = new_bb
                hops = 0
                while cursor and cursor not in visited and hops < 50:
                    visited.add(cursor)
                    if cursor == task.id:
                        raise HTTPException(
                            status_code=422,
                            detail={
                                "code": "BLOCKED_BY_CYCLE",
                                "message": (
                                    "Setting this blocker would create a dependency cycle. "
                                    "Pick a different task or resolve the intermediate chain first."
                                ),
                            },
                        )
                    nxt = db.query(Task.blocked_by_task_id).filter(Task.id == cursor).scalar()
                    cursor = nxt
                    hops += 1
            task.blocked_by_task_id = new_bb
    if body.task_type is not None:
        task.task_type = body.task_type if body.task_type in VALID_TASK_TYPES else None

    # --- Audit logging to activity_events ---
    # actor_type, actor_name already resolved above (before transition validation)

    # Status change
    if body.status is not None and body.status != old_status:
        reason = body.transition_note or ""
        if not reason and body.abandoned_note:
            try:
                log = _json.loads(body.abandoned_note)
                if isinstance(log, list) and log:
                    reason = log[-1].get("reason", "")
            except (ValueError, TypeError):
                reason = body.abandoned_note
        db.add(ActivityEvent(
            project_id=task.project_id, task_id=task.id,
            actor_type=actor_type,
            action="status_changed",
            details=_json.dumps({"from": old_status, "to": body.status, "reason": reason, "actor": actor_name}),
        ))
        # Auto-post transition note as a visible TaskNote — agents only.
        # Agent transitions to needs_review/blocked are gated by note-fidelity (VF-255)
        # so the note is substantive and belongs in the conversation feed. Human
        # transitions from the editor send boilerplate ("Status changed from X to Y
        # via editor") which would pollute the notes feed; for humans the activity
        # event is the right surface, not a TaskNote.
        if reason and actor_type == "agent":
            db.add(TaskNote(
                task_id=task.id, body="<p>" + reason + "</p>",
                author_type=actor_type, author_name=actor_name,
            ))

        # R2.7 wave 1.8.1: structured docs_state assessment auto-post.
        # The PATCH-field carries the data; this note makes it visible in the
        # conversation feed alongside the transition note. Reviewer sees one
        # additional bullet labelled "docs_state: <state>" plus the agent's
        # rationale. Validation already enforced state + length above; we
        # post unconditionally here when state was provided so the audit
        # surface always reflects what was assessed.
        if actor_type == "agent" and body.status == "needs_review" and (body.docs_state or "").strip():
            _ds_state = (body.docs_state or "").strip().lower()
            _ds_note = (body.docs_note or "").strip()
            db.add(TaskNote(
                task_id=task.id,
                body=f"<p><strong>docs_state:</strong> {_ds_state} — {_ds_note}</p>",
                author_type=actor_type, author_name=actor_name,
            ))
            db.add(ActivityEvent(
                project_id=task.project_id, task_id=task.id, actor_type=actor_type,
                action="docs_state_assessed",
                details=_json.dumps({
                    "docs_state": _ds_state,
                    "docs_note": _ds_note,
                    "trigger": "needs_review_transition",
                    "actor": actor_name,
                }),
            ))

        # VF-163: Auto-clear downstream blocked_by when this task closes.
        # Rule: a done/cancelled task cannot "block" anything alive. Any task
        # with blocked_by pointing at us gets that relationship converted to a
        # 'related' link (history preserved) and blocked_by_task_id nulled. The
        # pink "blocked:" pill on the downstream card disappears, and the
        # graph stops asserting something false.
        # Track downstream updates so we can broadcast SSE events after commit —
        # without the broadcast, the downstream cards only refresh on page reload.
        _vf163_auto_cleared = []
        if body.status in ("done", "cancelled"):
            from app.models.task_relationship import TaskRelationship as _TR
            from datetime import datetime as _dt, timezone as _tz
            downstream_tasks = (db.query(Task)
                                .filter(Task.blocked_by_task_id == task.id)
                                .all())
            _my_row = (db.query(Task.task_number, Project.prefix)
                       .join(Project, Project.id == Task.project_id)
                       .filter(Task.id == task.id)
                       .first())
            _my_short = f"{_my_row[1]}-{_my_row[0]}" if _my_row and _my_row[0] else task.id[:8]
            _now = _dt.now(_tz.utc)
            for dt_task in downstream_tasks:
                ids = sorted([dt_task.id, task.id])
                existing = (db.query(_TR)
                            .filter(_TR.a_id == ids[0], _TR.b_id == ids[1], _TR.kind == "related")
                            .first())
                auto_reason = f"Auto-converted — blocker {_my_short} transitioned to {body.status}"
                if not existing:
                    db.add(_TR(
                        a_id=ids[0], b_id=ids[1], kind="related",
                        reason=auto_reason, created_at=_now,
                        created_by=_patch_actor_user_id,
                    ))
                dt_task.blocked_by_task_id = None
                _vf163_auto_cleared.append((dt_task.id, dt_task.project_id, dt_task.status))
                db.add(ActivityEvent(
                    project_id=dt_task.project_id, task_id=dt_task.id, actor_type=actor_type,
                    action="blocked_by_auto_cleared",
                    details=_json.dumps({
                        "blocker_id": task.id,
                        "blocker_short": _my_short,
                        "blocker_transitioned_to": body.status,
                        "converted_to_related": not bool(existing),
                        "reason": auto_reason,
                        "actor": actor_name,
                    }),
                ))
                db.add(ActivityEvent(
                    project_id=task.project_id, task_id=task.id, actor_type=actor_type,
                    action="blocks_auto_cleared",
                    details=_json.dumps({
                        "target_task_id": dt_task.id,
                        "reason": auto_reason,
                        "actor": actor_name,
                    }),
                ))

    # Phase change
    if body.phase_id is not None and body.phase_id != (old_phase_id or ''):
        old_name = "Unassigned"
        new_name = "Unassigned"
        if old_phase_id:
            old_p = db.query(Phase.name).filter(Phase.id == old_phase_id).first()
            if old_p: old_name = old_p[0]
        if task.phase_id:
            new_p = db.query(Phase.name).filter(Phase.id == task.phase_id).first()
            if new_p: new_name = new_p[0]
        db.add(ActivityEvent(
            project_id=task.project_id, task_id=task.id, actor_type=actor_type,
            action="phase_changed",
            details=_json.dumps({"from": old_name, "to": new_name, "reason": (body.phase_change_reason or "").strip()}),
        ))

    # Title change
    if body.title is not None and body.title != old_title:
        db.add(ActivityEvent(
            project_id=task.project_id, task_id=task.id, actor_type=actor_type,
            action="title_changed",
            details=_json.dumps({"from": old_title, "to": body.title}),
        ))

    # Priority change
    if body.priority is not None and body.priority != old_priority:
        db.add(ActivityEvent(
            project_id=task.project_id, task_id=task.id, actor_type=actor_type,
            action="priority_changed",
            details=_json.dumps({"from": old_priority, "to": body.priority}),
        ))

    # Owner change
    if body.owner_label is not None and body.owner_label != old_owner:
        db.add(ActivityEvent(
            project_id=task.project_id, task_id=task.id, actor_type=actor_type,
            action="owner_changed",
            details=_json.dumps({"from": old_owner, "to": body.owner_label}),
        ))

    # Description change
    if body.description is not None and body.description != old_description:
        db.add(ActivityEvent(
            project_id=task.project_id, task_id=task.id, actor_type=actor_type,
            action="description_changed",
            details=_json.dumps({"summary": "Description updated"}),
        ))

    # Blocked by change — VF-304: split into set/changed/cleared actions, carry reason.
    # Dual-write: event lands on the SOURCE task AND on the affected other task(s)
    # (the new blocker, the former blocker, or both on a change).
    if body.blocked_by_task_id is not None and str(body.blocked_by_task_id or '') != str(old_blocked_by or ''):
        new_bb_final = (body.blocked_by_task_id or "").strip() or None
        reason = (body.blocked_by_reason or "").strip()
        if old_blocked_by is None and new_bb_final is not None:
            action = "blocked_by_set"
        elif old_blocked_by is not None and new_bb_final is None:
            action = "blocked_by_cleared"
        else:
            action = "blocked_by_changed"
        # Enrich with short_ids for human-readable audit
        def _short(tid):
            if not tid:
                return None
            row = db.query(Task.task_number, Project.prefix).join(
                Project, Project.id == Task.project_id
            ).filter(Task.id == tid).first()
            if not row:
                return None
            return f"{row[1]}-{row[0]}" if row[0] else None

        src_short = _short(task.id)
        from_short = _short(old_blocked_by)
        to_short = _short(new_bb_final)
        base_details = {
            "from": old_blocked_by, "to": new_bb_final,
            "from_short": from_short, "to_short": to_short,
            "reason": reason, "actor": actor_name,
        }
        # 1) Event on SOURCE (the task whose blocked_by field changed)
        db.add(ActivityEvent(
            project_id=task.project_id, task_id=task.id, actor_type=actor_type,
            action=action, details=_json.dumps(base_details),
        ))
        # 2) Mirror event on the OTHER task(s). The mirror uses reverse-direction
        #    action names ("blocks_*") so reading either task's audit log tells the
        #    correct causal story from that task's point of view.
        #    On "set": write blocks_added on the new blocker.
        #    On "cleared": write blocks_removed on the former blocker.
        #    On "changed": write blocks_removed on old + blocks_added on new.
        def _mirror(target_id: str, mirror_action: str):
            t_other = db.query(Task).filter(Task.id == target_id).first()
            if not t_other:
                return
            db.add(ActivityEvent(
                project_id=t_other.project_id, task_id=t_other.id, actor_type=actor_type,
                action=mirror_action,
                details=_json.dumps({
                    **base_details,
                    "source_task_id": task.id,
                    "source_short": src_short,
                }),
            ))
        if action == "blocked_by_set" and new_bb_final:
            _mirror(new_bb_final, "blocks_added")
        elif action == "blocked_by_cleared" and old_blocked_by:
            _mirror(old_blocked_by, "blocks_removed")
        elif action == "blocked_by_changed":
            if old_blocked_by:
                _mirror(old_blocked_by, "blocks_removed")
            if new_bb_final:
                _mirror(new_bb_final, "blocks_added")

    # Start date change
    new_start = str(body.start_date) if body.start_date else None
    if body.start_date is not None and new_start != old_start_date:
        db.add(ActivityEvent(
            project_id=task.project_id, task_id=task.id, actor_type=actor_type,
            action="start_date_changed",
            details=_json.dumps({"from": old_start_date, "to": new_start}),
        ))

    # Due date change
    new_due = str(body.due_date) if body.due_date else None
    if body.due_date is not None and new_due != old_due_date:
        db.add(ActivityEvent(
            project_id=task.project_id, task_id=task.id, actor_type=actor_type,
            action="due_date_changed",
            details=_json.dumps({"from": old_due_date, "to": new_due}),
        ))

    # WHY: Stamp actor on every new ActivityEvent created during this request, plus inject
    # 'actor' into details JSON so the activity feed can attribute correctly. Also fold the
    # actor name into details for events that didn't already include it. (VF-196 follow-up)
    for obj in list(db.new):
        if isinstance(obj, ActivityEvent):
            if obj.actor_user_id is None and _patch_actor_user_id:
                obj.actor_user_id = _patch_actor_user_id
            if obj.actor_token_id is None and _patch_actor_token_id:
                obj.actor_token_id = _patch_actor_token_id
            try:
                d = _json.loads(obj.details) if obj.details else {}
                if isinstance(d, dict) and "actor" not in d:
                    d["actor"] = actor_name
                    obj.details = _json.dumps(d)
            except (ValueError, TypeError):
                pass

    db.commit()
    db.refresh(task)
    # Broadcast to SSE subscribers
    proj = db.query(Project.slug).filter(Project.id == task.project_id).first()
    if proj:
        broadcast(proj[0], "task.updated", {"task_id": task.id, "status": task.status, "old_status": old_status})
    # VF-163: broadcast each downstream task we auto-cleared so their cards
    # re-render the now-cleared pill without a manual browser refresh. Downstream
    # might live in a different project than the closing task, so resolve each
    # project's slug individually.
    try:
        if _vf163_auto_cleared:
            _slug_cache = {}
            for ds_id, ds_proj_id, ds_status in _vf163_auto_cleared:
                ds_slug = _slug_cache.get(ds_proj_id)
                if ds_slug is None:
                    row = db.query(Project.slug).filter(Project.id == ds_proj_id).first()
                    ds_slug = row[0] if row else None
                    _slug_cache[ds_proj_id] = ds_slug
                if ds_slug:
                    broadcast(ds_slug, "task.updated", {
                        "task_id": ds_id, "status": ds_status, "old_status": ds_status,
                        "cause": "blocked_by_auto_cleared",
                    })
    except NameError:
        # _vf163_auto_cleared isn't defined on code paths where body.status was None
        pass
    return _task_out(task, db)


# --- HTMX partial: task list for project view ---

@router.get("/api/v2/projects/{slug}/tasks/partial", response_class=HTMLResponse)
def tasks_partial(
    slug: str,
    status: str | None = None,
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter(Project.slug == slug).first()
    if not project:
        return HTMLResponse("<div>Project not found</div>", status_code=404)

    q = db.query(Task).filter(Task.project_id == project.id)
    if status:
        q = q.filter(Task.status == status)

    tasks = q.order_by(Task.sort_order, Task.created_at).all()

    if not tasks:
        return HTMLResponse('<div style="color:var(--color-text-muted);font-size:var(--text-sm);">No tasks yet.</div>')

    rows = []
    for t in tasks:
        colour = {
            "backlog": "var(--color-backlog)",
            "ready": "var(--color-ready)",
            "in_progress": "var(--color-in-progress)",
            "needs_review": "var(--color-needs-review)",
            "blocked": "var(--color-blocked)",
            "done": "var(--color-done)",
            "cancelled": "var(--color-cancelled)",
        }.get(t.status, "#94a3b8")

        label = t.status.replace("_", " ").title()
        rows.append(f"""
        <div style="display:flex;align-items:center;gap:var(--space-3);
                    padding:var(--space-2) 0;border-bottom:1px solid var(--color-border);">
          <span style="width:0.5rem;height:0.5rem;border-radius:50%;
                       background:{colour};flex-shrink:0;"></span>
          <span style="flex:1;font-size:var(--text-sm);color:var(--color-text);">{t.title}</span>
          <span style="font-size:var(--text-xs);color:var(--color-text-muted);white-space:nowrap;">{label}</span>
        </div>""")

    return HTMLResponse("".join(rows))


def _render_card(t: Task, milestone: str | None, db: Session) -> str:
    is_agent = t.owner_label in ("agent", "claude", "codex") or "agent" in (t.owner_label or "").lower()
    owner_cls = "owner-agent" if is_agent else "owner-human"
    owner_icon = "\U0001F916" if is_agent else "\U0001F464"
    # Resolve display name: "agent:Claude" -> "Claude", plain "agent" -> look up from project members
    _raw_owner = (t.owner_label or "").strip()
    if ":" in _raw_owner:
        owner_name = _raw_owner.split(":", 1)[1].strip()
    else:
        # Plain type with no name — resolve from project members
        if is_agent:
            # WHY: filter by active + project-scoped to avoid cross-project agent leak (VF-285)
            _agent_member = (db.query(Agent.name)
                .join(ProjectMember, ProjectMember.agent_id == Agent.id)
                .filter(ProjectMember.project_id == t.project_id,
                        Agent.status == "active",
                        Agent.project_id == t.project_id)
                .first())
            owner_name = _agent_member[0] if _agent_member else _raw_owner
        else:
            from app.models.user import User as _User
            _human_member = (db.query(_User.display_name)
                .join(ProjectMember, ProjectMember.user_id == _User.id)
                .filter(ProjectMember.project_id == t.project_id)
                .first())
            owner_name = _human_member[0] if _human_member else _raw_owner
    card_desc = t.short_description or (t.description[:120] if t.description else "")
    desc_html = f'<div class="task-desc">{card_desc}</div>' if card_desc else ""
    # Short ID
    proj = db.query(Project.prefix).filter(Project.id == t.project_id).first()
    short_id = f"{proj[0]}-{t.task_number}" if proj and proj[0] and t.task_number else ""
    # VF-320: emit a per-status class so CSS can style done + cancelled distinctly.
    done_cls = f" {t.status}" if t.status in ("done", "cancelled") else ""
    priority_cls = f" priority-{t.priority}" if t.priority in ("high", "medium", "low") else " priority-low"

    phase_chip = ''
    if t.phase_id:
        phase = db.query(Phase.name).filter(Phase.id == t.phase_id).first()
        if phase:
            phase_chip = f'<span class="task-chip task-chip-phase">{phase[0]}</span>'

    # Task type chip (feature, bug, chore, spike, verification)
    type_chip = ''
    if t.task_type:
        type_cls = f"task-chip-type-{t.task_type}" if t.task_type in ("bug", "feature", "chore", "spike", "verification") else ""
        type_chip = f'<span class="task-chip task-chip-type {type_cls}">{t.task_type}</span>'

    priority_chip_cls = f"task-chip-{t.priority}" if t.priority in ("high", "medium", "low") else "task-chip-low"
    priority_chip = f'<span class="task-chip {priority_chip_cls}"><span class="chip-dot"></span>{t.priority}</span>'

    blocked_chip = ''
    if t.blocked_by_task_id:
        blocker = db.query(Task.title).filter(Task.id == t.blocked_by_task_id).first()
        blocker_name = blocker[0][:20] + '...' if blocker and len(blocker[0]) > 20 else (blocker[0] if blocker else '?')
        blocked_chip = (
            f'<span class="task-chip task-chip-blocked" title="Blocked by: {blocker[0] if blocker else "?"}">'
            f'blocked: {blocker_name}</span>'
        )

    id_chip = f'<span class="task-chip task-chip-id">{short_id}</span>' if short_id else ''
    # v4.1: prepend DRIFT chip if this task has an active escalation AND the system gate is on
    drift_chip = ''
    _drift_on = False
    try:
        from app.api.v2.admin_experimental import get_bool as _get_bool
        _drift_on = _get_bool(db, "drift_gate_enabled", True)
    except Exception:
        _drift_on = True  # fail-open for render if settings lookup errors
    task_has_drift = False
    if _drift_on:
        from app.api.v2.drift_gate import is_task_flagged as _is_flagged
        task_has_drift = _is_flagged(t.id, db)
    if task_has_drift:
        drift_chip = '<span class="task-chip task-chip-drift" title="Drift-eval mechanism flagged possible response gaming. Click the ticket to review and clear.">\u26a0 DRIFT</span>'
    tags_html = f'<div class="task-tags">{drift_chip}{id_chip}{type_chip}{phase_chip}{priority_chip}{blocked_chip}</div>'

    date_html = ''
    if t.start_date or t.due_date:
        s = t.start_date.isoformat() if t.start_date else ''
        d = t.due_date.isoformat() if t.due_date else ''
        cal_svg = (
            '<svg width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" style="flex-shrink:0;">'
            '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/></svg>'
        )
        if s and d:
            date_html = f'<span class="task-date" data-start="{s}" data-due="{d}">{cal_svg}<span class="task-date-text">{s} \u2013 {d}</span></span>'
        else:
            val = s or d
            label = 'start' if s else 'due'
            date_html = f'<span class="task-date" data-{label}="{val}">{cal_svg}<span class="task-date-text">{val}</span></span>'

    owner_html = (
        f'<div class="task-owner {owner_cls}" title="{t.owner_label}">'
        f'<span class="task-owner-icon">{owner_icon}</span>'
        f'<span class="task-owner-label">{owner_name}</span>'
        f'</div>'
    )

    footer_html = (
        f'<div class="task-footer">'
        f'<div class="task-footer-icons">{date_html}</div>'
        f'{owner_html}'
        f'</div>'
    )
    drift_cls = " task-card--drift-flagged" if task_has_drift else ""
    return f"""
        <div class="task-card{done_cls}{priority_cls}{drift_cls}" data-task-id="{t.id}">
          {tags_html}
          <div class="task-title">{t.title}</div>
          {desc_html}
          {footer_html}
        </div>"""


# --- HTMX partial: task cards for board column ---

@router.get("/api/v2/projects/{slug}/tasks/board-column", response_class=HTMLResponse)
def board_column_partial(
    slug: str,
    status: str,
    milestone: str | None = None,
    db: Session = Depends(get_db),
):
    # VF-163: board-column responses must never be cached. HTMX refreshes
    # triggered by SSE (blocker-closed → downstream pill clear) pull the same
    # URL repeatedly; browser heuristic caching would serve stale HTML with
    # the old pill until a hard refresh. `no-store` kills that outright.
    _no_cache = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
    }
    project = db.query(Project).filter(Project.slug == slug).first()
    if not project:
        return HTMLResponse("", headers=_no_cache)

    # Fetch leaf tasks only (skip phase tasks)
    q = db.query(Task).filter(
        Task.project_id == project.id,
        Task.status == status,
        Task.owner_label != "phase",
    )
    if milestone:
        q = q.filter(Task.milestone_label == milestone)

    # v4.1: float drift-flagged cards to the top of the column via a correlated EXISTS subquery
    # on drift_escalations. No DB sort_order change — reverts naturally on clear.
    from app.models.drift import DriftEscalation
    from sqlalchemy import exists as sa_exists, case, literal
    has_drift = sa_exists().where(
        (DriftEscalation.task_id == Task.id) & (DriftEscalation.ended_at.is_(None))
    ).correlate(Task)
    drift_sort = case((has_drift, literal(0)), else_=literal(1))
    tasks = q.order_by(drift_sort, Task.sort_order, Task.created_at).all()

    if not tasks:
        return HTMLResponse('<div class="empty-col">No tasks</div>', headers=_no_cache)

    # Build phase lookup from phases table
    phase_ids = {t.phase_id for t in tasks if t.phase_id}
    phase_map = {}  # phase_id → (name, milestone_label)
    if phase_ids:
        phases = (
            db.query(Phase.id, Phase.name, Phase.sort_order, Milestone.label)
            .outerjoin(Milestone, Phase.milestone_id == Milestone.id)
            .filter(Phase.id.in_(phase_ids))
            .order_by(Milestone.sort_order.nullslast(), Phase.sort_order)
            .all()
        )
        for p in phases:
            phase_map[p.id] = (p.name, p.label)

    # Fallback: also check parent_task_id for tasks not yet migrated to phase_id
    legacy_ids = {t.parent_task_id for t in tasks if t.parent_task_id and not t.phase_id}
    if legacy_ids:
        legacy_phases = db.query(Task.id, Task.title, Task.milestone_label).filter(
            Task.id.in_(legacy_ids), Task.owner_label == "phase"
        ).all()
        for p in legacy_phases:
            name = p.title
            if "] " in name:
                name = name.split("] ", 1)[1]
            phase_map[p.id] = (name, p.milestone_label)

    # Group tasks by phase
    from collections import OrderedDict
    phased_groups = OrderedDict()
    ungrouped = []

    for t in tasks:
        pid = t.phase_id or (t.parent_task_id if t.parent_task_id in phase_map else None)
        if pid and pid in phase_map:
            phased_groups.setdefault(pid, []).append(t)
        else:
            ungrouped.append(t)

    cards = []

    # Render phased groups (with headers)
    for pid, group_tasks in phased_groups.items():
        phase_name, phase_ms = phase_map[pid]
        ms_tag = ''
        if phase_ms and not milestone:
            ms_short = phase_ms.replace('Milestone ', '')
            ms_tag = f'<span class="ms-tag">{ms_short}</span> '
        cards.append(f"""
        <div class="phase-divider" data-phase="true">
          <span class="phase-label">{ms_tag}{phase_name}</span>
        </div>""")

        for t in group_tasks:
            cards.append(_render_card(t, milestone, db))

    # Ungrouped tasks at end
    for t in ungrouped:
        cards.append(_render_card(t, milestone, db))

    return HTMLResponse("".join(cards), headers=_no_cache)


@router.get("/api/v2/projects/{slug}/phases")
@router.get("/api/v2/projects/{slug}/phases/")
def list_phases(slug: str, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.slug == slug).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    phases = (
        db.query(Phase.id, Phase.name, Phase.milestone_id, Milestone.label)
        .outerjoin(Milestone, Phase.milestone_id == Milestone.id)
        .filter(Phase.project_id == project.id, Phase.status == "active")
        .order_by(Milestone.sort_order.nullslast(), Phase.sort_order)
        .all()
    )
    return [{"id": p.id, "name": p.name, "milestone_label": p.label or "No milestone"} for p in phases]


# --- Task notes ---

class NoteCreate(BaseModel):
    body: str
    author_type: str = "human"
    author_name: str = "Admin"
    is_completion_note: bool = False
    is_internal: bool = False  # Human-only flag; agents are rejected if they attempt to set True


@router.get("/api/v2/tasks/{task_id}/notes")
def list_notes(task_id: str, request: Request = None, db: Session = Depends(get_db)):
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    actor_type = "human"
    if request:
        actor_type, _ = _resolve_actor(request, db, project_id=task.project_id)
        _check_human_project_access(request, db, task.project_id)
    q = db.query(TaskNote).filter(TaskNote.task_id == task_id)
    # GATE: Agents never see is_internal notes. Humans see them (and the is_internal
    # flag is returned so the UI can render them visually distinct).
    if actor_type == "agent":
        q = q.filter(TaskNote.is_internal == False)  # noqa: E712 — SQLAlchemy requires == for bool
    notes = q.order_by(TaskNote.created_at).all()
    base = [{"id": n.id, "body": n.body, "author_type": n.author_type, "author_name": n.author_name,
             "is_completion_note": n.is_completion_note,
             "superseded_at": n.superseded_at.isoformat() if n.superseded_at else None,
             "superseded_by": n.superseded_by, "superseded_reason": n.superseded_reason,
             "supersede_history": n.supersede_history,
             "created_at": n.created_at.isoformat()} for n in notes]
    # Humans get is_internal; agents wouldn't see any internal rows anyway (filtered above).
    if actor_type != "agent":
        for row, n in zip(base, notes):
            row["is_internal"] = n.is_internal
    return base


def _sanitize_html(html: str) -> str:
    """Whitelist-based HTML sanitizer for note bodies."""
    import re
    # Allow only safe tags
    allowed = {'p', 'br', 'strong', 'em', 'b', 'i', 'ul', 'ol', 'li', 'span'}
    # Also allow span with our mention classes
    # Strip all tags not in whitelist
    def replace_tag(match):
        tag = match.group(1).split()[0].strip('/').lower()
        if tag in allowed:
            return match.group(0)
        return ''
    result = re.sub(r'<(/?\w[^>]*)>', replace_tag, html)
    # Strip on* event handlers from remaining tags
    result = re.sub(r'\s+on\w+="[^"]*"', '', result)
    result = re.sub(r"\s+on\w+='[^']*'", '', result)
    return result


@router.post("/api/v2/tasks/{task_id}/notes", status_code=201)
def create_note(task_id: str, body: NoteCreate, request: Request = None, db: Session = Depends(get_db)):
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    proj = db.query(Project.status).filter(Project.id == task.project_id).first()
    if proj and proj[0] in ("archived", "completed"):
        raise HTTPException(status_code=422, detail=f"Project is {proj[0]}. Reopen to make changes.")
    # GATE: Enforce agent identity + scope — agents cannot impersonate humans or flag completion notes
    actor_type, actor_name = _resolve_actor(request, db, project_id=task.project_id) if request else ("human", "Human")
    if actor_type == "agent":
        # GATE: Agents MUST NOT set is_internal — that flag is human-only.
        if body.is_internal:
            raise HTTPException(status_code=422, detail="Agents cannot post internal notes. is_internal is a human-only flag.")
        body.author_type = "agent"
        body.author_name = actor_name
        body.is_completion_note = False
        body.is_internal = False  # belt and braces
    # GATE: Human caller needs write access on this project
    if request:
        _require_write(request, db, task.project_id)
    sanitized = _sanitize_html(body.body)
    note = TaskNote(task_id=task_id, body=sanitized, author_type=body.author_type,
                    author_name=body.author_name, is_completion_note=body.is_completion_note,
                    is_internal=body.is_internal)
    db.add(note)
    db.commit()
    db.refresh(note)
    return {"id": note.id, "body": note.body, "author_type": note.author_type,
            "author_name": note.author_name, "is_completion_note": note.is_completion_note,
            "is_internal": note.is_internal,
            "created_at": note.created_at.isoformat()}


class SupersedeRequest(BaseModel):
    reason: str


def _note_number(task_id: str, note_id: str, db: Session) -> int:
    """Get 1-based note number by created_at order."""
    all_notes = db.query(TaskNote.id).filter(TaskNote.task_id == task_id).order_by(TaskNote.created_at).all()
    for i, (nid,) in enumerate(all_notes, 1):
        if nid == note_id:
            return i
    return 0


@router.post("/api/v2/tasks/{task_id}/notes/{note_id}/supersede", status_code=200)
def supersede_note(task_id: str, note_id: str, body: SupersedeRequest, request: Request = None, db: Session = Depends(get_db)):
    note = db.query(TaskNote).filter(TaskNote.id == note_id, TaskNote.task_id == task_id).first()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    if note.superseded_at:
        raise HTTPException(status_code=422, detail="Note is already superseded")
    if not body.reason.strip():
        raise HTTPException(status_code=422, detail="Reason is required when superseding a note")
    # Block supersede on done tasks — reopen first
    task = db.query(Task).filter(Task.id == task_id).first()
    if task and task.status == "done":
        raise HTTPException(status_code=422, detail="Cannot supersede notes on a closed task. Reopen the task first.")
    # GATE: Agent scope enforcement
    actor_type, actor_name = _resolve_actor(request, db, project_id=task.project_id if task else None) if request else ("human", "Human")
    # Agent can only supersede own notes
    if actor_type == "agent" and note.author_type != "agent":
        raise HTTPException(status_code=422, detail="Agents can only supersede their own notes.")
    # GATE: Agents cannot touch internal notes at all (even reading, by existing filter)
    if actor_type == "agent" and note.is_internal:
        raise HTTPException(status_code=404, detail="Note not found")
    # GATE: Human caller needs write access on this project
    if request and task:
        _require_write(request, db, task.project_id)
    from datetime import datetime, timezone
    import json as _json
    now = datetime.now(timezone.utc)
    note_num = _note_number(task_id, note_id, db)
    note.superseded_at = now
    note.superseded_by = actor_name
    note.superseded_reason = body.reason.strip()
    # Append to history
    history = note.supersede_history or []
    history.append({"action": "superseded", "by": actor_name, "reason": body.reason.strip(), "at": now.isoformat()})
    note.supersede_history = history
    db.add(ActivityEvent(
        project_id=task.project_id, task_id=task_id, actor_type=actor_type, action="note_superseded",
        details=_json.dumps({"note_num": note_num, "note_id": note_id,
                             "note_author": note.author_name, "reason": body.reason.strip(),
                             "superseded_by": actor_name, "body_preview": note.body[:200]}),
    ))
    db.commit()
    db.refresh(note)
    return {"id": note.id, "superseded_at": note.superseded_at.isoformat(),
            "superseded_by": note.superseded_by, "superseded_reason": note.superseded_reason,
            "supersede_history": note.supersede_history}


@router.post("/api/v2/tasks/{task_id}/notes/{note_id}/revert", status_code=200)
def revert_supersede(task_id: str, note_id: str, body: SupersedeRequest, request: Request = None, db: Session = Depends(get_db)):
    note = db.query(TaskNote).filter(TaskNote.id == note_id, TaskNote.task_id == task_id).first()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    if not note.superseded_at:
        raise HTTPException(status_code=422, detail="Note is not superseded")
    if not body.reason.strip():
        raise HTTPException(status_code=422, detail="Reason is required when reverting a supersede")
    task = db.query(Task).filter(Task.id == task_id).first()
    if task and task.status == "done":
        raise HTTPException(status_code=422, detail="Cannot revert notes on a closed task. Reopen the task first.")
    # GATE: Agent scope enforcement
    actor_type, actor_name = _resolve_actor(request, db, project_id=task.project_id if task else None) if request else ("human", "Human")
    # Agent can only revert supersede on own notes
    if actor_type == "agent" and note.author_type != "agent":
        raise HTTPException(status_code=422, detail="Agents can only revert supersede on their own notes.")
    # GATE: Agents cannot touch internal notes at all
    if actor_type == "agent" and note.is_internal:
        raise HTTPException(status_code=404, detail="Note not found")
    # GATE: Human caller needs write access on this project
    if request and task:
        _require_write(request, db, task.project_id)
    from datetime import datetime, timezone
    import json as _json
    now = datetime.now(timezone.utc)
    note_num = _note_number(task_id, note_id, db)
    # Append revert to history before clearing state
    history = note.supersede_history or []
    history.append({"action": "reverted", "by": actor_name, "reason": body.reason.strip(), "at": now.isoformat()})
    note.supersede_history = history
    # Clear superseded state
    note.superseded_at = None
    note.superseded_by = None
    note.superseded_reason = None
    # Strip completion flag on revert — content restored but closure authority requires fresh note
    was_completion = note.is_completion_note
    if note.is_completion_note:
        note.is_completion_note = False
    db.add(ActivityEvent(
        project_id=task.project_id, task_id=task_id, actor_type=actor_type, action="note_revert_supersede",
        details=_json.dumps({"note_num": note_num, "note_id": note_id,
                             "note_author": note.author_name, "reason": body.reason.strip(),
                             "reverted_by": actor_name, "body_preview": note.body[:200],
                             "completion_cleared": was_completion}),
    ))
    db.commit()
    db.refresh(note)
    return {"id": note.id, "superseded_at": None, "supersede_history": note.supersede_history}


# --- Drift v4: clear-drift handler ---

@router.post("/api/v2/tasks/{task_id}/clear-drift", status_code=200)
def clear_drift_flag(task_id: str, request: Request = None, db: Session = Depends(get_db)):
    """Clear the drift escalation on a task, unfreeze the agent, and post
    a visible re-alignment note.

    v4.1: state lives in drift_escalations. Clearing = UPDATE ended_at on the
    active row. Human-only endpoint. Human must have write access on the project.
    """
    from datetime import datetime, timezone
    from app.api.v2.drift_gate import build_re_alignment_note
    from app.models.drift import DriftEscalation

    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    actor_type, actor_name = _resolve_actor(request, db, project_id=task.project_id) if request else ("human", "Admin")
    if actor_type != "human":
        raise HTTPException(status_code=403, detail="Only humans can clear drift flags.")
    if request:
        _require_write(request, db, task.project_id)

    # Find the active escalation on this task
    escalation = db.query(DriftEscalation).filter(
        DriftEscalation.task_id == task_id,
        DriftEscalation.ended_at.is_(None),
    ).first()
    if escalation is None:
        raise HTTPException(status_code=422, detail="Task has no active drift flag.")

    now = datetime.now(timezone.utc)
    agent = db.query(Agent).filter(Agent.id == escalation.agent_id).first()
    frozen_agent_name = agent.name if agent else "unknown"

    # Resolve human user id for cleared_by attribution
    user_id = None
    if request:
        from app.models.session import UserSession
        session_id = request.cookies.get("vf_session")
        if session_id:
            sess = db.query(UserSession).filter(
                UserSession.id == session_id,
                UserSession.session_type == "user",
                UserSession.expires_at > now,
            ).first()
            if sess:
                user_id = sess.user_id

    # End the escalation (source-of-truth for the freeze)
    escalation.ended_at = now
    escalation.cleared_by = user_id
    escalation.cleared_reason = f"cleared by {actor_name}"

    # Reset the agent's eval cycle memory — fresh slate post re-alignment.
    if agent is not None:
        agent.drift_eval_hashes = []
        agent.drift_eval_count = 0
        agent.drift_eval_passed_at = None

    audit_note = TaskNote(
        task_id=task_id,
        body=(
            f"Drift flag cleared by {actor_name} at {now.isoformat()}. "
            f"Previously frozen agent: {frozen_agent_name}. "
            f"Escalation ID: {escalation.id}. "
            f"Re-alignment note auto-posted as the next visible note."
        ),
        author_type="system",
        author_name="system",
        is_internal=True,
        is_completion_note=False,
    )
    db.add(audit_note)

    project = db.query(Project).filter(Project.id == task.project_id).first()
    project_slug = project.slug if project else "your-project"
    realign_body = build_re_alignment_note(actor_name, project_slug)
    realign_note = TaskNote(
        task_id=task_id,
        body=realign_body,
        author_type="system",
        author_name="system",
        is_internal=False,
        is_completion_note=False,
    )
    db.add(realign_note)

    db.add(ActivityEvent(
        project_id=task.project_id, task_id=task_id, actor_type="human",
        action="drift_flag_cleared",
        details=json.dumps({
            "cleared_by": actor_name,
            "agent": frozen_agent_name,
            "escalation_id": escalation.id,
            "at": now.isoformat(),
        }),
    ))

    db.commit()

    return {
        "id": task.id,
        "has_active_drift_flag": False,
        "escalation_id": escalation.id,
        "cleared_by": actor_name,
        "cleared_at": now.isoformat(),
        "realignment_note_id": realign_note.id,
    }


# --- Task audit log (from activity_events) ---

@router.get("/api/v2/tasks/{task_id}/audit")
def task_audit(task_id: str, db: Session = Depends(get_db)):
    events = (db.query(ActivityEvent)
              .filter(ActivityEvent.task_id == task_id)
              .order_by(ActivityEvent.created_at.desc())
              .limit(50)
              .all())
    return [{"id": e.id, "action": e.action, "actor_type": e.actor_type,
             "details": e.details, "created_at": e.created_at.isoformat()} for e in events]


# ── Project Artefacts (per PROJECT-SCAFFOLD-PROPOSAL.md §4) ──

@router.get("/api/v2/projects/{slug}/artefacts/{name}")
def get_artefact(slug: str, name: str, request: Request, db: Session = Depends(get_db)):
    """Wave 2.0.8 R3 (VF-367): KISS read-only artefact-fetch over already-persisted
    onboard_state content. Replaces the older ProjectArtefact-table-backed
    endpoint that returned contradictory copy ("available types X, Y, Z" then
    404'd on those — see Codex blind cross-vendor onboard findings).

    Type-routed (the {name} path param is the artefact type for backward compat
    with the older endpoint shape):
      plan      → onboard_state.plan_hash + (optional) plan_content
      agent_md  → onboard_state.agent_md_hash + (optional) agent_md_content
      contract  → 308 redirect to /agentnotes/{slug} (canonical contract source)
      handover  → 404 with FS pointer (not server-captured; see proposal VF-372)

    Anything else returns 404 with explicit supported_types + agent_remedy.
    Falls back to the legacy ProjectArtefact lookup ONLY if a populated row
    exists for the given name (preserves any in-flight workflow that wrote
    rows directly to that table — none in test scope, but keeps the door open).

    Auth: same as other project-scoped reads. Cross-project token returns 403.
    """
    project = db.query(Project).filter(Project.slug == slug).first()
    if not project:
        raise HTTPException(status_code=404, detail={
            "code": "PROJECT_NOT_FOUND",
            "message": f"No project with slug '{slug}'.",
        })
    _resolve_actor(request, db, project_id=project.id)

    artefact_type = name  # path param kept as `name` for backward compat; semantically a type
    supported_types = ("plan", "agent_md", "contract", "handover")

    if artefact_type not in supported_types:
        # Legacy fallback: if a ProjectArtefact row exists for this name, return it.
        # Lets any pre-existing flow that wrote directly to the table keep working.
        from app.models.artefact import ProjectArtefact
        legacy = (db.query(ProjectArtefact)
            .filter(ProjectArtefact.project_id == project.id, ProjectArtefact.name == name)
            .order_by(ProjectArtefact.version.desc())
            .first())
        if legacy:
            return {
                "name": legacy.name,
                "version": legacy.version,
                "body": legacy.body,
                "byte_count": legacy.byte_count,
                "content_hash": legacy.content_hash,
                "actor_type": legacy.actor_type,
                "actor_name": legacy.actor_name,
                "created_at": legacy.created_at.isoformat() if legacy.created_at else None,
                "_legacy_storage": True,
            }
        raise HTTPException(status_code=404, detail={
            "code": "ARTEFACT_TYPE_UNKNOWN",
            "message": f"No artefact type '{name}'.",
            "supported_types": list(supported_types),
            "agent_remedy": (
                "Use one of: plan, agent_md, contract, handover. "
                "plan + agent_md return content + hash from onboard_state. "
                "contract is a 308 redirect to /agentnotes/{slug} (canonical contract source). "
                "handover is not server-captured (returns 404 with filesystem pointer)."
            ),
        })

    # Contract → redirect to canonical source
    if artefact_type == "contract":
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=f"/agentnotes/{slug}", status_code=308)

    state = project.onboard_state or {}

    if artefact_type == "plan":
        plan_hash = state.get("plan_hash")
        plan_content = state.get("plan_content")
        if not plan_hash:
            raise HTTPException(status_code=404, detail={
                "code": "ARTEFACT_NOT_REGISTERED",
                "message": "plan_hash not registered yet (onboard substep 5 not complete).",
                "agent_remedy": "Complete onboard substep 5 (plan_hash) first via POST /onboard-state/ack.",
            })
        return {
            "type": "plan",
            "project_slug": slug,
            "hash": plan_hash,
            "content": plan_content,
            "content_captured": plan_content is not None,
            "filesystem_path_hint": "0-MD/0-Documentation/internal/initial_plan.md",
            "agent_note": (
                "If content is null, the agent that registered plan_hash didn't include plan_content "
                "on the /ack call. Hash is still authoritative for drift detection. Filesystem path "
                "is a hint — read locally if you have FS access; otherwise re-register with content."
            ),
        }

    if artefact_type == "agent_md":
        agent_md_hash = state.get("agent_md_hash")
        agent_md_content = state.get("agent_md_content")
        if not agent_md_hash:
            raise HTTPException(status_code=404, detail={
                "code": "ARTEFACT_NOT_REGISTERED",
                "message": "agent_md_hash not registered yet (onboard substep 6 not complete).",
                "agent_remedy": "Complete onboard substep 6 (agent_md_hash) first via POST /onboard-state/complete.",
            })
        return {
            "type": "agent_md",
            "project_slug": slug,
            "hash": agent_md_hash,
            "content": agent_md_content,
            "content_captured": agent_md_content is not None,
            "filesystem_path_hint": "AGENTS.md (Codex/Cursor/generic vendors) or CLAUDE.md (Claude vendors)",
            "agent_note": (
                "Content captured server-side at substep 6 registration time (up to 64KB). "
                "Hash is authoritative for drift detection. If content is null, agent registered "
                "without including agent_md_content; filesystem path is the customer-side fallback."
            ),
        }

    # Handover: not server-captured (backlog VF-372 proposal)
    if artefact_type == "handover":
        raise HTTPException(status_code=404, detail={
            "code": "ARTEFACT_NOT_SERVER_CAPTURED",
            "message": "handover artefacts are not server-stored in this contract version.",
            "filesystem_path_hint": "0-MD/progress/SESSION-HANDOFF-{date}-{summary}.md",
            "agent_remedy": (
                "Read handover docs from filesystem at 0-MD/progress/. The most recent "
                "SESSION-HANDOFF-*.md is typically the active cross-session bridge. "
                "Server-side handover capture is queued as proposal VF-372 (backlog low) — "
                "see 0-MD/proposed/2026-05-04-server-side-artefact-lifecycle.md for the design options."
            ),
        })


def _task_out(t: Task, db: Session = None) -> TaskOut:
    # Compute ITIL IDs
    prefix = None
    proj_num = None
    if db and t.project_id:
        proj = db.query(Project.prefix, Project.project_number).filter(Project.id == t.project_id).first()
        if proj:
            prefix = proj[0]
            proj_num = proj[1]
    short_id = f"{prefix}-{t.task_number}" if prefix and t.task_number else None
    full_id = f"PRJ{proj_num:05d}-TSK{t.task_number:05d}" if proj_num and t.task_number else None

    # Resolve phase label
    phase_label = None
    if db and t.phase_id:
        phase = db.query(Phase.name).filter(Phase.id == t.phase_id).first()
        if phase:
            phase_label = phase[0]

    return TaskOut(
        id=t.id,
        project_id=t.project_id,
        task_number=t.task_number,
        short_id=short_id,
        full_id=full_id,
        title=t.title,
        short_description=t.short_description or "",
        description=t.description,
        status=t.status,
        priority=t.priority,
        owner_label=t.owner_label,
        sort_order=t.sort_order,
        external_number=t.external_number,
        parent_task_id=t.parent_task_id,
        milestone_label=t.milestone_label,
        start_date=t.start_date,
        due_date=t.due_date,
        phase_id=t.phase_id,
        phase_label=phase_label,
        task_type=t.task_type,
        blocked_by_task_id=t.blocked_by_task_id,
        abandoned_note=t.abandoned_note,
        has_active_drift_flag=_is_task_flagged(t.id, db) if db else False,
        created_at=t.created_at,
        updated_at=t.updated_at,
    )


def _is_task_flagged(task_id: str, db) -> bool:
    """v4.1 — derive has_active_drift_flag from drift_escalations EXISTS-query."""
    from app.api.v2.drift_gate import is_task_flagged
    return is_task_flagged(task_id, db)


# Wave 2.0.8 R3 (VF-367) artefact endpoint lives at the existing
# /api/v2/projects/{slug}/artefacts/{name} route earlier in this file
# (search "Wave 2.0.8 R3" in get_artefact). The legacy ProjectArtefact
# fallback is preserved for any pre-existing flow; the new KISS read
# path takes precedence for the four known types.
