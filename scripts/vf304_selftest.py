#!/usr/bin/env python3
"""
VF-304 self-test — blocked_by mutation rules + relationships endpoint.

Runs INSIDE the app container. Creates a transient project + 4 tasks,
exercises: set without reason (422), set with cycle (422), set valid (200),
change with reason (200), clear without reason (422), clear with reason (200),
GET /relationships (returns blocked_by enriched + blocks reverse list).
Cleans up all test rows on exit.

    docker compose exec app python scripts/vf304_selftest.py
"""
import os, sys, json, uuid, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from app.models.user import User
from app.models.project import Project
from app.models.task import Task
from app.models.session import UserSession
from app.models.activity import ActivityEvent

DB_URL = os.environ.get("DATABASE_URL",
                        "postgresql+psycopg2://vibeforge:vibeforge@db:5432/vibeforge")
BASE = "http://localhost:8000"

_cookie = None


def _call(path, method="GET", body=None):
    headers = {"Content-Type": "application/json"}
    if _cookie:
        headers["Cookie"] = f"vf_session={_cookie}"
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
    global _cookie
    engine = create_engine(DB_URL)
    Session = sessionmaker(bind=engine)
    db = Session()

    print("=" * 66)
    print("  VF-304 SELF-TEST — blocked_by mutation rules + relationships")
    print("=" * 66)

    # Borrow an SU session for auth on all calls
    su = db.query(User).filter(User.role == "super_user", User.status == "active").first()
    _ok(su is not None, "active SU exists")
    _cookie = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    db.add(UserSession(id=_cookie, user_id=su.id, session_type="user",
                       created_at=now, expires_at=now + timedelta(hours=1)))
    db.commit()

    # Transient project
    tag = uuid.uuid4().hex[:4]
    proj_slug = f"vf304-{tag}"
    proj_id = str(uuid.uuid4())
    from sqlalchemy import func
    max_pn = db.query(func.max(Project.project_number)).scalar() or 0
    pn = max_pn + 1000
    proj = Project(id=proj_id, slug=proj_slug, name=f"VF304 {tag}",
                   status="active", prefix=f"V{tag[:2].upper()}",
                   project_number=pn, agentic_dev=False)
    db.add(proj)
    db.commit()

    # 4 tasks — A, B, C, D
    def _mk(num, title):
        t = Task(id=str(uuid.uuid4()), project_id=proj_id,
                 task_number=num, title=title, description="test",
                 status="backlog", priority="medium",
                 created_at=now, updated_at=now)
        db.add(t); db.commit(); return t

    tA = _mk(1001, "Alpha")
    tB = _mk(1002, "Beta")
    tC = _mk(1003, "Gamma")
    tD = _mk(1004, "Delta")

    try:
        # [1] set blocked_by without reason → 422
        print("\n[1] set blocked_by WITHOUT reason → 422")
        code, body = _call(f"/api/v2/tasks/{tA.id}", method="PATCH",
                           body={"blocked_by_task_id": tB.id})
        _ok(code == 422, f"HTTP {code}")
        _ok("BLOCKED_BY_REASON_REQUIRED" in body, "error code present")

        # [2] set blocked_by with <10-char reason → 422
        print("\n[2] set blocked_by with 5-char reason → 422")
        code, body = _call(f"/api/v2/tasks/{tA.id}", method="PATCH",
                           body={"blocked_by_task_id": tB.id, "blocked_by_reason": "short"})
        _ok(code == 422, f"HTTP {code}")
        _ok("BLOCKED_BY_REASON_REQUIRED" in body, "rejected for <10 chars")

        # [3] set blocked_by to self → 422 SELF
        print("\n[3] set blocked_by to SELF → 422 BLOCKED_BY_SELF")
        code, body = _call(f"/api/v2/tasks/{tA.id}", method="PATCH",
                           body={"blocked_by_task_id": tA.id,
                                 "blocked_by_reason": "cannot block self, test case"})
        _ok(code == 422, f"HTTP {code}")
        _ok("BLOCKED_BY_SELF" in body, "self-block rejected")

        # [4] Valid set: A blocked_by B
        print("\n[4] set A blocked_by B WITH reason → 200")
        code, body = _call(f"/api/v2/tasks/{tA.id}", method="PATCH",
                           body={"blocked_by_task_id": tB.id,
                                 "blocked_by_reason": "A depends on B finishing design"})
        _ok(code == 200, f"HTTP {code}")
        db.expire_all()
        tA_reload = db.query(Task).filter(Task.id == tA.id).first()
        _ok(tA_reload.blocked_by_task_id == tB.id, "A.blocked_by == B")

        # Activity event should be blocked_by_set with the reason
        ev = (db.query(ActivityEvent)
              .filter(ActivityEvent.task_id == tA.id,
                      ActivityEvent.action == "blocked_by_set")
              .order_by(ActivityEvent.created_at.desc()).first())
        _ok(ev is not None, "blocked_by_set activity event exists")
        det = json.loads(ev.details)
        _ok(det.get("reason") == "A depends on B finishing design", "reason captured")
        _ok(det.get("to") == tB.id, "to=B in details")
        _ok(det.get("from") is None, "from=None (was unset)")

        # [5] Set B blocked_by C (chain: A→B→C)
        print("\n[5] set B blocked_by C (chain A→B→C)")
        code, body = _call(f"/api/v2/tasks/{tB.id}", method="PATCH",
                           body={"blocked_by_task_id": tC.id,
                                 "blocked_by_reason": "B waits on C for data shape"})
        _ok(code == 200, f"HTTP {code}")

        # [6] Cycle: try to set C blocked_by A (A→B→C→A would cycle)
        print("\n[6] set C blocked_by A → 422 BLOCKED_BY_CYCLE")
        code, body = _call(f"/api/v2/tasks/{tC.id}", method="PATCH",
                           body={"blocked_by_task_id": tA.id,
                                 "blocked_by_reason": "intentional cycle attempt, should reject"})
        _ok(code == 422, f"HTTP {code}")
        _ok("BLOCKED_BY_CYCLE" in body, "cycle rejected")

        # [7] Non-cyclic change: A's blocker changes from B to D
        print("\n[7] change A's blocker B→D → 200, action=blocked_by_changed")
        code, body = _call(f"/api/v2/tasks/{tA.id}", method="PATCH",
                           body={"blocked_by_task_id": tD.id,
                                 "blocked_by_reason": "pivot: D became the real blocker"})
        _ok(code == 200, f"HTTP {code}")
        ev = (db.query(ActivityEvent)
              .filter(ActivityEvent.task_id == tA.id,
                      ActivityEvent.action == "blocked_by_changed")
              .order_by(ActivityEvent.created_at.desc()).first())
        _ok(ev is not None, "blocked_by_changed event exists")
        det = json.loads(ev.details)
        _ok(det.get("from") == tB.id and det.get("to") == tD.id, "from=B, to=D")

        # [8] Clear without reason → 422
        print("\n[8] clear A's blocker WITHOUT reason → 422")
        code, body = _call(f"/api/v2/tasks/{tA.id}", method="PATCH",
                           body={"blocked_by_task_id": ""})
        _ok(code == 422, f"HTTP {code}")

        # [9] Clear with reason → 200, action=blocked_by_cleared
        print("\n[9] clear A's blocker WITH reason → 200")
        code, body = _call(f"/api/v2/tasks/{tA.id}", method="PATCH",
                           body={"blocked_by_task_id": "",
                                 "blocked_by_reason": "Dependency resolved; A can proceed now"})
        _ok(code == 200, f"HTTP {code}")
        db.expire_all()
        tA_reload = db.query(Task).filter(Task.id == tA.id).first()
        _ok(tA_reload.blocked_by_task_id is None, "A.blocked_by cleared to NULL")
        ev = (db.query(ActivityEvent)
              .filter(ActivityEvent.task_id == tA.id,
                      ActivityEvent.action == "blocked_by_cleared")
              .order_by(ActivityEvent.created_at.desc()).first())
        _ok(ev is not None, "blocked_by_cleared event exists")

        # [10] /relationships endpoint — B has blocker C, and is blocked by A is no longer (A cleared)
        # Re-set A→B to populate relationships for the test
        code, _ = _call(f"/api/v2/tasks/{tA.id}", method="PATCH",
                        body={"blocked_by_task_id": tB.id,
                              "blocked_by_reason": "re-set A on B for relationships test"})
        _ok(code == 200, "reset A→B")

        print("\n[10] GET /api/v2/tasks/{B.id}/relationships")
        code, body = _call(f"/api/v2/tasks/{tB.id}/relationships")
        _ok(code == 200, f"HTTP {code}")
        d = json.loads(body)
        _ok(d.get("blocked_by") is not None, "B has a blocker (C)")
        _ok(d["blocked_by"]["id"] == tC.id, f"blocker is C (got {d['blocked_by']['id']})")
        _ok(d["blocked_by"].get("reason") is not None, "reason carried in blocked_by view")
        _ok(isinstance(d.get("blocks"), list), "blocks is a list")
        _ok(any(t["id"] == tA.id for t in d["blocks"]), "A is in B's blocks-reverse list")

        # [11] Relationships endpoint on task with no blocker and no blocks — C has no upstream, blocks B
        print("\n[11] GET /api/v2/tasks/{C.id}/relationships — C blocks B, no upstream")
        code, body = _call(f"/api/v2/tasks/{tC.id}/relationships")
        _ok(code == 200, f"HTTP {code}")
        d = json.loads(body)
        _ok(d.get("blocked_by") is None, "C has no blocker")
        _ok(any(t["id"] == tB.id for t in d.get("blocks", [])), "B is in C's blocks-reverse list")

        # [12] 404 for nonexistent task
        print("\n[12] GET /relationships for nonexistent task → 404")
        code, body = _call(f"/api/v2/tasks/nonexistent-id/relationships")
        _ok(code == 404, f"HTTP {code}")

        # === VF-304 expanded scope: reverse-blocks + related CRUD ===

        # [13] Reverse-blocks: set B.blocked_by = A via POST /tasks/B/blocks from A's perspective
        # First clear B's existing blocker so it's free
        code, _ = _call(f"/api/v2/tasks/{tB.id}", method="PATCH",
                        body={"blocked_by_task_id": "",
                              "blocked_by_reason": "clearing B's blocker to test reverse-blocks"})
        _ok(code == 200, "cleared B's blocker")

        print("\n[13] POST /tasks/{D.id}/blocks with target=B → 200")
        code, body = _call(f"/api/v2/tasks/{tD.id}/blocks", method="POST",
                           body={"target_task_id": tB.id,
                                 "reason": "D blocks B per design dep"})
        _ok(code == 200, f"HTTP {code}")
        db.expire_all()
        tB_reload = db.query(Task).filter(Task.id == tB.id).first()
        _ok(tB_reload.blocked_by_task_id == tD.id, f"B.blocked_by == D (got {tB_reload.blocked_by_task_id})")

        # [14] Reverse-blocks when target already has a blocker → 422 BLOCKED_BY_TARGET_HAS_BLOCKER
        print("\n[14] POST /tasks/{A.id}/blocks with target=B (B already blocked) → 422")
        code, body = _call(f"/api/v2/tasks/{tA.id}/blocks", method="POST",
                           body={"target_task_id": tB.id,
                                 "reason": "conflict test: B already has D as blocker"})
        _ok(code == 422, f"HTTP {code}")
        _ok("BLOCKED_BY_TARGET_HAS_BLOCKER" in body, "conflict code present")

        # [15] Add related link A↔C
        print("\n[15] POST /tasks/{A.id}/related with other=C → 201 (created)")
        code, body = _call(f"/api/v2/tasks/{tA.id}/related", method="POST",
                           body={"other_task_id": tC.id,
                                 "reason": "A and C are related in the search discussion"})
        _ok(code == 200, f"HTTP {code}")  # endpoint returns 200 ok:true
        rel_resp = json.loads(body)
        _ok("relationship_id" in rel_resp, "relationship_id returned")
        rel_id = rel_resp["relationship_id"]

        # [16] Related shows up on BOTH tasks' /relationships
        print("\n[16] Related link visible from A and C")
        code, body = _call(f"/api/v2/tasks/{tA.id}/relationships")
        d = json.loads(body)
        _ok(any(r.get("id") == tC.id for r in d.get("related", [])), "C in A's related")
        code, body = _call(f"/api/v2/tasks/{tC.id}/relationships")
        d = json.loads(body)
        _ok(any(r.get("id") == tA.id for r in d.get("related", [])), "A in C's related")

        # [17] Duplicate related add → 409
        print("\n[17] POST /tasks/{A.id}/related with other=C again → 409")
        code, body = _call(f"/api/v2/tasks/{tA.id}/related", method="POST",
                           body={"other_task_id": tC.id,
                                 "reason": "duplicate attempt should be rejected"})
        _ok(code == 409, f"HTTP {code}")
        _ok("RELATED_ALREADY_LINKED" in body, "duplicate code present")

        # [18] Related add without reason → 422
        print("\n[18] POST /tasks/{A.id}/related without reason → 422")
        code, body = _call(f"/api/v2/tasks/{tA.id}/related", method="POST",
                           body={"other_task_id": tD.id, "reason": "short"})
        _ok(code == 422, f"HTTP {code}")

        # [19] Remove related without reason → 422
        print("\n[19] DELETE related without reason → 422")
        code, body = _call(f"/api/v2/tasks/{tA.id}/related/{rel_id}", method="DELETE",
                           body={"reason": ""})
        _ok(code == 422, f"HTTP {code}")

        # [20] Remove related with reason → 200, gone from both tasks
        print("\n[20] DELETE related with reason → 200")
        code, body = _call(f"/api/v2/tasks/{tA.id}/related/{rel_id}", method="DELETE",
                           body={"reason": "No longer considered related after design review"})
        _ok(code == 200, f"HTTP {code}")
        code, body = _call(f"/api/v2/tasks/{tA.id}/relationships")
        d = json.loads(body)
        _ok(not any(r.get("id") == tC.id for r in d.get("related", [])), "C no longer in A's related")

        # [21] Self-related → 422
        print("\n[21] POST /tasks/{A.id}/related with other=A → 422 RELATED_SELF")
        code, body = _call(f"/api/v2/tasks/{tA.id}/related", method="POST",
                           body={"other_task_id": tA.id,
                                 "reason": "self-related, should be rejected"})
        _ok(code == 422, f"HTTP {code}")
        _ok("RELATED_SELF" in body, "self-related code present")

        print("\n" + "=" * 66)
        print("  ALL CHECKS GREEN")
        print("=" * 66)
        return 0

    finally:
        for t in [tA, tB, tC, tD]:
            db.execute(text("DELETE FROM activity_events WHERE task_id = :t"), {"t": t.id})
            db.execute(text("DELETE FROM task_relationships WHERE a_id = :t OR b_id = :t"), {"t": t.id})
        db.execute(text("DELETE FROM tasks WHERE project_id = :p"), {"p": proj_id})
        db.execute(text("DELETE FROM projects WHERE id = :p"), {"p": proj_id})
        if _cookie:
            db.execute(text("DELETE FROM sessions WHERE id = :s"), {"s": _cookie})
        db.commit()
        db.close()


if __name__ == "__main__":
    sys.exit(main())
