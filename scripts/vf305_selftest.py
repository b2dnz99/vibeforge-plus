#!/usr/bin/env python3
"""
VF-305 self-test — /api/v2/projects/{slug}/tasks/search endpoint.

Runs INSIDE the app container. Creates a transient project with 3 tasks
covering the three match buckets (number, title, description), exercises
the endpoint, verifies ranking + bucket tagging, cleans up.

    docker compose exec app python scripts/vf305_selftest.py
"""
import os, sys, json, uuid, urllib.request, urllib.error
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from datetime import timedelta
from app.models.user import User
from app.models.project import Project
from app.models.task import Task
from app.models.session import UserSession

DB_URL = os.environ.get("DATABASE_URL",
                        "postgresql+psycopg2://vibeforge:vibeforge@db:5432/vibeforge")
BASE = "http://localhost:8000"

# Populated in main() for auth on calls
_session_cookie = None


def _call(path):
    headers = {"Content-Type": "application/json"}
    if _session_cookie:
        headers["Cookie"] = f"vf_session={_session_cookie}"
    req = urllib.request.Request(BASE + path, method="GET", headers=headers)
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

    print("=" * 62)
    print("  VF-305 SELF-TEST — task search endpoint")
    print("=" * 62)

    global _session_cookie

    # Transient SU board session (has all-project access)
    su = db.query(User).filter(User.role == "super_user", User.status == "active").first()
    if not su:
        print("  FAIL: no active SU to borrow session from")
        return 2
    _session_cookie = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    db.add(UserSession(
        id=_session_cookie, user_id=su.id, session_type="user",
        created_at=now, expires_at=now + timedelta(hours=1),
    ))
    db.commit()

    # Transient project with unique slug
    tag = uuid.uuid4().hex[:6]
    proj_slug = f"vf305-{tag}"
    proj_id = str(uuid.uuid4())
    # Find max project_number to avoid clash
    from sqlalchemy import func
    max_pn = db.query(func.max(Project.project_number)).scalar() or 0
    pn = max_pn + 1000  # room for real projects to use anything up to max+999
    proj = Project(
        id=proj_id, slug=proj_slug,
        name=f"VF305 Test {tag}", status="active",
        prefix=f"T{tag[:2].upper()}",
        project_number=pn, agentic_dev=False,
    )
    db.add(proj)
    db.commit()

    # 3 tasks covering the buckets
    t1 = Task(id=str(uuid.uuid4()), project_id=proj_id, task_number=1001,
              title=f"Alpha task with unique-marker-{tag}",
              description="generic body one", status="backlog", priority="medium",
              owner_label=f"agent:search_t1_{tag}",
              created_at=datetime.now(timezone.utc),
              updated_at=datetime.now(timezone.utc))
    t2 = Task(id=str(uuid.uuid4()), project_id=proj_id, task_number=1002,
              title=f"Beta beta beta",
              description=f"This description contains the beacon_{tag} keyword.",
              status="in_progress", priority="high",
              owner_label=f"human:PK",
              created_at=datetime.now(timezone.utc),
              updated_at=datetime.now(timezone.utc))
    t3 = Task(id=str(uuid.uuid4()), project_id=proj_id, task_number=9999,
              title="Gamma — a done task",
              description="terminal",
              status="done", priority="low",
              owner_label=None,
              created_at=datetime.now(timezone.utc),
              updated_at=datetime.now(timezone.utc))
    db.add_all([t1, t2, t3])
    db.commit()

    try:
        # [1] empty q → empty result
        print("\n[1] q='' → empty rows")
        code, body = _call(f"/api/v2/projects/{proj_slug}/tasks/search?q=")
        _ok(code == 200, f"HTTP {code}")
        d = json.loads(body)
        _ok(d["rows"] == [], "rows is empty on empty q")
        _ok(d["total"] == 0, "total=0")

        # [2] numeric q matches task_number bucket
        print("\n[2] q='1001' → number bucket, VF-1001")
        code, body = _call(f"/api/v2/projects/{proj_slug}/tasks/search?q=1001")
        _ok(code == 200, f"HTTP {code}")
        d = json.loads(body)
        _ok(len(d["rows"]) >= 1, "at least 1 row")
        first = d["rows"][0]
        _ok(first["task_number"] == 1001, f"first row is t_n=1001 (got {first['task_number']})")
        _ok(first["match_field"] == "number", f"bucket=number (got {first['match_field']})")
        _ok(first["short_id"] == f"{proj.prefix}-1001", f"short_id correct (got {first['short_id']})")
        _ok(first["owner_label"] == f"agent:search_t1_{tag}", "owner_label rendered")

        # [3] partial number matches multiple (e.g. '99' matches 9999 and 1001... wait only 9999)
        print("\n[3] q='99' → matches 9999 (number bucket)")
        code, body = _call(f"/api/v2/projects/{proj_slug}/tasks/search?q=99")
        d = json.loads(body)
        nums = [r for r in d["rows"] if r["match_field"] == "number"]
        _ok(any(r["task_number"] == 9999 for r in nums), "9999 in number bucket")

        # [4] title match
        print("\n[4] q='Beta' → title bucket, VF-1002")
        code, body = _call(f"/api/v2/projects/{proj_slug}/tasks/search?q=Beta")
        d = json.loads(body)
        titles = [r for r in d["rows"] if r["match_field"] == "title"]
        _ok(any(r["task_number"] == 1002 for r in titles), "1002 in title bucket")
        one = next(r for r in titles if r["task_number"] == 1002)
        _ok(one["status"] == "in_progress", "status returned correctly")
        _ok(one["priority"] == "high", "priority returned correctly")

        # [5] description match with unique token
        print(f"\n[5] q='beacon_{tag}' → description bucket, VF-1002")
        code, body = _call(f"/api/v2/projects/{proj_slug}/tasks/search?q=beacon_{tag}")
        d = json.loads(body)
        descs = [r for r in d["rows"] if r["match_field"] == "description"]
        _ok(any(r["task_number"] == 1002 for r in descs), "1002 in description bucket")
        _ok(any(f"beacon_{tag}" in (r.get("description") or "") for r in descs),
            "description body contains the query")

        # [6] unique title match (no overlap with desc for same task)
        print(f"\n[6] q='unique-marker-{tag}' → title bucket only, VF-1001")
        code, body = _call(f"/api/v2/projects/{proj_slug}/tasks/search?q=unique-marker-{tag}")
        d = json.loads(body)
        _ok(len(d["rows"]) == 1, f"exactly 1 row (got {len(d['rows'])})")
        _ok(d["rows"][0]["match_field"] == "title", "bucket=title")

        # [7] case-insensitive
        print("\n[7] q='BETA' (uppercase) → title bucket")
        code, body = _call(f"/api/v2/projects/{proj_slug}/tasks/search?q=BETA")
        d = json.loads(body)
        _ok(any(r["task_number"] == 1002 for r in d["rows"]),
            "case-insensitive match returns VF-1002")

        # [8] includes done tasks
        print("\n[8] q='Gamma' → done task still matches (history-searchable)")
        code, body = _call(f"/api/v2/projects/{proj_slug}/tasks/search?q=Gamma")
        d = json.loads(body)
        done_matches = [r for r in d["rows"] if r["task_number"] == 9999]
        _ok(len(done_matches) == 1, "done task included in results")
        _ok(done_matches[0]["status"] == "done", "status=done preserved")

        # [9] limit respected
        print("\n[9] limit=1 → only 1 row returned even if more match")
        code, body = _call(f"/api/v2/projects/{proj_slug}/tasks/search?q=beta&limit=1")
        d = json.loads(body)
        _ok(len(d["rows"]) == 1, f"limit=1 honoured (got {len(d['rows'])})")

        # [10] unknown project slug → 404
        print("\n[10] unknown project slug → 404")
        code, body = _call("/api/v2/projects/nosuch-project-slug/tasks/search?q=x")
        _ok(code == 404, f"HTTP {code}")

        # [11] full_id format
        print("\n[11] full_id format")
        code, body = _call(f"/api/v2/projects/{proj_slug}/tasks/search?q=1001")
        d = json.loads(body)
        fid = d["rows"][0]["full_id"]
        expected = f"PRJ{pn:05d}-TSK01001"
        _ok(fid == expected, f"full_id = {fid} (expected {expected})")

        print("\n" + "=" * 62)
        print("  ALL CHECKS GREEN")
        print("=" * 62)
        return 0

    finally:
        db.execute(text("DELETE FROM tasks WHERE project_id = :p"), {"p": proj_id})
        db.execute(text("DELETE FROM projects WHERE id = :p"), {"p": proj_id})
        if _session_cookie:
            db.execute(text("DELETE FROM sessions WHERE id = :s"), {"s": _session_cookie})
        db.commit()
        db.close()


if __name__ == "__main__":
    sys.exit(main())
