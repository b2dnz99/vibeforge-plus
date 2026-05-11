"""Super-admin experimental page — drift gate analytics + toggle.

See 0-MD/proposed/SYNC-ARCH-EXPERIMENT.md §4.

Routes:
- GET  /ui/admin/experimental/drift           HTML page
- GET  /admin/api/experimental/drift/summary        KPI strip
- GET  /admin/api/experimental/drift/timeline       30-day bar chart
- GET  /admin/api/experimental/drift/recent         recent escalations table
- GET  /admin/api/experimental/drift/by-agent       per-agent rollup
- GET  /admin/api/experimental/drift/escalation/{id} drilldown timeline
- POST /admin/api/experimental/drift/toggle         system-wide on/off
- POST /admin/api/experimental/drift/reset-all      violent clear

All routes require SA elevation (vf_sa_session cookie). Not board-reachable.
"""
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.system_settings import SystemSetting
from app.models.drift import DriftEscalation, DriftEvalAttempt
from app.models.agent import Agent
from app.models.project import Project
from app.models.task import Task


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# ─── settings helpers ───

def get_setting(db: Session, key: str, default: str = "") -> str:
    row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    return row.value if row else default


def get_bool(db: Session, key: str, default: bool = False) -> bool:
    return get_setting(db, key, "false" if not default else "true").lower() == "true"


def get_int_setting(db: Session, key: str, default: int, lower: int = None, upper: int = None) -> int:
    """VF-341: typed int reader for SystemSetting with optional bound-clamping.

    Used by session-policy + token-TTL knobs. Bound clamping is defensive
    against manual SQL writes; the policy form always validates against the
    proposal §3 lower/upper before writing.
    """
    raw = get_setting(db, key, "")
    try:
        n = int(raw) if raw else default
    except (ValueError, TypeError):
        return default
    if lower is not None and n < lower:
        return lower
    if upper is not None and n > upper:
        return upper
    return n


def get_drift_window_seconds(db: Session) -> int:
    """VF-306: drift-eval window is operator-tunable via SystemSetting.

    Reads `drift_eval_window_seconds` from system_settings; falls back to
    DRIFT_REFRESH_INTERVAL_DEFAULT (3600 = 1h) if unset or unparseable.
    Bounded to {1200, 1800, 3600, 7200} seconds (20 / 30 / 60 / 120 min)
    by the slider UI; out-of-range values from manual SQL are clamped.
    """
    raw = get_setting(db, "drift_eval_window_seconds", "")
    try:
        n = int(raw) if raw else 3600
    except ValueError:
        return 3600
    if n < 60:    # clamp lower (defensive — slider min is 1200)
        return 60
    if n > 86400: # clamp upper (defensive — slider max is 7200)
        return 86400
    return n


# VF-382: wizard expiry — operator-tunable post-completion fade window for the
# proper-onboard-wizard surfaces (drawer "Resume Onboarding" pill, /ui/onboard/{slug}
# resume render). PK directive 2026-05-08: lives under "Project wizard" section
# of /admin/portal/configuration/session-policy (NOT agent-telemetry, which was
# my original wrong placement). Allowed values updated to PK's chosen set.
WIZARD_EXPIRY_DEFAULT_SECONDS = 86400  # 24h
_WIZARD_EXPIRY_ALLOWED = {900, 3600, 21600, 43200, 64800, 86400}  # 15m / 1h / 6h / 12h / 18h / 24h


def get_wizard_expiry_seconds(db: Session) -> int:
    """VF-382: wizard post-completion expiry window in seconds. Reads
    `wizard_expiry_seconds` from system_settings; defaults to
    WIZARD_EXPIRY_DEFAULT_SECONDS (86400 = 24h) if unset or unparseable.
    Bounded {1800, 3600, 14400, 43200, 86400} (30m / 1h / 4h / 12h / 24h)
    by the slider UI; helper clamps defensively to [60, 86400].
    """
    raw = get_setting(db, "wizard_expiry_seconds", "")
    try:
        n = int(raw) if raw else WIZARD_EXPIRY_DEFAULT_SECONDS
    except ValueError:
        return WIZARD_EXPIRY_DEFAULT_SECONDS
    if n < 60:
        return 60
    if n > 86400:
        return 86400
    return n


def set_setting(db: Session, key: str, value: str, user_id: Optional[str] = None) -> None:
    row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    now = datetime.now(timezone.utc)
    if row is None:
        row = SystemSetting(key=key, value=value, updated_at=now, updated_by=user_id)
        db.add(row)
    else:
        row.value = value
        row.updated_at = now
        row.updated_by = user_id
    db.commit()


# ─── auth ───

def _require_super_admin(request: Request, db: Session):
    """Require SA elevation OR an elevated SU session. Matches /admin/ dual-path gate
    post-VF-264. The name is kept for call-site stability; the behaviour is now dual.
    See 0-MD/0-Documentation/public/su-elevation-tier.md. Fixed under VF-316.
    """
    from app.api.v2.admin import _require_sa
    user = _require_sa(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="SA elevation or elevated SU required.")
    return user


# ─── HTML page (legacy URL — VF-306 redirect to new portal) ───

@router.get("/admin/experimental/drift", response_class=HTMLResponse)
def experimental_drift_page(request: Request):
    """VF-306: legacy URL now 301s to the new portal location.

    The HTML rendering moved to admin_portal.portal_admin_agent_telemetry
    at /admin/portal/administration/agent-telemetry-and-drift. The XHR
    endpoints in this module (/admin/api/experimental/drift/*) are NOT
    affected — the new template still calls them.

    301 (not 410) because internal SAs may have stale bookmarks during
    the deprecation window; silent redirect is the bare-minimum cut.
    """
    from fastapi.responses import RedirectResponse
    return RedirectResponse(
        url="/admin/portal/administration/agent-telemetry-and-drift",
        status_code=301,
    )


# ─── JSON endpoints ───

@router.get("/admin/api/experimental/drift/summary")
def drift_summary(request: Request, db: Session = Depends(get_db)):
    _require_super_admin(request, db)
    active = db.query(DriftEscalation).filter(DriftEscalation.ended_at.is_(None)).count()
    lifetime = db.query(DriftEscalation).count()
    # avg clear duration (seconds)
    row = db.query(
        func.avg(func.extract("epoch", DriftEscalation.ended_at - DriftEscalation.started_at))
    ).filter(DriftEscalation.ended_at.isnot(None)).first()
    avg_clear_seconds = int(row[0]) if row and row[0] is not None else None
    affected = db.query(func.count(func.distinct(DriftEscalation.agent_id))).scalar() or 0
    total_agents = db.query(Agent).filter(Agent.status == "active").count()
    return {
        "drift_gate_enabled": get_bool(db, "drift_gate_enabled", True),
        "active": active,
        "lifetime": lifetime,
        "avg_clear_seconds": avg_clear_seconds,
        "agents_affected": affected,
        "agents_total": total_agents,
    }


@router.get("/admin/api/experimental/drift/timeline")
def drift_timeline(request: Request, db: Session = Depends(get_db)):
    _require_super_admin(request, db)
    since = datetime.now(timezone.utc) - timedelta(days=30)
    rows = (
        db.query(
            func.date(DriftEscalation.started_at).label("day"),
            func.count().label("n"),
        )
        .filter(DriftEscalation.started_at >= since)
        .group_by(func.date(DriftEscalation.started_at))
        .order_by("day")
        .all()
    )
    return [{"day": r.day.isoformat(), "count": r.n} for r in rows]


@router.get("/admin/api/experimental/drift/recent")
def drift_recent(
    request: Request,
    status: str = "all",
    limit: int = 50,
    db: Session = Depends(get_db),
):
    _require_super_admin(request, db)
    q = (
        db.query(DriftEscalation, Agent, Task, Project)
        .join(Agent, Agent.id == DriftEscalation.agent_id)
        .outerjoin(Task, Task.id == DriftEscalation.task_id)
        .join(Project, Project.id == DriftEscalation.project_id)
    )
    if status == "active":
        q = q.filter(DriftEscalation.ended_at.is_(None))
    elif status == "cleared":
        q = q.filter(DriftEscalation.ended_at.isnot(None))
    q = q.order_by(DriftEscalation.started_at.desc()).limit(min(limit, 200))
    out = []
    for esc, agent, task, project in q.all():
        duration_s = None
        if esc.ended_at and esc.started_at:
            duration_s = int((esc.ended_at - esc.started_at).total_seconds())
        out.append({
            "id": esc.id,
            "agent_id": esc.agent_id,
            "agent_name": agent.name,
            "task_id": esc.task_id,
            "task_number": task.task_number if task else None,
            "task_title": task.title if task else None,
            "project_id": esc.project_id,
            "project_slug": project.slug,
            "started_at": esc.started_at.isoformat(),
            "ended_at": esc.ended_at.isoformat() if esc.ended_at else None,
            "duration_seconds": duration_s,
            "active": esc.ended_at is None,
        })
    return out


@router.get("/admin/api/experimental/drift/by-agent")
def drift_by_agent(request: Request, db: Session = Depends(get_db)):
    _require_super_admin(request, db)
    rows = (
        db.query(
            DriftEscalation.agent_id,
            Agent.name,
            func.count(DriftEscalation.id).label("count"),
            func.avg(
                func.extract("epoch", DriftEscalation.ended_at - DriftEscalation.started_at)
            ).filter(DriftEscalation.ended_at.isnot(None)).label("avg_clear"),
            func.max(DriftEscalation.started_at).label("last_started"),
        )
        .join(Agent, Agent.id == DriftEscalation.agent_id)
        .group_by(DriftEscalation.agent_id, Agent.name)
        .order_by(func.count(DriftEscalation.id).desc())
        .all()
    )
    return [
        {
            "agent_id": r.agent_id,
            "agent_name": r.name,
            "escalations": r.count,
            "avg_clear_seconds": int(r.avg_clear) if r.avg_clear is not None else None,
            "last_started_at": r.last_started.isoformat() if r.last_started else None,
        }
        for r in rows
    ]


@router.get("/admin/api/experimental/drift/escalation/{escalation_id}")
def drift_escalation_detail(escalation_id: str, request: Request, db: Session = Depends(get_db)):
    _require_super_admin(request, db)
    esc = db.query(DriftEscalation).filter(DriftEscalation.id == escalation_id).first()
    if esc is None:
        raise HTTPException(status_code=404, detail="Escalation not found.")
    agent = db.query(Agent).filter(Agent.id == esc.agent_id).first()
    task = db.query(Task).filter(Task.id == esc.task_id).first() if esc.task_id else None
    project = db.query(Project).filter(Project.id == esc.project_id).first()

    # Timeline: all DriftEvalAttempt rows linked to this escalation, plus prompt rows
    # for this agent within the cycle that led to it (back-linked by _attach_firing_to_escalation).
    attempts = (
        db.query(DriftEvalAttempt)
        .filter(DriftEvalAttempt.escalation_id == escalation_id)
        .order_by(DriftEvalAttempt.attempted_at)
        .all()
    )
    return {
        "id": esc.id,
        "agent_id": esc.agent_id,
        "agent_name": agent.name if agent else None,
        "task_id": esc.task_id,
        "task_number": task.task_number if task else None,
        "task_title": task.title if task else None,
        "project_slug": project.slug if project else None,
        "started_at": esc.started_at.isoformat(),
        "ended_at": esc.ended_at.isoformat() if esc.ended_at else None,
        "cleared_by": esc.cleared_by,
        "cleared_reason": esc.cleared_reason,
        "attempts": [
            {
                "id": a.id,
                "attempted_at": a.attempted_at.isoformat(),
                "question_idx": a.question_idx,
                "response_hash": a.response_hash,
                "outcome": a.outcome,
            }
            for a in attempts
        ],
    }


from pydantic import BaseModel


class DriftToggleIn(BaseModel):
    enabled: bool


class DriftResetIn(BaseModel):
    confirm: bool = False


class DriftWindowIn(BaseModel):
    seconds: int  # bounded server-side to {1200, 1800, 3600, 7200} (20/30/60/120 min)


@router.post("/admin/api/experimental/drift/toggle", status_code=200)
def drift_toggle(body: DriftToggleIn, request: Request, db: Session = Depends(get_db)):
    """Flip drift_gate_enabled. Super-admin only."""
    user = _require_super_admin(request, db)
    set_setting(db, "drift_gate_enabled", "true" if body.enabled else "false", user.id)
    return {"drift_gate_enabled": body.enabled}


@router.post("/admin/api/experimental/drift/reset-all", status_code=200)
def drift_reset_all(body: DriftResetIn, request: Request, db: Session = Depends(get_db)):
    """Violent clear — end all active drift escalations. Preserves history + audit notes.
    Double-ask confirmation enforced by the UI; server requires `confirm: true` to be safe."""
    user = _require_super_admin(request, db)
    if not body.confirm:
        raise HTTPException(status_code=422, detail="Reset requires confirm=true.")
    from app.api.v2.drift_gate import reset_all_drift_state
    count = reset_all_drift_state(db, cleared_by_user_id=user.id)
    return {"ended": count}


# ─── VF-306 — Agent telemetry & drift extensions ───

# Slider values are operator-tunable but constrained to a small set so the
# UI presents 4 buttons (per VF-306). Manual SQL can set anything within the
# helper's clamp range; the slider POST validates.
_DRIFT_WINDOW_ALLOWED = {1200, 1800, 3600, 7200}


@router.post("/admin/api/experimental/drift/window", status_code=200)
def drift_window_set(body: DriftWindowIn, request: Request, db: Session = Depends(get_db)):
    """VF-306: set the drift-eval look-back window. Live — next gate firing
    picks up the new value (drift gate reads from SystemSetting on each eval).
    """
    user = _require_super_admin(request, db)
    if body.seconds not in _DRIFT_WINDOW_ALLOWED:
        raise HTTPException(
            status_code=422,
            detail=f"window must be one of {sorted(_DRIFT_WINDOW_ALLOWED)} seconds (20/30/60/120 min).",
        )
    set_setting(db, "drift_eval_window_seconds", str(body.seconds), user.id)
    return {"drift_eval_window_seconds": body.seconds}


@router.get("/admin/api/experimental/drift/window")
def drift_window_get(request: Request, db: Session = Depends(get_db)):
    """VF-306: read the current drift-eval window value."""
    _require_super_admin(request, db)
    return {"drift_eval_window_seconds": get_drift_window_seconds(db)}


# VF-382 — wizard expiry setter/getter (operator-tunable post-completion fade)

class WizardExpiryIn(BaseModel):
    seconds: int  # bounded server-side to {1800, 3600, 14400, 43200, 86400}


@router.post("/admin/api/experimental/wizard-expiry", status_code=200)
def wizard_expiry_set(body: WizardExpiryIn, request: Request, db: Session = Depends(get_db)):
    """VF-382: set the post-completion onboard-wizard expiry window. Live —
    next render of the drawer Resume affordance + /ui/onboard/{slug} resume
    route reads from SystemSetting on each request.
    """
    user = _require_super_admin(request, db)
    if body.seconds not in _WIZARD_EXPIRY_ALLOWED:
        raise HTTPException(
            status_code=422,
            detail=f"wizard expiry must be one of {sorted(_WIZARD_EXPIRY_ALLOWED)} seconds (15m / 1h / 6h / 12h / 18h / 24h).",
        )
    set_setting(db, "wizard_expiry_seconds", str(body.seconds), user.id)
    return {"wizard_expiry_seconds": body.seconds}


@router.get("/admin/api/experimental/wizard-expiry")
def wizard_expiry_get(request: Request, db: Session = Depends(get_db)):
    """VF-382: read the current wizard-expiry value (SA only)."""
    _require_super_admin(request, db)
    return {"wizard_expiry_seconds": get_wizard_expiry_seconds(db)}


@router.get("/admin/api/experimental/drift/agent-eval-stats")
def drift_agent_eval_stats(request: Request, db: Session = Depends(get_db)):
    """VF-306 item 3: per-agent drift-gate pass/fail tally (lifetime).

    Bare-minimum cut: lifetime totals only, no windowing. Joins agents to
    drift_eval_attempts and counts pass/fail outcomes per agent. Includes
    the `drift_eval_count` and `drift_eval_passed_at` columns from the
    agents table for additional context.
    """
    _require_super_admin(request, db)
    from sqlalchemy import case
    rows = (
        db.query(
            Agent.id.label("agent_id"),
            Agent.name.label("agent_name"),
            Agent.status.label("status"),
            Agent.drift_eval_count.label("eval_count"),
            Agent.drift_eval_passed_at.label("last_passed_at"),
            func.coalesce(
                func.sum(case((DriftEvalAttempt.outcome == "pass", 1), else_=0)),
                0,
            ).label("passes"),
            func.coalesce(
                func.sum(case((DriftEvalAttempt.outcome == "fail", 1), else_=0)),
                0,
            ).label("fails"),
        )
        .outerjoin(DriftEvalAttempt, DriftEvalAttempt.agent_id == Agent.id)
        .group_by(Agent.id, Agent.name, Agent.status, Agent.drift_eval_count, Agent.drift_eval_passed_at)
        .order_by(Agent.name)
        .all()
    )
    return [
        {
            "agent_id": r.agent_id,
            "agent_name": r.agent_name,
            "status": r.status,
            "eval_count": r.eval_count,
            "last_passed_at": r.last_passed_at.isoformat() if r.last_passed_at else None,
            "passes": int(r.passes),
            "fails": int(r.fails),
        }
        for r in rows
    ]


@router.get("/admin/api/experimental/agent-api-counters")
def agent_api_counters(request: Request, db: Session = Depends(get_db)):
    """VF-306 item 1: per-agent cumulative API-call counter.

    Bare-minimum cut: cumulative count + the timestamp it started counting
    (defaults to row creation; resets to now() on token cycle). UI labels
    each row with "Total: N (since <date>)".
    """
    _require_super_admin(request, db)
    rows = (
        db.query(Agent)
        .order_by(Agent.api_call_count.desc(), Agent.name)
        .all()
    )
    return [
        {
            "agent_id": a.id,
            "agent_name": a.name,
            "status": a.status,
            "project_id": a.project_id,
            "api_call_count": a.api_call_count or 0,
            "since": a.api_call_count_since.isoformat() if a.api_call_count_since else None,
            "last_seen_at": a.last_seen_at.isoformat() if a.last_seen_at else None,
            "has_token": a.api_token_hash is not None,
        }
        for a in rows
    ]
