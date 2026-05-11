#!/usr/bin/env python3
"""
VF-307 self-test — Shape B admin tree + audit log filter params.

Runs INSIDE the app container. Creates a transient SA session row, hits
the admin endpoints over loopback, asserts the new tree markup + filter
chip, cleans up. Never needs a password. Never touches prod.

    docker compose exec app python scripts/vf307_selftest.py
"""
import os
import sys
import uuid
import urllib.request
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.models.user import User
from app.models.session import UserSession

DB_URL = os.environ.get("DATABASE_URL",
                        "postgresql+psycopg2://vibeforge:vibeforge@db:5432/vibeforge")
BASE = "http://localhost:8000"

def _get(path, cookie):
    req = urllib.request.Request(BASE + path, headers={"Cookie": f"vf_sa_session={cookie}"})
    return urllib.request.urlopen(req, timeout=10)

def _assert(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        raise AssertionError(msg)

def main():
    engine = create_engine(DB_URL)
    Session = sessionmaker(bind=engine)
    db = Session()

    print("=" * 60)
    print("  VF-307 SELF-TEST — Shape B admin tree + audit filters")
    print("=" * 60)

    sa = db.query(User).filter(User.role == "super_admin", User.status == "active").first()
    if not sa:
        print("  FAIL: no active SA user found; run seed_dev_accounts.py first.")
        return 2

    # Transient SA session — cookie value == sessions.id
    session_token = str(uuid.uuid4())
    sess = UserSession(
        id=session_token,
        user_id=sa.id,
        session_type="sa",
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
    )
    db.add(sess)
    db.commit()

    try:
        # 1. /admin/ renders the new tree markup
        print("\n[1] GET /admin/ authed — expect tree markup")
        r = _get("/admin/", session_token)
        body = r.read().decode()
        _assert(r.status == 200, f"HTTP {r.status}")
        _assert('id="adminTree"' in body, 'tree container id="adminTree" present')
        _assert('class="tree-controls"' in body, 'tree-controls block present')
        _assert("VF_TREE_KEY" in body, "localStorage persistence key embedded")
        _assert('class="user-row' in body or 'class="no-agents"' in body,
                "at least one user-row OR no-agents fallback rendered")

        # 2. Audit log without filter
        print("\n[2] GET /admin/auditlog — no chip")
        r = _get("/admin/auditlog", session_token)
        body = r.read().decode()
        _assert(r.status == 200, f"HTTP {r.status}")
        _assert('class="filter-chip"' not in body, "filter-chip absent when no params")

        # 3. Audit log filtered by actor_user_id=sa.id — chip should show
        print("\n[3] GET /admin/auditlog?actor_user_id=<sa.id> — expect chip")
        r = _get(f"/admin/auditlog?actor_user_id={sa.id}", session_token)
        body = r.read().decode()
        _assert(r.status == 200, f"HTTP {r.status}")
        _assert('class="filter-chip"' in body, "filter-chip rendered for user filter")
        _assert(sa.display_name in body, f"filter chip label '{sa.display_name}' rendered")
        _assert('href="/admin/auditlog"' in body, "Clear-filter link back to root present")

        # 4. Audit log filtered by bogus actor_user_id → chip still renders with raw id
        print("\n[4] GET /admin/auditlog?actor_user_id=nosuchid — chip fallback label")
        r = _get("/admin/auditlog?actor_user_id=nosuchid-deadbeef", session_token)
        body = r.read().decode()
        _assert(r.status == 200, f"HTTP {r.status}")
        _assert('class="filter-chip"' in body, "chip still renders for unknown user id")

        # 5. project_id filter (use first project id if any)
        from app.models.project import Project
        proj = db.query(Project).first()
        if proj:
            print(f"\n[5] GET /admin/auditlog?project_id={proj.id} — expect chip")
            r = _get(f"/admin/auditlog?project_id={proj.id}", session_token)
            body = r.read().decode()
            _assert(r.status == 200, f"HTTP {r.status}")
            _assert('class="filter-chip"' in body, "chip renders for project filter")
            _assert(proj.name in body, f"project name '{proj.name}' in chip label")
        else:
            print("\n[5] SKIP: no projects in DB")

        print("\n" + "=" * 60)
        print("  ALL CHECKS GREEN")
        print("=" * 60)
        return 0

    finally:
        db.execute(text("DELETE FROM sessions WHERE id = :id"), {"id": session_token})
        db.commit()
        db.close()


if __name__ == "__main__":
    sys.exit(main())
