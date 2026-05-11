"""VF-327 — Escalated Admin Portal routes.

Implements the board-paradigm shell at /admin/portal/* — left sidebar with
primes + sub-items, workspace pane that changes per sub. All routes SA-only
(vf_sa_session cookie OR elevated vf_session).

See 0-MD/proposed/ADMIN-PORTAL-REDESIGN-PROPOSAL.md for the Escalation Thesis.
See ADMIN-PORTAL-REDESIGN-MOCKUP.html for the click-through mockup.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.session import get_db

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# ═══════════════════════════════════════════════════════════════════════════
# Nav tree — single source of truth for the sidebar. Every portal route uses
# this via _portal_context(). Status "live" = clickable, "dev" = padlocked.
# ═══════════════════════════════════════════════════════════════════════════

# VF-344 PK feedback 2026-04-28: reordered Administration before Configuration,
# and Health between them. Operator priority sequence: Overview (always) → daily
# admin (Administration) → operational visibility (Health) → rare config
# (Configuration) → integrations + lifecycle. Configuration is set-once-per-env,
# revisited only on real changes; putting it after the daily-touch surfaces
# matches actual operator workflow.
NAV_TREE = [
    {
        "prime": "Overview", "icon": "★",
        "subs": [
            {"id": "overview", "label": "Landing", "status": "live",
             "url": "/admin/portal/"},
        ],
    },
    {
        "prime": "Administration", "icon": "⚐",
        "subs": [
            {"id": "adm-users",    "label": "Users",                "status": "live",
             "url": "/admin/portal/administration/users"},
            {"id": "adm-agents",   "label": "Agents",               "status": "live",
             "url": "/admin/portal/administration/agents"},
            {"id": "adm-sessions", "label": "Active sessions",      "status": "live",
             "url": "/admin/portal/administration/sessions"},
            # VF-306: Agent telemetry & Drift — bare-minimum migration of the
            # legacy /admin/experimental/drift surface into the new portal.
            # PK 2026-04-28 placement feedback: sits BELOW Active sessions
            # (not above) — telemetry is a deeper-dive surface than the
            # daily-touch lists above it.
            {"id": "adm-agent-telemetry", "label": "Agent telemetry and Drift", "status": "live",
             "url": "/admin/portal/administration/agent-telemetry-and-drift"},
            {"id": "adm-groups",   "label": "Groups · Teams",  "status": "dev"},
            {"id": "adm-roles",    "label": "Custom roles",         "status": "dev"},
        ],
    },
    {
        "prime": "Health", "icon": "♥",
        "subs": [
            # Health is modelled like Windows Task Manager — one canonical
            # surface per role. "System" is the comprehensive dashboard
            # (VM, containers, TLS, Caddy, Postgres, jobs, events, sessions,
            # agents, auth, activity). Audit log stays separate because
            # log-reading is a different interaction pattern from
            # dashboard-watching. Configuration is allowed to fragment;
            # Health is not. SU gets the same signals on /ui/health.
            {"id": "hlt-overview", "label": "System",               "status": "live",
             "url": "/admin/portal/health/overview"},
            {"id": "hlt-audit",    "label": "Audit log",            "status": "live",
             "url": "/admin/portal/health/audit"},
        ],
    },
    {
        "prime": "Configuration", "icon": "⚙",
        "subs": [
            {"id": "cfg-certs",    "label": "Certificates",        "status": "live",
             "url": "/admin/portal/configuration/certificates"},
            # VF-341 §5.1: Session policy graduates from DEV placeholder to live
            # form. Sits directly below Certificates per PK directive.
            {"id": "cfg-session",  "label": "Session policy",       "status": "live",
             "url": "/admin/portal/configuration/session-policy"},
            {"id": "cfg-sso",      "label": "SSO · OIDC · SAML", "status": "dev"},
            {"id": "cfg-smtp",     "label": "Email · SMTP",    "status": "dev"},
            {"id": "cfg-backup",   "label": "Backup · Retention","status": "dev"},
            {"id": "cfg-branding", "label": "Branding",             "status": "dev"},
            {"id": "cfg-single",   "label": "Single-user mode",     "status": "dev"},
            {"id": "cfg-flags",    "label": "Feature flags",        "status": "dev"},
        ],
    },
    {
        "prime": "Integrations", "icon": "⇄",
        "subs": [
            {"id": "int-forgejo",  "label": "Forgejo",              "status": "dev"},
            {"id": "int-vault",    "label": "VaultWarden OAuth",    "status": "dev"},
            {"id": "int-mcp",      "label": "MCP servers",          "status": "dev"},
            {"id": "int-webhook",  "label": "Webhooks · Event sink", "status": "dev"},
        ],
    },
    {
        "prime": "Lifecycle", "icon": "↻",
        "subs": [
            {"id": "lc-env",       "label": "Environment info",     "status": "live",
             "url": "/admin/portal/lifecycle/environment"},
            {"id": "lc-upgrade",   "label": "Upgrade · Migration", "status": "dev"},
            {"id": "lc-dr",        "label": "Disaster recovery",    "status": "dev"},
        ],
    },
    # NOTE: VF-353 customer-onboard test wizard moved from admin portal to
    # /ui/test-wizard (board / SU surface) — SA can't touch projects + 15min
    # elevation timeout makes testing painful. Lives in board sidebar's
    # temporary "Test" section per base.html. Admin-portal placement reserved
    # for the customer-facing wizard if/when it ships under Settings post-RC.
]


PLACEHOLDER_COPY = {
    "cfg-sso":      "OIDC / SAML single sign-on. Operator configures an IDP, sets group→role mapping, enables/disables password fallback. Lands after the cert lifecycle settles so the trust story is in place first.",
    "cfg-smtp":     "Outbound SMTP for invitations, password-reset emails, and audit-log alerts. Requires a decision on default provider (none / Postmark-style / ship-with-Postfix).",
    "cfg-backup":   "Scheduled DB + ops/certs backup with retention policy. Currently runs out-of-band via backup_db.sh — this surface brings it in-band with observability.",
    "cfg-branding": "Instance name, logo, theme-lock, legal footer. Lets a self-host brand the instance without touching templates.",
    "cfg-session":  "Max session lifetime, idle logout, elevation TTL, reauth requirement on sensitive actions.",
    "cfg-single":   "Toggle single-user mode — no sign-up, no multi-project UI, collapses the board to a notepad-style workspace. For solo self-hosters.",
    "cfg-flags":    "Feature flag board for in-flight experiments (board-git engine, GUESS substrate, sync variants). Admin can enable/disable per-project or globally.",
    "adm-groups":   "Teams and groups — grouping users for bulk role assignment and project membership. Model doesn't exist yet; reserved here so the nav shape survives adding it.",
    "adm-roles":    "Custom roles beyond super_user / user. Reserved for when the permission model grows past the current two-level split.",
    "int-forgejo":  "Git admin surface — user sync, hook config, repo templates. Currently Forgejo is mounted but managed via its own UI; this brings it into VF+.",
    "int-vault":    "OAuth bridge into VaultWarden — single-sign-on to the org's password vault. Padlocked pending VaultWarden OAuth PR upstream.",
    "int-mcp":      "MCP server registration for AI agent integrations — register external tools the agents can call from board context.",
    "int-webhook":  "Event-sink config — where to POST task events (Slack, Discord, custom URL). Replaces ad-hoc webhook wiring.",
    "lc-upgrade":   "Version + migration state, safe-upgrade wizard, rollback path. Important for self-hosters who don't want to learn compose.",
    "lc-dr":        "Disaster-recovery restore wizard. Picks a backup, confirms the reset, restores the DB + certs.",
}


def _find_label(sub_id: str) -> str:
    for grp in NAV_TREE:
        for s in grp["subs"]:
            if s["id"] == sub_id:
                return s["label"]
    return sub_id


def _portal_context(request: Request, db: Session, active_admin: str,
                    extra: dict | None = None) -> dict:
    """Shared context for every portal template. Handles elevation TTL,
    nav highlight, acting-state detection (VF-328), and any per-page extras."""
    from app.api.v2.admin import _get_sa_user
    # Don't re-auth here — the route has already gated. But we need the
    # elevation timestamp for the countdown. Pull the session.
    from app.models.session import UserSession
    now = datetime.now(timezone.utc)
    ttl_seconds = None
    has_su_session = False
    session_id = request.cookies.get("vf_session")
    if session_id:
        has_su_session = True
        sess = (db.query(UserSession)
                .filter(UserSession.id == session_id)
                .first())
        if sess and sess.elevated_until:
            delta = sess.elevated_until - now
            ttl_seconds = max(0, int(delta.total_seconds()))
    # Break-glass sessions (vf_sa_session) don't carry an elevated_until —
    # they're full SA for the cookie lifetime. Show no countdown.

    # VF-328 acting-state detection — drives pill text + body data attrs +
    # break-glass chrome in admin_base.html. Three mutually-exclusive states:
    #   - pure break-glass: vf_sa_session present, no vf_session
    #   - stacked SA:       both cookies present
    #   - SU-elevated only: vf_session with elevation, no vf_sa_session
    sa_user = _get_sa_user(request, db)
    has_sa_cookie = sa_user is not None
    acting_state = {
        "has_sa_cookie": has_sa_cookie,
        "has_su_session": has_su_session,
        "is_pure_break_glass": has_sa_cookie and not has_su_session,
        "is_stacked_sa": has_sa_cookie and has_su_session,
        "is_su_elevated_only": has_su_session and not has_sa_cookie,
    }

    ctx = {
        "request": request,
        "nav_tree": NAV_TREE,
        "active_admin": active_admin,
        "elevation_ttl_seconds": ttl_seconds,
        "acting_state": acting_state,
    }
    if extra:
        ctx.update(extra)
    return ctx


def _require_sa_or_login(request: Request, db: Session):
    """Gate for all portal routes. Returns the SA user, or None if the caller
    should be redirected to /admin/login."""
    from app.api.v2.admin import _require_sa
    return _require_sa(request, db)


# ═══════════════════════════════════════════════════════════════════════════
# Routes — all live subs + one generic placeholder handler.
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/admin/portal/", response_class=HTMLResponse)
@router.get("/admin/portal", response_class=HTMLResponse)
def portal_overview(request: Request, db: Session = Depends(get_db)):
    """Overview landing — tile grid + recent audit actions."""
    sa = _require_sa_or_login(request, db)
    if not sa:
        return RedirectResponse(url="/admin/login", status_code=302)

    from app.models.user import User
    from app.models.activity import ActivityEvent
    from sqlalchemy import desc

    # Tile stats
    active_user_count = db.query(User).filter(User.status == "active").count()
    suspended_count = db.query(User).filter(User.status == "suspended").count()
    from app.models.agent import Agent
    active_agents = db.query(Agent).filter(Agent.status == "active").count()

    # Cert info — best-effort, don't fail the page if cert reading breaks.
    cert_summary = None
    try:
        from app.api.v2.proxy import get_cert_info
        info = get_cert_info()
        cert_summary = {
            "mode": info.get("mode", "unknown"),
            "days": info.get("days_remaining"),
        }
    except Exception:
        pass

    # Recent audit actions — the "what just happened here" stream.
    recent_events = (db.query(ActivityEvent)
                     .filter(ActivityEvent.action.in_([
                         "proxy_reloaded", "cert_swapped", "proxy_mode_switched",
                         "proxy_cert_renewed", "proxy_cert_exported",
                         "admin_login", "admin_elevated", "admin_unelevated",
                         "user_role_changed", "user_suspended", "user_unsuspended",
                         "user_password_reset", "agent_created", "agent_cycled",
                         "agent_revoked",
                     ]))
                     .order_by(desc(ActivityEvent.created_at))
                     .limit(12)
                     .all())
    actor_ids = {e.actor_user_id for e in recent_events if e.actor_user_id}
    actors = {u.id: u for u in db.query(User).filter(User.id.in_(actor_ids)).all()}
    recent = []
    for e in recent_events:
        actor = actors.get(e.actor_user_id) if e.actor_user_id else None
        actor_name = (actor.display_name if actor else None) or (actor.username if actor else None) or "system"
        recent.append({
            "when": e.created_at,
            "actor": actor_name,
            "action": e.action,
            "details": e.details or "",
        })

    # Environment tier detection (heuristic — VIBEFORGE_HOSTNAME hint)
    hostname = os.environ.get("VIBEFORGE_HOSTNAME", "localhost")
    if "-dev." in hostname: tier = "DEV"
    elif "-uat." in hostname: tier = "UAT"
    elif hostname and "." in hostname and "localhost" not in hostname: tier = "PROD"
    else: tier = "LOCAL"

    return templates.TemplateResponse("ui/admin_portal_overview.html",
        _portal_context(request, db, "overview", {
            "tier": tier,
            "hostname": hostname,
            "active_user_count": active_user_count,
            "suspended_count": suspended_count,
            "active_agents": active_agents,
            "cert_summary": cert_summary,
            "recent_events": recent,
        }))


@router.get("/admin/portal/configuration/certificates", response_class=HTMLResponse)
def portal_cfg_certificates(request: Request, db: Session = Depends(get_db)):
    sa = _require_sa_or_login(request, db)
    if not sa: return RedirectResponse(url="/admin/login", status_code=302)
    try:
        from app.api.v2.proxy import get_cert_info
        current_cert = get_cert_info()
    except Exception as e:
        current_cert = {"status": "error", "mode": "unknown", "error": str(e)}
    return templates.TemplateResponse("ui/admin_portal_cfg_certs.html",
        _portal_context(request, db, "cfg-certs", {"current_cert": current_cert}))


@router.get("/admin/portal/configuration/certificates/change-cert",
            response_class=HTMLResponse)
def portal_cfg_change_cert(request: Request, db: Session = Depends(get_db)):
    """T5 cert wizard — re-homed from /ui/admin/proxy/change-cert."""
    sa = _require_sa_or_login(request, db)
    if not sa: return RedirectResponse(url="/admin/login", status_code=302)
    try:
        from app.api.v2.proxy import get_cert_info
        current_cert = get_cert_info()
    except Exception as e:
        current_cert = {"status": "error", "mode": "unknown", "error": str(e)}
    return templates.TemplateResponse("ui/admin_portal_change_cert.html",
        _portal_context(request, db, "cfg-certs", {"current_cert": current_cert}))


def _format_audit_event(action: str, details: str | None) -> str:
    """VF-343 PK feedback 2026-04-28: render the JSON `details` blob as a
    human-readable summary instead of dumping raw JSON in the audit panel.

    Action-specific formatters cover the common events (agent + user + cert +
    task lifecycle). Unmapped actions fall through to a key=value join.

    The output is plain text - no HTML - so frontend renders directly without
    XSS concern.
    """
    if not details:
        return ""
    import json as _json
    try:
        d = _json.loads(details)
    except (ValueError, TypeError):
        return str(details)[:200]
    if not isinstance(d, dict):
        return str(d)[:200]

    def g(k, default="?"):
        v = d.get(k)
        return default if v is None or v == "" else str(v)

    # Agent events
    if action == "agent_token_cycled":
        sc = " (self-cycle)" if d.get("self_cycle") else ""
        return f"Cycled token for agent {g('agent_name')} (by {g('actor')}){sc}"
    if action == "agent_token_revoked":
        return f"Revoked token only - agent {g('agent_name')} stays active; operator must reissue via board UI"
    if action == "agent_revoked":
        return f"Revoked agent {g('agent_name')} (token nulled, agent disabled)"
    if action == "agent_restored":
        return f"Restored agent {g('agent_name')} - cycle token to mint a fresh one"
    if action == "agent_created":
        return f"Created agent {g('agent_name')} for project {g('project')}"
    # User events
    if action == "user_created":
        return f"Created user {g('username', g('user_name'))} as {g('role')}"
    if action == "user_role_changed":
        return f"Changed role for {g('username')}: {g('from')} -> {g('to')}"
    if action == "user_suspended":
        r = d.get("reason")
        return f"Suspended user {g('username')}" + (f" - reason: {r}" if r else "")
    if action == "user_unsuspended":
        return f"Unsuspended user {g('username')}"
    if action == "user_soft_deleted":
        n = d.get("agents_revoked", 0)
        return f"Soft-deleted user {g('username')} (cascaded revoke to {n} agent(s))"
    if action == "user_restored":
        return f"Restored user {g('username')}"
    if action == "user_password_reset":
        return f"Reset password for {g('username')} - new password shown once"
    if action == "user_force_password_change_set":
        return f"Set force-password-change flag for {g('username')}"
    # Cert / proxy events
    if action == "proxy_cert_swapped":
        return f"Swapped proxy cert to mode={g('target_mode', g('mode'))}"
    if action == "proxy_cert_renewed":
        return f"Renewed proxy cert (mode={g('mode')})"
    if action == "proxy_cert_exported":
        return f"Exported current fullchain"
    if action == "proxy_mode_switched":
        return f"Switched proxy mode: {g('from')} -> {g('to')}"
    # SA / SU session events
    if action == "sa_session_started":
        return "SA elevation started"
    if action == "sa_session_ended":
        return "SA elevation ended"
    if action == "sa_session_invalidated":
        return f"SA session invalidated{(' - reason: ' + d.get('reason')) if d.get('reason') else ''}"
    # Task / board events
    if action == "task_created":
        title = d.get("title") or ""
        return f"Created task {g('short_id')}: {title[:80]}"
    if action == "status_changed":
        return f"Status changed {g('short_id')}: {g('from')} -> {g('to')}"
    if action == "blocked_by_set":
        r = d.get("reason") or ""
        return f"Set blocked-by {g('short_id', '')} -> {g('to_short')}" + (f" - reason: {r[:80]}" if r else "")
    if action == "blocked_by_cleared":
        return f"Cleared blocked-by on {g('short_id')}"
    if action == "note_posted":
        return f"Posted note on {g('short_id', d.get('task_id', '?')[:8])}"
    if action == "phase_changed":
        return f"Phase changed {g('short_id')}: {g('from')} -> {g('to')}"
    if action == "priority_changed":
        return f"Priority changed {g('short_id')}: {g('from')} -> {g('to')}"
    if action == "owner_changed":
        return f"Owner changed {g('short_id')}: {g('from')} -> {g('to')}"

    # Fallback: action name + key fields as compact key=value pairs
    parts = []
    for k, v in d.items():
        if v is None or v == "":
            continue
        if isinstance(v, (dict, list)):
            v = _json.dumps(v)
        s = str(v)
        if len(s) > 60:
            s = s[:60] + "…"
        parts.append(f"{k}={s}")
    return " · ".join(parts)[:200] if parts else ""


def _serialize_agent_for_portal(a, proj_slug=None, proj_name=None) -> dict:
    """Minimal payload for tree rendering + drawer initial state. Audit tail
    is fetched on-drawer-open via a separate endpoint to keep the initial
    page payload manageable."""
    # VF-343 PK feedback 2026-04-28: token_pending = agent is still 'active'
    # but its current token has been revoked (admin chose Revoke-token-only
    # rather than Revoke agent). Operator must visit board UI to issue new.
    token_pending = (a.status == "active") and (a.api_token_hash is None)
    return {
        "id": a.id,
        "name": a.name,
        "slug": a.slug,
        "description": a.description or "",
        "role_label": (a.description or "").split("·")[0].strip() if a.description else "",
        "status": a.status,
        "model_type": a.model_type or "unknown",
        "model_name": a.model_name or "",
        "token_prefix": a.token_prefix or "",
        "token_pending": token_pending,
        "last_seen_at": a.last_seen_at.isoformat() if a.last_seen_at else None,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "revoked_at": a.revoked_at.isoformat() if a.revoked_at else None,
        "project_slug": proj_slug,
        "project_name": proj_name,
    }


def _build_agents_tree(db: Session) -> list[dict]:
    """Build the Shape B tree (user -> project -> agent cards) per
    user-agent-model.md v3 §5.1. Returns a list shaped for direct Jinja +
    JSON consumption. Skips orphan agents (no created_by) — flag them in
    logs but don't break the tree."""
    from app.models.agent import Agent
    from app.models.user import User
    from app.models.project import Project
    from sqlalchemy import desc

    agents = db.query(Agent).order_by(desc(Agent.created_at)).all()
    user_ids = {a.created_by for a in agents if a.created_by}
    project_ids = {a.project_id for a in agents if a.project_id}
    users_by_id = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()} if user_ids else {}
    projects_by_id = {p.id: p for p in db.query(Project).filter(Project.id.in_(project_ids)).all()} if project_ids else {}

    # Also pull users who have zero agents but are still active SU/users — tree
    # intentionally only shows users with agents (per mockup behaviour).
    tree_by_user: dict[str, dict] = {}
    for a in agents:
        if not a.created_by:
            continue  # orphan; skip (schema invariant will tighten later)
        user = users_by_id.get(a.created_by)
        if not user:
            continue  # stale FK — shouldn't happen but defensive
        project = projects_by_id.get(a.project_id) if a.project_id else None
        proj_slug = project.slug if project else "(no project)"
        proj_name = project.name if project else "(no project)"

        if user.id not in tree_by_user:
            tree_by_user[user.id] = {
                "user": {
                    "id": user.id,
                    "username": user.username or user.id[:8],
                    "display_name": user.display_name,
                    "role": user.role,
                    "status": user.status,
                    "email": user.email,
                },
                "projects_map": {},
            }
        pm = tree_by_user[user.id]["projects_map"]
        if proj_slug not in pm:
            pm[proj_slug] = {"slug": proj_slug, "name": proj_name, "active": [], "archive": []}
        bucket = "active" if a.status == "active" else "archive"
        pm[proj_slug][bucket].append(_serialize_agent_for_portal(a, proj_slug, proj_name))

    # Flatten projects_map → list; sort users by display_name
    tree = []
    for entry in tree_by_user.values():
        projects_list = list(entry["projects_map"].values())
        projects_list.sort(key=lambda p: p["slug"])
        tree.append({"user": entry["user"], "projects": projects_list})
    tree.sort(key=lambda e: (e["user"]["role"] != "super_user", e["user"]["display_name"].lower()))
    return tree


def _build_agents_flat_list(db: Session) -> list[dict]:
    """VF-343: flat list of all agents with owner + project denormalised on each
    row. Replaces _build_agents_tree as the data source for the master-detail
    Agents workspace. Tree is kept around because _agents_for_user (Users
    drawer's Bound-agents section) shares the per-project shape — refactoring
    that is out of VF-343 scope."""
    from app.models.agent import Agent
    from app.models.user import User
    from app.models.project import Project
    from sqlalchemy import desc

    agents = db.query(Agent).order_by(desc(Agent.created_at)).all()
    user_ids = {a.created_by for a in agents if a.created_by}
    project_ids = {a.project_id for a in agents if a.project_id}
    users_by_id = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()} if user_ids else {}
    projects_by_id = {p.id: p for p in db.query(Project).filter(Project.id.in_(project_ids)).all()} if project_ids else {}

    out = []
    for a in agents:
        user = users_by_id.get(a.created_by) if a.created_by else None
        project = projects_by_id.get(a.project_id) if a.project_id else None
        item = _serialize_agent_for_portal(
            a,
            project.slug if project else None,
            project.name if project else None,
        )
        item["owner"] = ({
            "id": user.id,
            "username": user.username or "",
            "display_name": user.display_name or "",
            "role": user.role,
            "status": user.status,
        } if user else None)
        out.append(item)

    # VF-343 PK feedback 2026-04-28: sort by (status priority, last_seen_at DESC).
    # Active agents come before revoked; within each group, most-recently-active
    # at top. Agents with no last_seen_at sort to the bottom of their group.
    def _agent_sort_key(item):
        status_pri = 0 if item.get("status") == "active" else 1
        ls = item.get("last_seen_at")
        # Negative timestamp for DESC ordering; 0 (oldest) for None
        ts = 0
        if ls:
            try:
                from datetime import datetime
                ts = datetime.fromisoformat(ls.replace("Z", "+00:00")).timestamp()
            except Exception:
                ts = 0
        return (status_pri, -ts)
    out.sort(key=_agent_sort_key)
    return out


def _agents_for_user(db: Session, user_id: str) -> list[dict]:
    """Compact per-user grouped-by-project list for the Users drawer's
    'Bound agents' section. Same shape as _build_agents_tree's projects branch."""
    from app.models.agent import Agent
    from app.models.project import Project
    from sqlalchemy import desc
    agents = (db.query(Agent)
              .filter(Agent.created_by == user_id)
              .order_by(desc(Agent.created_at))
              .all())
    project_ids = {a.project_id for a in agents if a.project_id}
    projects_by_id = {p.id: p for p in db.query(Project).filter(Project.id.in_(project_ids)).all()} if project_ids else {}
    pm: dict[str, dict] = {}
    for a in agents:
        project = projects_by_id.get(a.project_id) if a.project_id else None
        slug = project.slug if project else "(no project)"
        name = project.name if project else "(no project)"
        if slug not in pm:
            pm[slug] = {"slug": slug, "name": name, "active": [], "archive": []}
        bucket = "active" if a.status == "active" else "archive"
        pm[slug][bucket].append(_serialize_agent_for_portal(a, slug, name))
    out = list(pm.values())
    out.sort(key=lambda p: p["slug"])
    return out


@router.get("/admin/portal/administration/users", response_class=HTMLResponse)
@router.get("/admin/portal/administration/users/{open_id}", response_class=HTMLResponse)
def portal_admin_users(request: Request, db: Session = Depends(get_db), open_id: str | None = None):
    sa = _require_sa_or_login(request, db)
    if not sa: return RedirectResponse(url="/admin/login", status_code=302)
    from app.models.user import User

    # VF-343 PK feedback 2026-04-28: sort by (status priority, last_activity DESC).
    # User model has no last_login_at column — true "last activity" comes from
    # MAX(UserSession.last_activity_at) per user. Fetch all users + last-activity
    # map, then sort in Python. Volume is tiny (personal-VPS) so no perf concern.
    users = db.query(User).all()
    # VF-329: pre-fetch project memberships for every user in one query, then
    # group client-side. Master-detail renders the Memberships panel inline so
    # we avoid a per-user roundtrip on selection.
    from app.models.project_member import ProjectMember
    from app.models.project import Project
    user_ids = [u.id for u in users]
    mem_rows = (
        db.query(ProjectMember, Project)
        .join(Project, ProjectMember.project_id == Project.id)
        .filter(ProjectMember.user_id.in_(user_ids))
        .order_by(Project.name)
        .all()
    ) if user_ids else []
    mems_by_user: dict[str, list] = {}
    for m, p in mem_rows:
        mems_by_user.setdefault(m.user_id, []).append({
            "slug": p.slug,
            "name": p.name,
            "role": m.role,
            "is_owner": p.owner_id == m.user_id,
        })

    # VF-343: fetch MAX(last_activity_at) per user from the sessions table.
    # This is the real "last seen on the board" signal — drives both the row
    # display ("3min ago") and the recency sort below.
    from app.models.session import UserSession
    from sqlalchemy import func
    last_activity_rows = (
        db.query(UserSession.user_id, func.max(UserSession.last_activity_at).label("la"))
        .filter(UserSession.user_id.in_(user_ids))
        .group_by(UserSession.user_id)
        .all()
    ) if user_ids else []
    last_by_user = {r.user_id: r.la for r in last_activity_rows}

    # Serialize + attach agents_by_project + memberships for each user
    users_payload = []
    for u in users:
        la = last_by_user.get(u.id)
        users_payload.append({
            "id": u.id,
            "username": u.username or u.id[:8],
            "email": u.email,
            "display_name": u.display_name,
            "role": u.role,
            "status": u.status,
            "must_change_password": bool(u.must_change_password),
            # last_login_at kept for backwards-compat with existing JS (it reads this
            # field for the "Last seen" line). Now sourced from MAX session activity.
            "last_login_at": la.isoformat() if la else None,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "is_self": (u.id == sa.id),
            "agents_by_project": _agents_for_user(db, u.id),
            "memberships": mems_by_user.get(u.id, []),
        })

    # VF-343 PK feedback 2026-04-28: sort by (status priority, last_activity DESC).
    # Active users first, then by recency within each status group. Users with no
    # session activity sort to the bottom of their status group.
    def _user_sort_key(item):
        s = item.get("status")
        status_pri = 0 if s == "active" else (1 if s == "suspended" else 2)
        la = item.get("last_login_at")
        ts = 0
        if la:
            try:
                from datetime import datetime
                ts = datetime.fromisoformat(la.replace("Z", "+00:00")).timestamp()
            except Exception:
                ts = 0
        return (status_pri, -ts)
    users_payload.sort(key=_user_sort_key)

    # Guard: detect last active SU — used by drawer + dot menu to gate destructive ops.
    active_su_count = db.query(User).filter(
        User.role == "super_user", User.status == "active"
    ).count()

    return templates.TemplateResponse("ui/admin_portal_users.html",
        _portal_context(request, db, "adm-users", {
            "users_payload": users_payload,
            "open_id": open_id,
            "active_su_count": active_su_count,
        }))


@router.get("/admin/portal/administration/agents", response_class=HTMLResponse)
@router.get("/admin/portal/administration/agents/{open_id}", response_class=HTMLResponse)
def portal_admin_agents(request: Request, db: Session = Depends(get_db), open_id: str | None = None):
    sa = _require_sa_or_login(request, db)
    if not sa: return RedirectResponse(url="/admin/login", status_code=302)
    # VF-343: switched from tree (Shape B) to flat list for the master-detail
    # workspace. Each row carries owner + project denormalised so the list pane
    # can render without a JOIN-time lookup.
    agents_payload = _build_agents_flat_list(db)
    return templates.TemplateResponse("ui/admin_portal_agents.html",
        _portal_context(request, db, "adm-agents", {
            "agents_payload": agents_payload,
            "open_id": open_id,
        }))


@router.get("/admin/portal/api/users/{user_id}/audit")
def portal_user_audit(user_id: str, request: Request, db: Session = Depends(get_db),
                      limit: int = 50):
    """VF-343 PK feedback 2026-04-28: last N ActivityEvent rows authored by this
    user. Called by Users workspace detail pane (default 50, expand to 200).
    Not persisted to a separate table - this is a read-side window over the
    existing audit stream. A richer Audit Log workspace lands separately."""
    sa = _require_sa_or_login(request, db)
    if not sa:
        raise HTTPException(status_code=401)
    from app.models.activity import ActivityEvent
    from sqlalchemy import desc
    limit = max(1, min(limit, 200))
    rows = (db.query(ActivityEvent)
            .filter(ActivityEvent.actor_user_id == user_id)
            .order_by(desc(ActivityEvent.created_at))
            .limit(limit)
            .all())
    audit = [{
        "t": e.created_at.strftime("%Y-%m-%d %H:%M") if e.created_at else "",
        "e": e.action,
        "d": _format_audit_event(e.action, e.details),
    } for e in rows]
    return {"audit": audit, "limit": limit, "count": len(audit)}


@router.get("/admin/portal/api/agents/{agent_id}/detail")
def portal_agent_detail(agent_id: str, request: Request, db: Session = Depends(get_db),
                        audit_limit: int = 50):
    """AJAX endpoint — returns recent activity + bindings for a single agent.
    Called by the master-detail pane on selection. Keeps the initial list
    payload small. VF-343 PK feedback 2026-04-28: audit_limit default 50,
    capped at 200 (frontend "Show more" button calls with limit=200)."""
    sa = _require_sa_or_login(request, db)
    if not sa:
        raise HTTPException(status_code=401)
    from app.models.agent import Agent
    from app.models.user import User
    from app.models.project import Project
    from app.models.activity import ActivityEvent
    from sqlalchemy import desc

    audit_limit = max(1, min(audit_limit, 200))  # bound the query

    a = db.query(Agent).filter(Agent.id == agent_id).first()
    if not a:
        raise HTTPException(status_code=404, detail="agent not found")
    user = db.query(User).filter(User.id == a.created_by).first() if a.created_by else None
    project = db.query(Project).filter(Project.id == a.project_id).first() if a.project_id else None

    # Recent activity — actions by this agent (actor_user_id is null for agent actors;
    # the actor_name column stores the agent display name at insert time)
    recent = (db.query(ActivityEvent)
              .filter(ActivityEvent.details.ilike(f"%{a.name}%"))
              .order_by(desc(ActivityEvent.created_at))
              .limit(audit_limit)
              .all())
    audit = []
    for e in recent:
        audit.append({
            "t": e.created_at.strftime("%Y-%m-%d %H:%M") if e.created_at else "",
            "e": e.action,
            "d": _format_audit_event(e.action, e.details),
        })

    return {
        "agent": _serialize_agent_for_portal(a,
            project.slug if project else None, project.name if project else None),
        "user": {
            "id": user.id, "username": user.username or "",
            "display_name": user.display_name, "role": user.role, "status": user.status,
        } if user else None,
        "project": {"slug": project.slug, "name": project.name} if project else None,
        "audit": audit,
    }


@router.post("/admin/portal/api/agents/{agent_id}/cycle")
def portal_agent_cycle(agent_id: str, request: Request, db: Session = Depends(get_db)):
    """Thin wrapper — delegates to /admin/api/agents/{id}/cycle under the SA gate."""
    from app.api.v2.admin import cycle_agent_token
    return cycle_agent_token(agent_id, request, db)


@router.post("/admin/portal/api/agents/{agent_id}/revoke")
def portal_agent_revoke(agent_id: str, request: Request, db: Session = Depends(get_db)):
    from app.api.v2.admin import revoke_agent
    return revoke_agent(agent_id, request, db)


@router.post("/admin/portal/api/agents/{agent_id}/revoke-token")
def portal_agent_revoke_token(agent_id: str, request: Request, db: Session = Depends(get_db)):
    """VF-343 PK feedback 2026-04-28: revoke the agent's current TOKEN without
    revoking the agent itself. Agent record stays active; api_token_hash is
    nulled so the next bearer-token auth fails. Operator self-services a fresh
    token via the board UI (VF-342 - Agent token management UI).

    Distinct from revoke_agent which retires the entire agent identity.
    """
    sa = _require_sa_or_login(request, db)
    if not sa:
        raise HTTPException(status_code=401)
    from app.models.agent import Agent
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found.")
    if agent.status != "active":
        raise HTTPException(status_code=422,
            detail=f"Agent is {agent.status} - revoke-token-only applies to active agents only. Use Restore first if needed.")
    if agent.api_token_hash is None:
        raise HTTPException(status_code=422,
            detail="Agent has no active token to revoke - already in token-pending state.")

    agent.api_token_hash = None
    agent.token_prefix = None

    # Audit event - distinct action from "agent_revoked" so the audit log can
    # tell the two paths apart.
    from app.models.activity import ActivityEvent
    db.add(ActivityEvent(
        project_id=agent.project_id,
        actor_type="human",
        actor_user_id=sa.id,
        action="agent_token_revoked",
        details=f"Agent token revoked by SA. Agent: {agent.name}. Operator must reissue via board UI.",
    ))
    db.commit()
    return {
        "ok": True,
        "message": f"Token for agent {agent.name} revoked. Agent remains active; operator must reissue via board UI.",
        "token_pending": True,
    }


@router.post("/admin/portal/api/agents/{agent_id}/restore")
def portal_agent_restore(agent_id: str, request: Request, db: Session = Depends(get_db)):
    from app.api.v2.admin import restore_agent
    return restore_agent(agent_id, request, db)


@router.get("/admin/portal/health/audit", response_class=HTMLResponse)
def portal_health_audit(request: Request, db: Session = Depends(get_db)):
    sa = _require_sa_or_login(request, db)
    if not sa: return RedirectResponse(url="/admin/login", status_code=302)
    from app.models.activity import ActivityEvent
    from app.models.user import User
    from sqlalchemy import desc
    events = (db.query(ActivityEvent)
              .order_by(desc(ActivityEvent.created_at))
              .limit(200)
              .all())
    user_ids = {e.actor_user_id for e in events if e.actor_user_id}
    actors = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()}
    rows = []
    for e in events:
        a = actors.get(e.actor_user_id) if e.actor_user_id else None
        # Target display: prefer explicit project/task id columns; otherwise leave blank.
        target = ""
        if getattr(e, "task_id", None):
            target = f"task {e.task_id[:8]}"
        elif getattr(e, "project_id", None):
            target = f"project {e.project_id[:8]}"
        rows.append({
            "when": e.created_at,
            "actor": (a.display_name or a.username) if a else "(system / agent)",
            "action": e.action,
            "target": target,
            "details": e.details or "",
        })
    return templates.TemplateResponse("ui/admin_portal_audit.html",
        _portal_context(request, db, "hlt-audit", {"events": rows}))


def _serialize_session(sess, user, now) -> dict:
    """VF-341: payload shape used by both list and detail endpoints. Keeps
    the workspace JS simple — one row dict it can render in either pane.
    """
    from app.api.v2 import session_policy as _sp
    from app.api.v2.ui import _parse_ua
    name = (user.display_name if user else None) or (user.username if user else None) or "(unknown)"
    role = user.role if user else "-"
    elevated = bool(sess.elevated_until and sess.elevated_until > now)
    state = _sp.derive_session_state(sess, now=now)
    last_age_s = None
    if sess.last_activity_at is not None:
        last_age_s = int((now - sess.last_activity_at).total_seconds())
    return {
        "id": sess.id,
        "user_id": sess.user_id,
        "user_name": name,
        "user_role": role,
        "session_type": sess.session_type,        # "user" or "sa"
        "state": state,                            # active / away / idle / expired / revoked
        "ip_address": sess.ip_address,
        "ip_class": _sp.classify_ip(sess.ip_address or ""),
        "ua_summary": _parse_ua(sess.user_agent or ""),
        "ua_raw": sess.user_agent,
        "elevated": elevated,
        "elevated_until": sess.elevated_until.isoformat() if sess.elevated_until else None,
        "created_at": sess.created_at.isoformat(),
        "expires_at": sess.expires_at.isoformat(),
        "last_activity_at": sess.last_activity_at.isoformat() if sess.last_activity_at else None,
        "last_age_seconds": last_age_s,
        "revoked_at": sess.revoked_at.isoformat() if sess.revoked_at else None,
        "revoked_by": sess.revoked_by,
        "revoke_reason": sess.revoke_reason,
    }


def _build_sessions_payload(db, request, include_revoked_days: int = 7) -> dict:
    """VF-341: master-detail data — active sessions + recent-revoked
    (forensic window). Calls sweep_stale_sessions first per §4.6 lazy-on-read.

    Returns {sessions: [...], current_session_id, current_sa_session_id} so
    the UI can hide revoke buttons on the operator's own current rows
    (defense-in-depth — server also refuses self-revoke).
    """
    from app.api.v2 import session_policy as _sp
    from app.models.session import UserSession
    from app.models.user import User
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    _sp.sweep_stale_sessions(db)  # §4.6 lazy
    # Active OR recently-revoked (forensic window).
    revoked_since = now - timedelta(days=include_revoked_days)
    rows = (
        db.query(UserSession)
        .filter(
            (UserSession.expires_at > now) |
            (UserSession.revoked_at >= revoked_since)
        )
        .order_by(UserSession.created_at.desc())
        .limit(200)
        .all()
    )
    user_ids = {s.user_id for s in rows if s.user_id}
    users = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()}
    payload = [_serialize_session(s, users.get(s.user_id), now) for s in rows]
    return {
        "sessions": payload,
        "current_session_id": request.cookies.get("vf_session"),
        "current_sa_session_id": request.cookies.get("vf_sa_session"),
        "now": now.isoformat(),
    }


@router.get("/admin/portal/administration/sessions", response_class=HTMLResponse)
def portal_admin_sessions(request: Request, db: Session = Depends(get_db)):
    """VF-341 §5: graduated to master-detail workspace. Data fetched via
    /admin/portal/api/sessions/list once the page lands."""
    sa = _require_sa_or_login(request, db)
    if not sa: return RedirectResponse(url="/admin/login", status_code=302)
    return templates.TemplateResponse("ui/admin_portal_sessions.html",
        _portal_context(request, db, "adm-sessions", {}))


@router.get("/admin/portal/api/sessions/list")
def portal_api_sessions_list(request: Request, db: Session = Depends(get_db)):
    """VF-341: JSON list for the Sessions workspace XHR loader."""
    sa = _require_sa_or_login(request, db)
    if not sa:
        raise HTTPException(status_code=401)
    return _build_sessions_payload(db, request)


@router.post("/admin/portal/api/sessions/{session_id}/revoke")
def portal_api_session_revoke(session_id: str, request: Request, db: Session = Depends(get_db)):
    """VF-341 §5.6: soft-revoke a session row. Server-side guard refuses
    self-revoke (target == current vf_session OR vf_sa_session) — defense in
    depth alongside UI omission. Sets revoked_at + revoked_by + revoke_reason.
    """
    sa = _require_sa_or_login(request, db)
    if not sa:
        raise HTTPException(status_code=401)
    from app.models.session import UserSession
    sess = db.query(UserSession).filter(UserSession.id == session_id).first()
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found.")
    # Self-revoke guard (per §5.6).
    own_board = request.cookies.get("vf_session")
    own_sa = request.cookies.get("vf_sa_session")
    if session_id in (own_board, own_sa):
        raise HTTPException(status_code=422, detail="Cannot revoke your own current session. Log out instead.")
    if sess.revoked_at is not None:
        raise HTTPException(status_code=422, detail="Session is already revoked.")
    now = datetime.now(timezone.utc)
    sess.revoked_at = now
    sess.revoked_by = sa.id
    sess.revoke_reason = "operator"
    from app.models.activity import ActivityEvent as _AE
    db.add(_AE(
        actor_type="human", actor_user_id=sa.id,
        action="session_revoked",
        details=f"session_id={session_id} target_user_id={sess.user_id} session_type={sess.session_type}",
    ))
    db.commit()
    return {"ok": True, "revoked_at": now.isoformat(), "revoked_by": sa.id}


@router.get("/admin/portal/configuration/session-policy", response_class=HTMLResponse)
def portal_session_policy_page(request: Request, db: Session = Depends(get_db)):
    """VF-341 §5.2: Session Policy form workspace (the 6 knobs)."""
    sa = _require_sa_or_login(request, db)
    if not sa: return RedirectResponse(url="/admin/login", status_code=302)
    return templates.TemplateResponse("ui/admin_portal_session_policy.html",
        _portal_context(request, db, "cfg-session", {}))


@router.get("/admin/portal/api/policy")
def portal_api_policy_get(request: Request, db: Session = Depends(get_db)):
    """VF-341: read the 6 policy knobs + their bounds for the form."""
    sa = _require_sa_or_login(request, db)
    if not sa:
        raise HTTPException(status_code=401)
    from app.api.v2 import session_policy as _sp
    return {
        "values": {
            "slide_window_hours": _sp.slide_window_hours(db),
            "absolute_cap_days": _sp.absolute_cap_days(db),
            "elevation_ttl_minutes": _sp.elevation_ttl_minutes(db),
            "concurrent_cap": _sp.concurrent_cap(db),
            "token_ttl_days": _sp.token_ttl_days(db),
            "auto_revoke_stale_days": _sp.auto_revoke_stale_days(db),
        },
        "bounds": {
            "slide_window_hours": [_sp.SLIDE_WINDOW_HOURS_MIN, _sp.SLIDE_WINDOW_HOURS_MAX],
            "absolute_cap_days": [_sp.ABSOLUTE_CAP_DAYS_MIN, _sp.ABSOLUTE_CAP_DAYS_MAX],
            "elevation_ttl_minutes": [_sp.ELEVATION_TTL_MINUTES_MIN, _sp.ELEVATION_TTL_MINUTES_MAX],
            "concurrent_cap": [_sp.CONCURRENT_CAP_MIN, _sp.CONCURRENT_CAP_MAX],
            "token_ttl_days": [_sp.TOKEN_TTL_DAYS_MIN, _sp.TOKEN_TTL_DAYS_MAX],
            "auto_revoke_stale_days": [_sp.AUTO_REVOKE_STALE_DAYS_MIN, _sp.AUTO_REVOKE_STALE_DAYS_MAX],
        },
        "defaults": {
            "slide_window_hours": _sp.SLIDE_WINDOW_HOURS_DEFAULT,
            "absolute_cap_days": _sp.ABSOLUTE_CAP_DAYS_DEFAULT,
            "elevation_ttl_minutes": _sp.ELEVATION_TTL_MINUTES_DEFAULT,
            "concurrent_cap": _sp.CONCURRENT_CAP_DEFAULT,
            "token_ttl_days": _sp.TOKEN_TTL_DAYS_DEFAULT,
            "auto_revoke_stale_days": _sp.AUTO_REVOKE_STALE_DAYS_DEFAULT,
        },
    }


class PolicyBody(BaseModel):
    """VF-341 §6: 6 knob form payload. All ints; bound-checked server-side
    against session_policy module constants (UI also validates but server
    is the truth)."""
    slide_window_hours: int
    absolute_cap_days: int
    elevation_ttl_minutes: int
    concurrent_cap: int
    token_ttl_days: int
    auto_revoke_stale_days: int


@router.post("/admin/portal/api/policy")
def portal_api_policy_set(body: PolicyBody, request: Request, db: Session = Depends(get_db)):
    """VF-341: write the 6 policy knobs. Validates against bounds + the
    slide<=cap constraint per §3 "constraint: slide ≤ cap".
    """
    sa = _require_sa_or_login(request, db)
    if not sa:
        raise HTTPException(status_code=401)
    from app.api.v2 import session_policy as _sp
    from app.api.v2.admin_experimental import set_setting
    keys_bounds_settings = [
        ("slide_window_hours", _sp.SLIDE_WINDOW_HOURS_MIN, _sp.SLIDE_WINDOW_HOURS_MAX, _sp.KEY_SLIDE_WINDOW_HOURS),
        ("absolute_cap_days", _sp.ABSOLUTE_CAP_DAYS_MIN, _sp.ABSOLUTE_CAP_DAYS_MAX, _sp.KEY_ABSOLUTE_CAP_DAYS),
        ("elevation_ttl_minutes", _sp.ELEVATION_TTL_MINUTES_MIN, _sp.ELEVATION_TTL_MINUTES_MAX, _sp.KEY_ELEVATION_TTL_MINUTES),
        ("concurrent_cap", _sp.CONCURRENT_CAP_MIN, _sp.CONCURRENT_CAP_MAX, _sp.KEY_CONCURRENT_CAP),
        ("token_ttl_days", _sp.TOKEN_TTL_DAYS_MIN, _sp.TOKEN_TTL_DAYS_MAX, _sp.KEY_TOKEN_TTL_DAYS),
        ("auto_revoke_stale_days", _sp.AUTO_REVOKE_STALE_DAYS_MIN, _sp.AUTO_REVOKE_STALE_DAYS_MAX, _sp.KEY_AUTO_REVOKE_STALE_DAYS),
    ]
    parsed = {}
    for k, lo, hi, _ in keys_bounds_settings:
        v = getattr(body, k)
        if v < lo or v > hi:
            raise HTTPException(status_code=422, detail=f"{k} out of bounds [{lo}, {hi}]: {v}")
        parsed[k] = v
    # Constraint: slide_window_hours <= absolute_cap_days * 24 (per §3 row "slide ≤ cap")
    if parsed["slide_window_hours"] > parsed["absolute_cap_days"] * 24:
        raise HTTPException(
            status_code=422,
            detail=f"slide_window_hours ({parsed['slide_window_hours']}h) cannot exceed absolute_cap_days ({parsed['absolute_cap_days']}d = {parsed['absolute_cap_days']*24}h)",
        )
    # All-or-nothing write — if any key fails validation we already raised above.
    for k, _, _, key_name in keys_bounds_settings:
        set_setting(db, key_name, str(parsed[k]), sa.id)
    return {"ok": True, "values": parsed}


@router.get("/admin/portal/administration/agent-telemetry-and-drift", response_class=HTMLResponse)
def portal_admin_agent_telemetry(request: Request, db: Session = Depends(get_db)):
    """VF-306 (bare-minimum cut): Agent telemetry & Drift workspace.

    Migrated surface from the legacy /admin/experimental/drift page (which
    now 301s here — see admin_experimental.experimental_drift_page).
    The XHR endpoints (/admin/api/experimental/drift/*) are unchanged and
    served from admin_experimental.py — only the HTML rendering moves.

    Adds three new sections per VF-306:
      1) Per-agent API call counter (cumulative since last token cycle)
      2) Drift-window slider (live tunable look-back window)
      3) Per-agent drift-gate eval pass/fail tally

    Deferred-scope detail in 0-MD/proposed/VF-306-AGENT-TELEMETRY-AND-DRIFT-PROPOSAL.md
    and on VF-306 ticket notes (blocked under VF-337 POST RC/1.0 umbrella).
    """
    sa = _require_sa_or_login(request, db)
    if not sa: return RedirectResponse(url="/admin/login", status_code=302)
    return templates.TemplateResponse("ui/admin_portal_agent_telemetry.html",
        _portal_context(request, db, "adm-agent-telemetry", {}))


@router.get("/admin/portal/health/overview", response_class=HTMLResponse)
def portal_health_overview(request: Request, db: Session = Depends(get_db)):
    """SA-side System Health overview — portal-native tiles fed by
    /admin/portal/api/health/* (which share data helpers with the Board's
    /ui/api/health/* but auth on the SA cookie plane). The earlier iframe
    approach was wrong: vf_sa_session is path=/admin/ (identity-roles.md §3),
    so it cannot authenticate /ui/* requests inside an iframe."""
    sa = _require_sa_or_login(request, db)
    if not sa: return RedirectResponse(url="/admin/login", status_code=302)
    return templates.TemplateResponse("ui/admin_portal_health_overview.html",
        _portal_context(request, db, "hlt-overview"))


# ═══════════════════════════════════════════════════════════════════════════
# /admin/portal/api/health/* — SA-gated JSON surface mirroring /ui/api/health/*
#
# WHY a parallel surface: vf_sa_session is path=/admin/ per identity-roles.md
# §3. JS fetches from a portal page MUST land under /admin/ for the cookie to
# attach. The data fns live in app.api.v2.ui (single source); these wrappers
# only carry the SA gate.
# ═══════════════════════════════════════════════════════════════════════════

def _portal_api_gate(request: Request, db: Session):
    """SA-gate for /admin/portal/api/* JSON. Returns 403 (not redirect) so
    the caller's fetch() surfaces the failure instead of returning HTML."""
    sa = _require_sa_or_login(request, db)
    if not sa:
        raise HTTPException(status_code=403, detail="SA elevation required.")


@router.get("/admin/portal/api/health/board-activity")
def portal_api_health_board_activity(request: Request, db: Session = Depends(get_db)):
    _portal_api_gate(request, db)
    from app.api.v2.ui import _collect_board_activity
    return _collect_board_activity(db)


@router.get("/admin/portal/api/health/sessions")
def portal_api_health_sessions(request: Request, db: Session = Depends(get_db)):
    _portal_api_gate(request, db)
    from app.api.v2.ui import _collect_sessions
    return _collect_sessions(db)


@router.get("/admin/portal/api/health/agent-fleet")
def portal_api_health_agent_fleet(request: Request, db: Session = Depends(get_db)):
    _portal_api_gate(request, db)
    from app.api.v2.ui import _collect_agent_fleet
    return _collect_agent_fleet(db)


@router.get("/admin/portal/api/health/auth-stats")
def portal_api_health_auth_stats(request: Request, db: Session = Depends(get_db)):
    _portal_api_gate(request, db)
    from app.api.v2.ui import _collect_auth_stats
    return _collect_auth_stats(db)


@router.get("/admin/portal/api/health/db-stats")
def portal_api_health_db_stats(request: Request, db: Session = Depends(get_db)):
    _portal_api_gate(request, db)
    from app.api.v2.ui import _collect_db_stats
    return _collect_db_stats(db)


# /admin/portal/health/proxy + /admin/portal/health/db were folded into
# the single /admin/portal/health/overview per the "Health = Task Manager"
# thesis (one canonical surface per role). Both their data sources are
# already surfaced on the unified page (Caddy/upstreams/TLS + Postgres).


@router.get("/admin/portal/lifecycle/environment", response_class=HTMLResponse)
def portal_lc_environment(request: Request, db: Session = Depends(get_db)):
    sa = _require_sa_or_login(request, db)
    if not sa: return RedirectResponse(url="/admin/login", status_code=302)

    hostname = os.environ.get("VIBEFORGE_HOSTNAME", "localhost")
    if "-dev." in hostname: tier = "DEV"
    elif "-uat." in hostname: tier = "UAT"
    elif hostname and "." in hostname and "localhost" not in hostname: tier = "PROD"
    else: tier = "LOCAL"

    import sys, platform, time
    env_info = {
        "tier": tier,
        "hostname": hostname,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
    }
    try:
        from sqlalchemy import text
        env_info["alembic_head"] = db.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).scalar()
        env_info["postgres_version"] = db.execute(text("SHOW server_version")).scalar()
    except Exception:
        pass

    # Install time — earliest User.created_at is the best proxy we have today.
    # (Schema doesn't carry a dedicated install_ts; bootstrap creates SA as
    # first user. Fallback: earliest alembic row or current time.)
    try:
        from app.models.user import User
        from sqlalchemy import asc
        first_user = db.query(User).order_by(asc(User.created_at)).first()
        env_info["install_time"] = first_user.created_at.isoformat() if first_user and first_user.created_at else None
    except Exception:
        env_info["install_time"] = None

    # Timezone — seed from the container's OS timezone. Operator will override
    # later via a proper Configuration > Session policy / Timezone sub-page.
    # For v1 this is display-only advisory.
    try:
        local_tz = datetime.now().astimezone().tzinfo
        env_info["os_tz"] = str(local_tz) if local_tz else "UTC"
    except Exception:
        env_info["os_tz"] = "UTC"
    env_info["tz_offset"] = time.strftime("%z")

    # NTP: advisory template. Containers don't control host time — this surfaces
    # what the operator SHOULD configure at the host layer. Geo-inferred from
    # the install wizard is a future hook; default AU for now per PK spec.
    env_info["ntp_country"] = "au"  # TODO: pull from install-wizard geo
    env_info["ntp_template"] = f"{env_info['ntp_country']}.pool.ntp.org"
    env_info["ntp_dst_auto"] = True
    env_info["ntp_host_cmd"] = (
        "timedatectl set-timezone Australia/Sydney && "
        f"timedatectl set-ntp true  # pool: {env_info['ntp_template']}"
    )

    return templates.TemplateResponse("ui/admin_portal_environment.html",
        _portal_context(request, db, "lc-env", {"env_info": env_info}))


# VF-353 — Customer onboard test workspace lived here briefly; moved to
# /ui/test-wizard (SU board surface) because SA can't touch projects and
# the 15-min admin elevation timeout makes testing painful. The route +
# template now live in app/api/v2/ui.py + app/templates/ui/test_wizard.html.
# This admin-portal slot is reserved for the customer-facing wizard if/when
# it ships under Settings post-RC.


@router.get("/admin/placeholder/{sub_id}", response_class=HTMLResponse)
def portal_placeholder(sub_id: str, request: Request, db: Session = Depends(get_db)):
    """Generic padlock renderer for dev subs. Copy is drawn from PLACEHOLDER_COPY."""
    sa = _require_sa_or_login(request, db)
    if not sa: return RedirectResponse(url="/admin/login", status_code=302)
    copy = PLACEHOLDER_COPY.get(sub_id,
        "Shipped later in the roadmap. Placeholder reserved so the navigation shape is stable now.")
    label = _find_label(sub_id)
    return templates.TemplateResponse("ui/admin_portal_placeholder.html",
        _portal_context(request, db, sub_id, {
            "placeholder_label": label,
            "placeholder_copy": copy,
        }))


# ═══════════════════════════════════════════════════════════════════════════
# Redirects — old Board-side mutation surfaces point at their new home.
# Keeps existing bookmarks alive for one release.
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/ui/admin/proxy/change-cert")
def redirect_change_cert():
    """T5 wizard moved to the escalated portal."""
    return RedirectResponse(
        url="/admin/portal/configuration/certificates/change-cert",
        status_code=301,
    )
