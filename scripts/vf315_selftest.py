#!/usr/bin/env python3
"""
VF-307 tweak + VF-315 self-test.

Runs INSIDE the app container. Exercises:
  A. VF-307 tweak — admin /agents/new?user_id=X scopes creator to that user; POST
     with target_user_id sets Agent.created_by accordingly; admin.html has per-user
     `+ Agent` action.
  B. VF-315 — _user_can_manage_agent now rejects PO + admin-member; cascade revoke
     on user soft-delete; existing cascade on member-removal still fires.

Creates transient SA session + a test user + a test agent in a temp project, exercises
the paths, and cleans up all test rows on exit (rollback + targeted DELETE).

    docker compose exec app python scripts/vf315_selftest.py
"""
import os, sys, json, uuid, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from app.models.user import User
from app.models.session import UserSession
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.agent import Agent
from app.models.activity import ActivityEvent
import bcrypt as _bcrypt

DB_URL = os.environ.get("DATABASE_URL",
                        "postgresql+psycopg2://vibeforge:vibeforge@db:5432/vibeforge")
BASE = "http://localhost:8000"


def _call(path, cookie=None, method="GET", body=None, cookie_name="vf_sa_session"):
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = f"{cookie_name}={cookie}"
    req = urllib.request.Request(BASE + path, method=method, headers=headers,
                                 data=(json.dumps(body).encode() if body is not None else None))
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _ok(c, m):
    print(("  OK   " if c else "  FAIL ") + m)
    if not c:
        raise AssertionError(m)


def main():
    engine = create_engine(DB_URL)
    Session = sessionmaker(bind=engine)
    db = Session()

    print("=" * 66)
    print("  VF-307 tweak + VF-315 self-test — per-user agent + RBAC tighten + cascade")
    print("=" * 66)

    sa = db.query(User).filter(User.role == "super_admin", User.status == "active").first()
    if not sa:
        print("  FAIL: no active SA user found.")
        return 2

    test_tag = uuid.uuid4().hex[:4]  # username column is varchar(10); keep it short
    sa_session_id = str(uuid.uuid4())
    # Transient SA session
    db.add(UserSession(
        id=sa_session_id, user_id=sa.id, session_type="sa",
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
    ))

    # Create two test users: alice (user) and bob (user), a test project, memberships
    pw_hash = _bcrypt.hashpw(b"xxxxxx", _bcrypt.gensalt()).decode()
    alice = User(id=str(uuid.uuid4()), username=f"ali{test_tag}",
                 display_name="VF315 Alice", email=f"alice_{test_tag}@vf315.local",
                 role="user", status="active",
                 password_hash=pw_hash, must_change_password=False)
    bob = User(id=str(uuid.uuid4()), username=f"bob{test_tag}",
               display_name="VF315 Bob", email=f"bob_{test_tag}@vf315.local",
               role="user", status="active",
               password_hash=pw_hash, must_change_password=False)
    db.add_all([alice, bob])
    db.commit()  # FK-safe: users must exist before memberships reference them

    proj = Project(id=str(uuid.uuid4()), slug=f"vf315-{test_tag}",
                   name=f"VF315 Test {test_tag}", status="active",
                   owner_id=alice.id, agentic_dev=False)
    db.add(proj)
    db.commit()  # project must exist before memberships reference it

    db.add(ProjectMember(id=str(uuid.uuid4()), project_id=proj.id,
                         user_id=alice.id, role="admin"))
    db.add(ProjectMember(id=str(uuid.uuid4()), project_id=proj.id,
                         user_id=bob.id, role="write"))
    db.commit()

    try:
        # === A. VF-307 tweak — admin /agents/new with ?user_id=X ===
        print("\n[A] VF-307 — admin /agents/new?user_id=<alice.id> pre-scopes owner")
        code, body = _call(f"/admin/agents/new?user_id={alice.id}", sa_session_id)
        _ok(code == 200, f"HTTP {code}")
        _ok(alice.display_name in body, "target user chip rendered with alice's name")
        _ok(f'value="{alice.id}"' in body, "hidden target_user_id input present")
        _ok('owner-chip' in body, "owner-chip styling class present")

        # Unknown user_id falls back to picker
        print("\n[B] Unknown user_id — falls back to picker")
        code, body = _call("/admin/agents/new?user_id=nosuch", sa_session_id)
        _ok(code == 200, f"HTTP {code}")
        _ok('— select owner —' in body, "fallback picker shown for unknown user")
        _ok(alice.display_name in body, "alice listed in eligible_users dropdown")

        # POST with target_user_id → agent created with created_by = alice
        print("\n[C] POST /admin/api/agents with target_user_id=alice")
        agent_name = f"vf315agent-{test_tag}"
        code, body = _call("/admin/api/agents", sa_session_id, method="POST", body={
            "name": agent_name, "project_slug": proj.slug,
            "model_type": "claude", "description": "test agent",
            "target_user_id": alice.id,
        })
        _ok(code == 201, f"HTTP {code} body={body[:200]}")
        resp = json.loads(body)
        agent_id = resp["id"]
        db.expire_all()
        ag = db.query(Agent).filter(Agent.id == agent_id).first()
        _ok(ag is not None, "agent row created")
        _ok(ag.created_by == alice.id, f"created_by = alice (got {ag.created_by!r})")

        # POST with target_user_id = SA-role (ineligible) → 422
        print("\n[D] POST with target_user_id = SA → 422 (not eligible to own agent)")
        code, body = _call("/admin/api/agents", sa_session_id, method="POST", body={
            "name": f"vf315sa-{test_tag}", "project_slug": proj.slug,
            "model_type": "claude", "description": "should fail",
            "target_user_id": sa.id,
        })
        _ok(code == 422, f"HTTP {code}")

        # === E. VF-315 — _user_can_manage_agent tightened ===
        # Alice owns her agent. Bob is a write-role member on same project. Alice is also
        # ProjectMember.role='admin'. Neither Bob nor Alice (as PO/admin-member) should be
        # able to revoke a DIFFERENT user's agent. Create bob's agent, then try variations.
        print("\n[E] VF-315 — RBAC: only creator + SU/SA can revoke")
        # Make bob an agent (api + target_user_id)
        code, body = _call("/admin/api/agents", sa_session_id, method="POST", body={
            "name": f"vf315bobag-{test_tag}", "project_slug": proj.slug,
            "model_type": "claude", "description": "bob's agent",
            "target_user_id": bob.id,
        })
        _ok(code == 201, f"create bob's agent → HTTP {code}")
        bob_agent = json.loads(body)
        bob_agent_id = bob_agent["id"]

        # Alice logs in (board session) and tries to revoke bob's agent
        alice_sess_id = str(uuid.uuid4())
        db.add(UserSession(id=alice_sess_id, user_id=alice.id, session_type="user",
                           created_at=datetime.now(timezone.utc),
                           expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
        db.commit()

        code, body = _call(f"/ui/api/agents/{bob_agent_id}/revoke",
                           alice_sess_id, method="POST", cookie_name="vf_session")
        # Alice is PO + admin-member but NOT creator + NOT SU/SA → 403 after VF-315
        _ok(code == 403, f"alice revoking bob's agent → HTTP {code} (expected 403)")

        # Alice revoking HER OWN agent should still work
        code, body = _call(f"/ui/api/agents/{agent_id}/revoke",
                           alice_sess_id, method="POST", cookie_name="vf_session")
        _ok(code == 200, f"alice revoking own agent → HTTP {code}")
        db.expire_all()
        ag = db.query(Agent).filter(Agent.id == agent_id).first()
        _ok(ag.status == "revoked", "alice's agent now revoked")

        # === F. Cascade on member removal (existing — verify still fires) ===
        print("\n[F] Cascade on member-removal — bob removed from project, bob's agent revoked")
        # Need to find bob's membership id
        bob_pm = db.query(ProjectMember).filter(
            ProjectMember.project_id == proj.id,
            ProjectMember.user_id == bob.id,
        ).first()
        # Use alice's session (alice is PO + admin on project — can remove members)
        code, body = _call(f"/api/v2/projects/{proj.slug}/members/{bob_pm.id}",
                           alice_sess_id, method="DELETE", cookie_name="vf_session")
        _ok(code == 200, f"alice removes bob → HTTP {code}")
        db.expire_all()
        bag = db.query(Agent).filter(Agent.id == bob_agent_id).first()
        _ok(bag.status == "revoked", "bob's agent cascade-revoked by member removal")

        # === G. Cascade on user soft-delete (NEW — VF-315) ===
        print("\n[G] Cascade on user soft-delete — all bob's active agents across ALL projects")
        # Create a fresh agent for bob via admin API (bypasses the now-revoked ones)
        code, body = _call("/admin/api/agents", sa_session_id, method="POST", body={
            "name": f"vf315bob2-{test_tag}", "project_slug": proj.slug,
            "model_type": "claude", "description": "bob's second",
            "target_user_id": bob.id,
        })
        _ok(code == 201, f"re-create agent for bob → HTTP {code}")
        bob_ag2 = json.loads(body)["id"]
        # Now soft-delete bob (SA-only endpoint)
        code, body = _call(f"/admin/api/users/{bob.id}/soft-delete",
                           sa_session_id, method="POST", body={})
        _ok(code == 200, f"soft-delete bob → HTTP {code}")
        resp = json.loads(body)
        _ok(resp.get("agents_cascade_revoked") == 1,
            f"response reports 1 cascade-revoked (got {resp.get('agents_cascade_revoked')})")
        db.expire_all()
        ag2 = db.query(Agent).filter(Agent.id == bob_ag2).first()
        _ok(ag2.status == "revoked", "bob's second agent cascade-revoked on user delete")
        _ok(ag2.api_token_hash is None, "token hash nulled")
        # Audit event with reason cascade_user_soft_deleted
        cascaded_event = db.query(ActivityEvent).filter(
            ActivityEvent.action == "agent_revoked",
            ActivityEvent.details.contains("cascade_user_soft_deleted"),
            ActivityEvent.details.contains(bob.id),
        ).first()
        _ok(cascaded_event is not None, "audit event for cascade exists with reason tag")

        print("\n" + "=" * 66)
        print("  ALL CHECKS GREEN")
        print("=" * 66)
        return 0

    finally:
        # Cleanup — delete in FK-safe order
        db.rollback()
        # Delete activity events for the test project
        db.execute(text("DELETE FROM activity_events WHERE project_id = :pid"), {"pid": proj.id})
        # Delete project members for the test project
        db.execute(text("DELETE FROM project_members WHERE project_id = :pid"), {"pid": proj.id})
        # Delete agents for the test project
        db.execute(text("DELETE FROM agents WHERE project_id = :pid"), {"pid": proj.id})
        # Delete sessions for test users
        db.execute(text("DELETE FROM sessions WHERE user_id IN (:a, :b) OR id IN (:s1, :s2)"),
                   {"a": alice.id, "b": bob.id, "s1": sa_session_id, "s2": alice_sess_id})
        # Delete the project
        db.execute(text("DELETE FROM projects WHERE id = :pid"), {"pid": proj.id})
        # Delete the test users
        db.execute(text("DELETE FROM users WHERE id IN (:a, :b)"),
                   {"a": alice.id, "b": bob.id})
        db.commit()
        db.close()


if __name__ == "__main__":
    sys.exit(main())
