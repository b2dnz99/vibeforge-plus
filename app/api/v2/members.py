"""
Project members + agents + @mention trigger API.
Scaffold for future RBAC — roles shown but not enforced.
"""
import hashlib
import json
import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel, ConfigDict

from app.db.session import get_db
from app.models.user import User
from app.models.agent import Agent
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.activity import ActivityEvent

router = APIRouter()


# --- Agents ---

class AgentCreate(BaseModel):
    name: str
    slug: str
    description: str = ""


@router.get("/api/v2/agents")
def list_agents(request: Request = None, db: Session = Depends(get_db)):
    # GATE: Agent scope — only see agents on your project
    import hashlib
    agent_project_id = None
    if request:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:].strip()
            if token:
                token_hash = hashlib.sha256(token.encode()).hexdigest()
                caller = db.query(Agent).filter(Agent.api_token_hash == token_hash, Agent.status == "active").first()
                if caller and caller.project_id:
                    agent_project_id = caller.project_id
    q = db.query(Agent).filter(Agent.status == "active")
    if agent_project_id:
        # WHY: Agent only sees agents on its own project — no cross-project leaks
        q = q.filter(Agent.project_id == agent_project_id)
    agents = q.order_by(Agent.name).all()
    return [
        {"id": a.id, "name": a.name, "slug": a.slug, "type": "agent",
         "description": a.description, "status": a.status}
        for a in agents
    ]


@router.post("/api/v2/agents", status_code=201)
def create_agent(body: AgentCreate, db: Session = Depends(get_db)):
    existing = db.query(Agent).filter(Agent.slug == body.slug).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Agent '{body.slug}' already exists")
    agent = Agent(
        id=str(uuid.uuid4()), name=body.name, slug=body.slug,
        description=body.description, status="active",
    )
    db.add(agent)
    db.commit()
    return {"id": agent.id, "name": agent.name, "slug": agent.slug, "type": "agent"}


# --- Agent token management ---

@router.post("/api/v2/agents/{agent_id}/token")
def issue_agent_token(agent_id: str, db: Session = Depends(get_db)):
    """Issue a new API token for an agent. Token stays visible."""
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Generate token: vf_ + 32 random hex chars
    raw_token = "vf_" + secrets.token_hex(16)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    prefix = raw_token[:8]

    agent.api_token_plain = raw_token
    agent.api_token_hash = token_hash
    agent.token_prefix = prefix
    db.commit()

    return {
        "agent_id": agent.id,
        "agent_name": agent.name,
        "token": raw_token,
        "prefix": prefix,
        "message": "Token issued. Store it securely.",
    }


@router.delete("/api/v2/agents/{agent_id}/token")
def revoke_agent_token(agent_id: str, db: Session = Depends(get_db)):
    """Revoke an agent's API token."""
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    agent.api_token_plain = None
    agent.api_token_hash = None
    agent.token_prefix = None
    db.commit()

    return {"agent_id": agent.id, "message": "Token revoked."}


@router.get("/api/v2/agents/{agent_id}/token")
def get_agent_token(agent_id: str, db: Session = Depends(get_db)):
    """Get agent token info (visible for now)."""
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    if not agent.api_token_plain:
        return {"agent_id": agent.id, "has_token": False, "token": None, "prefix": None}

    return {
        "agent_id": agent.id,
        "has_token": True,
        "token": agent.api_token_plain,
        "prefix": agent.token_prefix,
    }


# --- Users list (for @mention autocomplete) ---

@router.get("/api/v2/users")
def list_users(request: Request = None, db: Session = Depends(get_db)):
    # GATE: Global user enumeration leaks email addresses — restrict to SU/SA cookie users (VF-193)
    from app.models.session import UserSession
    from datetime import datetime, timezone
    allowed = False
    if request:
        session_id = request.cookies.get("vf_session")
        if session_id:
            sess = db.query(UserSession).filter(
                UserSession.id == session_id,
                UserSession.session_type == "user",
                UserSession.expires_at > datetime.now(timezone.utc),
            ).first()
            if sess:
                u = db.query(User).filter(User.id == sess.user_id, User.status == "active").first()
                if u and u.role == "super_user":
                    allowed = True
    if not allowed:
        raise HTTPException(status_code=403, detail="Super user role required.")
    users = db.query(User).filter(User.status == "active").order_by(User.display_name).all()
    return [
        {"id": u.id, "name": u.display_name, "email": u.email, "type": "human"}
        for u in users
    ]


# --- Project members ---

@router.get("/api/v2/projects/{slug}/members")
def list_members(slug: str, request: Request = None, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.slug == slug).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    # GATE: Cross-project leak fix — agents scoped, humans must be members (VF-193)
    if request:
        from app.api.v2.projects import _resolve_actor, _check_human_project_access
        _resolve_actor(request, db, project_id=project.id)
        _check_human_project_access(request, db, project.id)

    # GATE: Detect agent caller — agents do not need email addresses (Codex feedback VF-187)
    is_agent_caller = False
    if request:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:].strip()
            if token:
                token_hash = hashlib.sha256(token.encode()).hexdigest()
                caller = db.query(Agent).filter(Agent.api_token_hash == token_hash, Agent.status == "active").first()
                if caller:
                    is_agent_caller = True

    members = db.query(ProjectMember).filter(ProjectMember.project_id == project.id).all()
    result = []
    for m in members:
        if m.user_id:
            user = db.query(User).filter(User.id == m.user_id).first()
            if user:
                entry = {
                    "id": m.id, "type": "human", "name": user.display_name,
                    "role": m.role, "member_id": user.id,
                }
                if not is_agent_caller:
                    entry["email"] = user.email
                result.append(entry)
        elif m.agent_id:
            agent = db.query(Agent).filter(Agent.id == m.agent_id).first()
            # GATE: only show agents scoped to THIS project (VF-273 — cross-project leak)
            if agent and (not agent.project_id or agent.project_id == project.id):
                result.append({
                    "id": m.id, "type": "agent", "name": agent.name,
                    "slug": agent.slug, "role": m.role, "member_id": agent.id,
                })

    # VF-296 fix: also include agents scoped to this project via agents.project_id
    # that don't have a project_members row. agents.project_id is the source-of-truth
    # for agent-to-project linkage; project_members.agent_id is a role-override layer
    # on top. Without this, agents disappear from the Assign UI whenever project_members
    # is wiped (admin reset, baseline cleanup, etc) — which is a legitimate admin action.
    # Round 6 hit this on dev during pc-parts-demo-store baseline reset.
    agent_ids_already_in_result = {e["member_id"] for e in result if e["type"] == "agent"}
    scoped_agents = db.query(Agent).filter(
        Agent.project_id == project.id,
        Agent.status == "active",
    ).all()
    for a in scoped_agents:
        if a.id in agent_ids_already_in_result:
            continue  # already surfaced via project_members (may carry an explicit role)
        result.append({
            "id": None,             # no project_members row exists
            "type": "agent",
            "name": a.name,
            "slug": a.slug,
            "role": "write",        # default scope-based role; explicit role needs project_members row
            "member_id": a.id,
        })
    return result


class MemberAdd(BaseModel):
    user_id: str | None = None
    agent_id: str | None = None
    role: str = "write"


@router.post("/api/v2/projects/{slug}/members", status_code=201)
def add_member(slug: str, body: MemberAdd, request: Request = None, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.slug == slug).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not body.user_id and not body.agent_id:
        raise HTTPException(status_code=422, detail="user_id or agent_id required")

    # GATE: project admin, owner, SU, or SA (VF-286)
    caller = None
    if request:
        from app.api.v2.projects import _require_admin, _cookie_user
        _require_admin(request, db, project.id)
        caller = _cookie_user(request, db)

    # Idempotent guard: reject duplicate member rows for same (project, user|agent)
    if body.user_id:
        dupe = db.query(ProjectMember).filter(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == body.user_id,
        ).first()
    else:
        dupe = db.query(ProjectMember).filter(
            ProjectMember.project_id == project.id,
            ProjectMember.agent_id == body.agent_id,
        ).first()
    if dupe:
        raise HTTPException(status_code=409,
            detail=f"Already a member of this project (role={dupe.role}).")

    # VF-328: Viewer global role cannot hold write/admin memberships.
    # Runtime _require_write already downgrades silently (projects.py:80), but the stored
    # row diverges from enforced behaviour; reject here so the admin sees the real answer.
    if body.user_id and body.role != "read":
        target_user = db.query(User).filter(User.id == body.user_id).first()
        if target_user and target_user.role == "viewer":
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{target_user.display_name} is a Viewer (read-only role). "
                    f"Only 'read' memberships are allowed. "
                    f"Change their global role to User or Super User first to grant write/admin."
                ),
            )

    member = ProjectMember(
        id=str(uuid.uuid4()), project_id=project.id,
        user_id=body.user_id, agent_id=body.agent_id,
        role=body.role,
    )
    db.add(member)

    # Audit: dual preservation (FK + snapshot) per v3
    if body.user_id:
        u = db.query(User).filter(User.id == body.user_id).first()
        target_name = u.display_name if u else body.user_id
        target_type = "user"
    else:
        a = db.query(Agent).filter(Agent.id == body.agent_id).first()
        target_name = a.name if a else body.agent_id
        target_type = "agent"
    actor_name = caller.display_name if caller else "System"
    db.add(ActivityEvent(
        id=str(uuid.uuid4()),
        project_id=project.id, task_id=None,
        actor_type="human" if caller else "system",
        actor_user_id=caller.id if caller else None,
        action="project_member_added",
        details=json.dumps({
            "project_slug": project.slug,
            "member_id": member.id,
            "target": target_name,
            "target_type": target_type,
            "role": body.role,
            "actor": actor_name,
        }),
    ))
    db.commit()
    return {"id": member.id, "role": member.role, "target_type": target_type}


class MemberRoleUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')  # VF-357: reject undocumented PATCH fields
    role: str


@router.patch("/api/v2/projects/{slug}/members/{member_id}")
def update_member_role(
    slug: str, member_id: str, body: MemberRoleUpdate,
    request: Request = None, db: Session = Depends(get_db),
):
    """Change a member's role on a project. v3 — agent role changes rejected (manage from agent detail)."""
    project = db.query(Project).filter(Project.slug == slug).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    caller = None
    if request:
        from app.api.v2.projects import _require_admin, _cookie_user
        _require_admin(request, db, project.id)
        caller = _cookie_user(request, db)

    if body.role not in ("read", "write", "admin"):
        raise HTTPException(status_code=422, detail="role must be one of: read, write, admin")

    member = db.query(ProjectMember).filter(
        ProjectMember.id == member_id,
        ProjectMember.project_id == project.id,
    ).first()
    if not member:
        raise HTTPException(status_code=404, detail="Member not found on this project")

    if member.agent_id:
        raise HTTPException(
            status_code=422,
            detail="Agent role changes are not supported under v3. "
                   "Manage agents from the agent detail page.",
        )

    old_role = member.role
    if old_role == body.role:
        return {"id": member.id, "role": member.role, "changed": False}

    # VF-328: Viewer global role cannot hold write/admin memberships.
    if body.role != "read" and member.user_id:
        target_user = db.query(User).filter(User.id == member.user_id).first()
        if target_user and target_user.role == "viewer":
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{target_user.display_name} is a Viewer (read-only role). "
                    f"Only 'read' memberships are allowed. "
                    f"Change their global role to User or Super User first to grant write/admin."
                ),
            )

    member.role = body.role

    u = db.query(User).filter(User.id == member.user_id).first() if member.user_id else None
    target_name = u.display_name if u else member.user_id
    actor_name = caller.display_name if caller else "System"
    db.add(ActivityEvent(
        id=str(uuid.uuid4()),
        project_id=project.id, task_id=None,
        actor_type="human" if caller else "system",
        actor_user_id=caller.id if caller else None,
        action="project_member_role_changed",
        details=json.dumps({
            "project_slug": project.slug,
            "member_id": member.id,
            "target": target_name,
            "target_type": "user",
            "from": old_role,
            "to": body.role,
            "actor": actor_name,
        }),
    ))
    db.commit()
    return {"id": member.id, "role": member.role, "changed": True, "from": old_role}


@router.delete("/api/v2/projects/{slug}/members/{member_id}")
def remove_member(
    slug: str, member_id: str,
    request: Request = None, db: Session = Depends(get_db),
):
    """Remove a member. For user members: cascade-revoke their agents on this project (v3)."""
    project = db.query(Project).filter(Project.slug == slug).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    caller = None
    if request:
        from app.api.v2.projects import _require_admin, _cookie_user
        _require_admin(request, db, project.id)
        caller = _cookie_user(request, db)

    member = db.query(ProjectMember).filter(
        ProjectMember.id == member_id,
        ProjectMember.project_id == project.id,
    ).first()
    if not member:
        raise HTTPException(status_code=404, detail="Member not found on this project")

    actor_name = caller.display_name if caller else "System"
    caller_id = caller.id if caller else None
    agents_revoked: list[dict] = []

    if member.user_id:
        # v3 cascade: revoke this user's active agents on this project
        u = db.query(User).filter(User.id == member.user_id).first()
        target_name = u.display_name if u else member.user_id

        # Prevent removing the project owner — every project MUST have an owner (VF-286 invariant).
        # There is no ownership-transfer flow today, so the only safe answer is "refuse."
        if project.owner_id == member.user_id:
            raise HTTPException(
                status_code=422,
                detail="Cannot remove the project owner — every project must have an owner. "
                       "No ownership-transfer flow exists yet; archive the project if no one "
                       "should be running it.",
            )

        agents = db.query(Agent).filter(
            Agent.created_by == member.user_id,
            Agent.project_id == project.id,
            Agent.status == "active",
        ).all()
        now = datetime.now(timezone.utc)
        for ag in agents:
            ag.status = "revoked"
            ag.revoked_at = now
            ag.revoked_by = caller_id
            ag.api_token_hash = None
            ag.token_prefix = None
            agents_revoked.append({"id": ag.id, "name": ag.name, "slug": ag.slug})
            db.add(ActivityEvent(
                id=str(uuid.uuid4()),
                project_id=project.id, task_id=None,
                actor_type="human" if caller else "system",
                actor_user_id=caller_id,
                action="agent_revoked",
                details=json.dumps({
                    "agent_name": ag.name,
                    "agent_slug": ag.slug,
                    "actor": actor_name,
                    "reason": "cascade_user_removed_from_project",
                    "removed_user_id": member.user_id,
                }),
            ))

        old_role = member.role
        db.delete(member)
        db.add(ActivityEvent(
            id=str(uuid.uuid4()),
            project_id=project.id, task_id=None,
            actor_type="human" if caller else "system",
            actor_user_id=caller_id,
            action="project_member_removed",
            details=json.dumps({
                "project_slug": project.slug,
                "member_id": member_id,
                "target": target_name,
                "target_type": "user",
                "old_role": old_role,
                "actor": actor_name,
                "agents_revoked_count": len(agents_revoked),
            }),
        ))
    else:
        # Agent role-override row — just delete it. Agent remains scoped via agent.project_id (VF-296).
        a = db.query(Agent).filter(Agent.id == member.agent_id).first() if member.agent_id else None
        target_name = a.name if a else member.agent_id
        old_role = member.role
        db.delete(member)
        db.add(ActivityEvent(
            id=str(uuid.uuid4()),
            project_id=project.id, task_id=None,
            actor_type="human" if caller else "system",
            actor_user_id=caller_id,
            action="project_member_removed",
            details=json.dumps({
                "project_slug": project.slug,
                "member_id": member_id,
                "target": target_name,
                "target_type": "agent",
                "old_role": old_role,
                "actor": actor_name,
                "note": "role-override removed; agent still project-scoped via agent.project_id",
            }),
        ))

    db.commit()
    return {"ok": True, "removed_member_id": member_id, "agents_revoked": agents_revoked}


@router.get("/api/v2/projects/{slug}/addable-users")
def addable_users(slug: str, request: Request = None, db: Session = Depends(get_db)):
    """Users who are NOT yet members of this project. Powers the member-picker UI (VF-286)."""
    project = db.query(Project).filter(Project.slug == slug).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if request:
        from app.api.v2.projects import _require_admin
        _require_admin(request, db, project.id)

    existing_user_ids = {
        m.user_id for m in db.query(ProjectMember).filter(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id.isnot(None),
        ).all()
    }
    if project.owner_id:
        existing_user_ids.add(project.owner_id)

    # VF-328: Viewers are addable as read-only members. UI defaults role=read and
    # disables write/admin when a Viewer is selected (see _member_picker.html).
    users = (
        db.query(User)
        .filter(User.status == "active",
                User.role.in_(("user", "super_user", "viewer")))
        .order_by(User.display_name)
        .all()
    )
    result = []
    for u in users:
        if u.id in existing_user_ids:
            continue
        result.append({
            "id": u.id,
            "username": u.username,
            "display_name": u.display_name,
            "email": u.email,
            "role": u.role,
        })
    return result


# --- @mention autocomplete (all project members) ---

@router.get("/api/v2/projects/{slug}/mentionables")
def list_mentionables(slug: str, request: Request = None, db: Session = Depends(get_db)):
    """Returns all users + agents who are members of this project, for @mention dropdown."""
    project = db.query(Project).filter(Project.slug == slug).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    # GATE: Cross-project leak fix (VF-193)
    if request:
        from app.api.v2.projects import _resolve_actor, _check_human_project_access
        _resolve_actor(request, db, project_id=project.id)
        _check_human_project_access(request, db, project.id)

    members = db.query(ProjectMember).filter(ProjectMember.project_id == project.id).all()
    result = []
    for m in members:
        if m.user_id:
            user = db.query(User).filter(User.id == m.user_id).first()
            if user:
                slug_name = user.email.split('@')[0] if user.email else ''
                result.append({"name": user.display_name, "type": "human", "id": user.id, "slug": slug_name})
        elif m.agent_id:
            agent = db.query(Agent).filter(Agent.id == m.agent_id).first()
            if agent:
                result.append({"name": agent.name, "type": "agent", "id": agent.id, "slug": agent.slug})
    return result


# --- Null trigger for @mentions ---

class MentionTrigger(BaseModel):
    task_id: str
    note_id: str | None = None
    mentioned_name: str
    mentioned_type: str  # human or agent
    mentioned_id: str


@router.post("/api/v2/triggers/mention")
def trigger_mention(body: MentionTrigger):
    """Null trigger — accepts mention events, does nothing. Future: notifications, webhooks."""
    return {"status": "accepted", "action": "none", "message": "Mention trigger placeholder — no action taken."}
