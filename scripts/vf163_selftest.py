#!/usr/bin/env python3
"""VF-163 self-test — auto-clear downstream blocked_by when blocker closes.

Runs INSIDE the app container. Creates a transient project + blocker + 2
downstream tasks, verifies:
  - downstream tasks carry blocked_by pointing at blocker
  - transitioning blocker to done auto-clears each downstream's blocked_by
  - a task_relationships row (kind=related) is inserted for each converted pair
  - blocked_by_auto_cleared + blocks_auto_cleared audit events are emitted
  - idempotency: re-running a done→reopen→done cycle does not duplicate related rows
  - same behaviour for cancelled transitions

    docker compose exec app python scripts/vf163_selftest.py
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
from app.models.task_relationship import TaskRelationship

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


def _ok(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        raise AssertionError(msg)


def main():
    global _cookie
    engine = create_engine(DB_URL)
    Session = sessionmaker(bind=engine)
    db = Session()

    print("=" * 66)
    print("  VF-163 SELF-TEST — auto-clear downstream blocked_by")
    print("=" * 66)

    # Borrow an SU session for auth
    su = db.query(User).filter(User.role == "super_user", User.status == "active").first()
    _ok(su is not None, "active SU exists")
    _cookie = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    db.add(UserSession(id=_cookie, user_id=su.id, session_type="user",
                       created_at=now, expires_at=now + timedelta(hours=1)))
    db.commit()

    # Transient project
    tag = uuid.uuid4().hex[:4]
    proj_slug = f"vf163-{tag}"
    proj_id = str(uuid.uuid4())
    from sqlalchemy import func
    max_pn = db.query(func.max(Project.project_number)).scalar() or 0
    pn = max_pn + 1000
    proj = Project(id=proj_id, slug=proj_slug, name=f"VF163 {tag}",
                   status="active", prefix=f"V{tag[:2].upper()}",
                   project_number=pn, agentic_dev=False)
    db.add(proj)
    db.commit()

    # 3 tasks: BLOCKER, DS_A, DS_B
    def _mk_task(title, status="ready"):
        tid = str(uuid.uuid4())
        tnum = (db.query(func.max(Task.task_number))
                .filter(Task.project_id == proj_id).scalar() or 0) + 1
        t = Task(id=tid, project_id=proj_id, title=title, status=status,
                 priority="medium", owner_label="human:Parvez Khan",
                 task_number=tnum)
        db.add(t)
        db.commit()
        return tid

    blocker_id = _mk_task("VF163 blocker")
    ds_a_id = _mk_task("VF163 downstream A")
    ds_b_id = _mk_task("VF163 downstream B")

    # Wire: DS_A and DS_B both blocked by BLOCKER
    for ds_id in (ds_a_id, ds_b_id):
        code, _ = _call(f"/api/v2/tasks/{ds_id}", method="PATCH", body={
            "blocked_by_task_id": blocker_id,
            "blocked_by_reason": "VF163 self-test wire",
        })
        _ok(code == 200, f"set blocked_by on {ds_id[:8]} → {code}")

    # Confirm blocked_by is wired
    for ds_id in (ds_a_id, ds_b_id):
        db.expire_all()
        t = db.query(Task).filter(Task.id == ds_id).first()
        _ok(t.blocked_by_task_id == blocker_id, f"{ds_id[:8]}.blocked_by == blocker")

    # ACT 1: Transition blocker to done (via human SU — agents can't close)
    print("\n[1] Transition blocker → done")
    code, body = _call(f"/api/v2/tasks/{blocker_id}", method="PATCH", body={
        "status": "needs_review",
        "owner_label": "human:Parvez Khan",
        "transition_note": "ready for VF-163 self-test done transition",
    })
    _ok(code == 200, f"blocker → needs_review ({code})")
    # add a completion note
    code, _ = _call(f"/api/v2/tasks/{blocker_id}/notes", method="POST", body={
        "body": "completion note for self-test", "is_completion_note": True,
    })
    _ok(code == 201, f"completion note posted ({code})")
    code, _ = _call(f"/api/v2/tasks/{blocker_id}", method="PATCH", body={
        "status": "done", "transition_note": "closing for VF-163 test",
    })
    _ok(code == 200, f"blocker → done ({code})")

    # Verify downstream cleanup
    print("\n[2] Downstream auto-clear verification")
    for ds_id, label in [(ds_a_id, "DS_A"), (ds_b_id, "DS_B")]:
        db.expire_all()
        t = db.query(Task).filter(Task.id == ds_id).first()
        _ok(t.blocked_by_task_id is None, f"{label}.blocked_by cleared to NULL")
        # task_relationships row exists
        ids = sorted([blocker_id, ds_id])
        rel = (db.query(TaskRelationship)
               .filter(TaskRelationship.a_id == ids[0],
                       TaskRelationship.b_id == ids[1],
                       TaskRelationship.kind == "related")
               .first())
        _ok(rel is not None, f"{label}: related row created")
        _ok(rel.reason.startswith("Auto-converted"), f"{label}: reason is auto-converted marker")

    # Verify audit events
    print("\n[3] Audit events")
    for ds_id, label in [(ds_a_id, "DS_A"), (ds_b_id, "DS_B")]:
        downstream_evt = (db.query(ActivityEvent)
                          .filter(ActivityEvent.task_id == ds_id,
                                  ActivityEvent.action == "blocked_by_auto_cleared")
                          .first())
        _ok(downstream_evt is not None, f"{label}: blocked_by_auto_cleared event exists")
        blocker_evt = (db.query(ActivityEvent)
                       .filter(ActivityEvent.task_id == blocker_id,
                               ActivityEvent.action == "blocks_auto_cleared")
                       .all())
        _ok(len(blocker_evt) >= 1, f"blocker has blocks_auto_cleared event for {label}")

    # ACT 2: Idempotency — reopen blocker, re-close. Should NOT duplicate related rows.
    print("\n[4] Idempotency on reopen→close cycle")
    code, _ = _call(f"/api/v2/tasks/{blocker_id}", method="PATCH", body={
        "status": "ready", "transition_note": "reopening for idempotency test",
    })
    _ok(code == 200, f"blocker reopened ({code})")
    # Note: downstream stays at blocked_by=NULL (we don't auto-restore on reopen — by design).
    # Re-close. No new related rows should be added because existing ones already cover the pair.
    code, _ = _call(f"/api/v2/tasks/{blocker_id}", method="PATCH", body={
        "status": "needs_review", "owner_label": "human:Parvez Khan",
        "transition_note": "re-entering review for idempotency test",
    })
    _ok(code == 200, "blocker → needs_review for re-close")
    code, _ = _call(f"/api/v2/tasks/{blocker_id}/notes", method="POST", body={
        "body": "re-completion note", "is_completion_note": True,
    })
    _ok(code == 201, "re-completion note posted")
    code, _ = _call(f"/api/v2/tasks/{blocker_id}", method="PATCH", body={
        "status": "done", "transition_note": "re-closing for idempotency",
    })
    _ok(code == 200, "blocker re-closed")

    # Count related rows — still only 2 (one per pair)
    related_count = (db.query(TaskRelationship)
                     .filter(TaskRelationship.kind == "related")
                     .filter((TaskRelationship.a_id == blocker_id) | (TaskRelationship.b_id == blocker_id))
                     .count())
    _ok(related_count == 2, f"idempotent: still 2 related rows (got {related_count})")

    # ACT 3: Cancelled path — new pair, verify same auto-clear behaviour
    print("\n[5] Cancelled blocker — same auto-clear path")
    blocker2 = _mk_task("VF163 blocker 2")
    ds_c = _mk_task("VF163 downstream C")
    code, _ = _call(f"/api/v2/tasks/{ds_c}", method="PATCH", body={
        "blocked_by_task_id": blocker2,
        "blocked_by_reason": "VF163 cancelled-path test wire",
    })
    _ok(code == 200, f"DS_C.blocked_by = blocker2 ({code})")
    # Cancel blocker2 directly (SU can cancel without needs_review)
    code, body = _call(f"/api/v2/tasks/{blocker2}", method="PATCH", body={
        "status": "cancelled",
        "abandoned_note": json.dumps([{"reason": "VF163 self-test cancellation", "at": now.isoformat()}]),
        "transition_note": "cancelling blocker2 for VF-163 test",
    })
    _ok(code == 200, f"blocker2 → cancelled ({code})  body={body[:200]}")
    db.expire_all()
    t = db.query(Task).filter(Task.id == ds_c).first()
    _ok(t.blocked_by_task_id is None, "DS_C.blocked_by cleared after cancelled")

    # Cleanup
    print("\n[cleanup]")
    db.query(ActivityEvent).filter(ActivityEvent.project_id == proj_id).delete(synchronize_session=False)
    db.query(TaskRelationship).filter(
        (TaskRelationship.a_id.in_([blocker_id, ds_a_id, ds_b_id, blocker2, ds_c])) |
        (TaskRelationship.b_id.in_([blocker_id, ds_a_id, ds_b_id, blocker2, ds_c]))
    ).delete(synchronize_session=False)
    db.query(Task).filter(Task.project_id == proj_id).delete(synchronize_session=False)
    db.query(Project).filter(Project.id == proj_id).delete(synchronize_session=False)
    db.query(UserSession).filter(UserSession.id == _cookie).delete(synchronize_session=False)
    db.commit()
    print("  cleaned up test rows")

    print("\n" + "=" * 66)
    print("  ALL CHECKS GREEN")
    print("=" * 66)


if __name__ == "__main__":
    main()
