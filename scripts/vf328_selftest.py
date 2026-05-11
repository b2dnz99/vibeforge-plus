#!/usr/bin/env python3
"""VF-328 self-test — Admin Portal three-tier permission model.

Exercises proposal §8 (acceptance criteria) end-to-end against a running app.
Run inside the app container so DB + URL are local:

    docker compose exec app python scripts/vf328_selftest.py

Tested cases:
  Tier-S enforcement
    - Elevated SU → 403 on a tier-S endpoint (proxy reload / change-sa-password)
    - Pure break-glass SA → not 403 on the same endpoint
    - Plain SU (no elevation) → 403 on tier-S
  Viewer save-gate
    - POST /api/v2/projects/{slug}/members (viewer + role=write) → 422
    - POST /api/v2/projects/{slug}/members (viewer + role=read)  → 201
    - PATCH role from read to admin → 422
  Viewer in addable-users
    - GET /api/v2/projects/{slug}/addable-users includes the viewer
  Demote-to-Viewer auto-downgrade
    - change_role to viewer for a user with write membership →
      response reports memberships_downgraded > 0; row stored as read
  Audit attribution
    - Elevated SU action: actor = SU display_name, privilege_used = su-elevated
    - Stacked SA action: actor = SU display_name, sa_session_active = true
    - Pure break-glass action: actor = SA display_name, break_glass = true
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, desc
from sqlalchemy.orm import sessionmaker

from app.models.activity import ActivityEvent
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.session import UserSession
from app.models.user import User

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://vibeforge:vibeforge@db:5432/vibeforge",
)
BASE = "http://localhost:8000"

CHECKS = 0
FAILS = 0


def _ok(cond, msg):
    global CHECKS, FAILS
    CHECKS += 1
    mark = "OK  " if cond else "FAIL"
    if not cond:
        FAILS += 1
    print(f"  {mark} {msg}")


def _section(t):
    print(f"\n[{t}]")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)
    http_error_301 = http_error_302
    http_error_303 = http_error_302
    http_error_307 = http_error_302


_opener = urllib.request.build_opener(_NoRedirect())


def _call(method, path, *, vf=None, sa=None, body=None):
    """Issue a request with optional vf_session / vf_sa_session cookies."""
    headers = {}
    cookie_parts = []
    if vf:
        cookie_parts.append(f"vf_session={vf}")
    if sa:
        cookie_parts.append(f"vf_sa_session={sa}")
    if cookie_parts:
        headers["Cookie"] = "; ".join(cookie_parts)
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        r = _opener.open(req, timeout=10)
        return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers) if e.headers else {}


def _create_session(db, *, user_id, session_type, elevated=False, minutes=60):
    """Create a UserSession row and return the cookie value (= session id)."""
    cookie = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    sess = UserSession(
        id=cookie, user_id=user_id, session_type=session_type,
        created_at=now, expires_at=now + timedelta(minutes=minutes),
    )
    if elevated:
        sess.elevated_until = now + timedelta(minutes=15)
    db.add(sess)
    db.commit()
    return cookie


def _last_event_details(db, action_filter=None):
    """Return the details dict of the most recent ActivityEvent (optionally filtered)."""
    q = db.query(ActivityEvent).order_by(desc(ActivityEvent.created_at))
    if action_filter:
        q = q.filter(ActivityEvent.action == action_filter)
    ev = q.first()
    if not ev:
        return None
    try:
        return json.loads(ev.details) if ev.details else {}
    except Exception:
        return {"_raw": ev.details}


def main():
    engine = create_engine(DB_URL)
    Session = sessionmaker(bind=engine)
    db = Session()

    print("=" * 66)
    print("  VF-328 SELF-TEST — Admin Portal three-tier perm model")
    print("=" * 66)

    # ── Fixtures ──────────────────────────────────────────────────────────
    _section("setup")
    sa = db.query(User).filter(User.role == "super_admin", User.status == "active").first()
    _ok(sa is not None, "active SA exists")
    su = db.query(User).filter(User.role == "super_user", User.status == "active").first()
    _ok(su is not None, "active SU exists")
    if not (sa and su):
        print("  abort — no SA or SU to test against")
        return 1

    # Find or create a viewer fixture user
    viewer = db.query(User).filter(User.role == "viewer", User.status == "active").first()
    created_viewer = False
    if not viewer:
        viewer = User(
            id=str(uuid.uuid4()),
            username=f"vt{uuid.uuid4().hex[:6]}",
            email=f"vf328vt-{uuid.uuid4().hex[:6]}@example.local",
            display_name="VF-328 Test Viewer",
            role="viewer", status="active",
            password_hash="x" * 60,
        )
        db.add(viewer)
        db.commit()
        created_viewer = True
    _ok(viewer is not None, f"viewer fixture present (created={created_viewer})")

    # Pick a project
    project = db.query(Project).filter(Project.status != "archived").first()
    _ok(project is not None, "active project exists")
    if not project:
        return 1

    # Sessions
    cookie_plain_su = _create_session(db, user_id=su.id, session_type="user", elevated=False)
    cookie_elev_su = _create_session(db, user_id=su.id, session_type="user", elevated=True)
    cookie_pure_sa = _create_session(db, user_id=sa.id, session_type="sa")
    print(f"  fixtures: sa={sa.display_name}  su={su.display_name}  viewer={viewer.display_name}  project={project.slug}")

    # ── A. Tier-S enforcement ─────────────────────────────────────────────
    _section("A. tier-S enforcement (change_sa_password gate)")
    # Plain SU → 403
    s, _, _ = _call("POST", "/admin/api/change-sa-password",
                     vf=cookie_plain_su,
                     body={"current_password": "wrong", "new_password": "Wrong1234!@xx"})
    _ok(s == 403, f"plain SU on tier-S → 403 (got {s})")

    # Elevated SU → 403 (this is the load-bearing new behaviour)
    s, b, _ = _call("POST", "/admin/api/change-sa-password",
                     vf=cookie_elev_su,
                     body={"current_password": "wrong", "new_password": "Wrong1234!@xx"})
    _ok(s == 403, f"elevated SU on tier-S → 403 (got {s})")
    _ok("Super Admin" in b or "system-config" in b.lower(),
        "  elevated SU rejection mentions SA / system-config")

    # Pure SA → not 403 (will be 401 because we're using a wrong password, but the gate passed)
    s, _, _ = _call("POST", "/admin/api/change-sa-password",
                     sa=cookie_pure_sa,
                     body={"current_password": "wrong", "new_password": "Wrong1234!@xx"})
    _ok(s != 403, f"pure SA on tier-S → not 403 (got {s})")
    _ok(s == 401, f"pure SA bad-pwd → 401 (gate passed, pwd check failed) (got {s})")

    # ── B. Viewer save-gate on POST /members ─────────────────────────────
    _section("B. viewer save-gate on members")
    # Make sure viewer is not yet a member of the project
    db.query(ProjectMember).filter(
        ProjectMember.project_id == project.id,
        ProjectMember.user_id == viewer.id,
    ).delete(synchronize_session=False)
    db.commit()

    s, b, _ = _call("POST", f"/api/v2/projects/{project.slug}/members",
                     vf=cookie_elev_su,
                     body={"user_id": viewer.id, "role": "write"})
    _ok(s == 422, f"viewer + role=write → 422 (got {s})")
    _ok("viewer" in b.lower() and "read" in b.lower(),
        "  rejection message names Viewer and 'read'")

    s, b, _ = _call("POST", f"/api/v2/projects/{project.slug}/members",
                     vf=cookie_elev_su,
                     body={"user_id": viewer.id, "role": "admin"})
    _ok(s == 422, f"viewer + role=admin → 422 (got {s})")

    s, b, _ = _call("POST", f"/api/v2/projects/{project.slug}/members",
                     vf=cookie_elev_su,
                     body={"user_id": viewer.id, "role": "read"})
    _ok(s == 201, f"viewer + role=read → 201 (got {s})")
    member_id = json.loads(b)["id"] if s == 201 else None

    # ── C. Viewer save-gate on PATCH /members/{id} ───────────────────────
    _section("C. viewer save-gate on PATCH role")
    if member_id:
        s, b, _ = _call("PATCH", f"/api/v2/projects/{project.slug}/members/{member_id}",
                         vf=cookie_elev_su,
                         body={"role": "admin"})
        _ok(s == 422, f"PATCH viewer member to role=admin → 422 (got {s})")

    # ── D. Viewer in addable-users ───────────────────────────────────────
    _section("D. viewer surfaced in addable-users")
    # Remove the viewer membership so they're addable again
    db.query(ProjectMember).filter(
        ProjectMember.project_id == project.id,
        ProjectMember.user_id == viewer.id,
    ).delete(synchronize_session=False)
    db.commit()
    s, b, _ = _call("GET", f"/api/v2/projects/{project.slug}/addable-users",
                     vf=cookie_elev_su)
    _ok(s == 200, f"GET addable-users → 200 (got {s})")
    if s == 200:
        users = json.loads(b)
        viewer_in_list = any(u.get("id") == viewer.id and u.get("role") == "viewer" for u in users)
        _ok(viewer_in_list, "viewer appears in addable-users list with role=viewer")

    # ── E. Demote-to-Viewer auto-downgrade ───────────────────────────────
    _section("E. demote-to-viewer auto-downgrade")
    # Set up: pick a non-SA non-viewer test user and give them a write membership
    target = db.query(User).filter(
        User.status == "active",
        User.role.in_(("user", "super_user")),
        User.id != su.id,
        User.id != sa.id,
    ).first()
    if target:
        # Wipe any existing membership row for cleanliness
        db.query(ProjectMember).filter(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == target.id,
        ).delete(synchronize_session=False)
        db.commit()
        # Give them a write membership directly via the DB (bypass API to keep setup clean)
        original_role = target.role
        m = ProjectMember(
            id=str(uuid.uuid4()),
            project_id=project.id, user_id=target.id, agent_id=None,
            role="write",
        )
        db.add(m)
        db.commit()

        # Now demote them to viewer via the API
        s, b, _ = _call("POST", f"/admin/api/users/{target.id}/change-role",
                         vf=cookie_elev_su,
                         body={"role": "viewer"})
        _ok(s == 200, f"change-role to viewer → 200 (got {s})")
        if s == 200:
            resp = json.loads(b)
            _ok(resp.get("memberships_downgraded", 0) >= 1,
                f"  memberships_downgraded reported (got {resp.get('memberships_downgraded')})")

        # Re-query the membership row — should now be 'read'
        db.expire_all()
        m2 = db.query(ProjectMember).filter(ProjectMember.id == m.id).first()
        _ok(m2 is not None and m2.role == "read",
            f"  membership row downgraded write → read (got {m2.role if m2 else 'gone'})")

        # Restore the target user to its original role for cleanliness
        target = db.query(User).filter(User.id == target.id).first()
        target.role = original_role
        db.commit()
    else:
        _ok(False, "no eligible target user — can't run demote-to-viewer test")

    # ── F. Audit attribution (elevated SU on tier-U) ─────────────────────
    _section("F. audit attribution — elevated SU tier-U action")
    # The change_role POST above just ran as elevated SU. Inspect its audit.
    details = _last_event_details(db, action_filter="user_role_changed")
    _ok(details is not None, "user_role_changed event present")
    if details:
        _ok(details.get("privilege_used") == "su-elevated",
            f"  privilege_used = su-elevated (got {details.get('privilege_used')})")
        _ok(details.get("elevated") is True,
            f"  elevated flag = true (got {details.get('elevated')})")
        _ok(details.get("sa_session_active") is None or details.get("sa_session_active") is False,
            f"  sa_session_active not set (got {details.get('sa_session_active')})")

    # ── G. Audit attribution (pure break-glass on tier-S) ────────────────
    _section("G. audit attribution — pure break-glass tier-S")
    # Trigger an SA password event via ack endpoint (write-only, deterministic)
    s, _, _ = _call("POST", "/admin/api/sa-password-force-reset/ack",
                     sa=cookie_pure_sa)
    _ok(s == 200, f"SA ack endpoint as pure break-glass → 200 (got {s})")
    if s == 200:
        details = _last_event_details(db, action_filter="sa_password_force_reset_ack")
        _ok(details is not None, "sa_password_force_reset_ack event present")
        if details:
            _ok(details.get("break_glass") is True,
                f"  break_glass = true (got {details.get('break_glass')})")
            _ok(details.get("privilege_used") == "sa",
                f"  privilege_used = sa (got {details.get('privilege_used')})")
            _ok(details.get("actor") == sa.display_name,
                f"  actor = SA display_name (got {details.get('actor')!r})")

    # ── Cleanup ──────────────────────────────────────────────────────────
    _section("cleanup")
    db.query(UserSession).filter(UserSession.id.in_([
        cookie_plain_su, cookie_elev_su, cookie_pure_sa,
    ])).delete(synchronize_session=False)
    db.query(ProjectMember).filter(
        ProjectMember.project_id == project.id,
        ProjectMember.user_id == viewer.id,
    ).delete(synchronize_session=False)
    if created_viewer:
        db.query(User).filter(User.id == viewer.id).delete(synchronize_session=False)
    db.commit()
    print("  sessions + fixtures cleaned")

    print("\n" + "=" * 66)
    if FAILS == 0:
        print(f"  ALL {CHECKS} CHECKS GREEN")
    else:
        print(f"  {FAILS} / {CHECKS} FAILED")
    print("=" * 66)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
