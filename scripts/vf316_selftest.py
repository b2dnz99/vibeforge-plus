#!/usr/bin/env python3
"""
VF-316 self-test — drift experimental endpoints must accept elevated SU.

Runs INSIDE the app container. Creates a transient elevated SU session (vf_session
with elevated_until in the future), hits every experimental endpoint + toggle, verifies
200 (was 401 before fix). Reverts toggle. Cleans up the session.

    docker compose exec app python scripts/vf316_selftest.py
"""
import os
import sys
import json
import uuid
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.models.user import User
from app.models.session import UserSession
from app.models.system_settings import SystemSetting

DB_URL = os.environ.get("DATABASE_URL",
                        "postgresql+psycopg2://vibeforge:vibeforge@db:5432/vibeforge")
BASE = "http://localhost:8000"


def _call(path, cookie, method="GET", body=None):
    req = urllib.request.Request(
        BASE + path, method=method,
        headers={"Cookie": f"vf_session={cookie}", "Content-Type": "application/json"},
        data=(json.dumps(body).encode() if body is not None else None),
    )
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _assert(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        raise AssertionError(msg)


def main():
    engine = create_engine(DB_URL)
    Session = sessionmaker(bind=engine)
    db = Session()

    print("=" * 62)
    print("  VF-316 SELF-TEST — SU-elevated access to drift experimental")
    print("=" * 62)

    su = db.query(User).filter(User.role == "super_user", User.status == "active").first()
    if not su:
        print("  FAIL: no active SU user; run seed_dev_accounts.py first.")
        return 2

    # Transient elevated SU session (mirrors /admin/elevate outcome)
    session_token = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    sess = UserSession(
        id=session_token,
        user_id=su.id,
        session_type="user",
        created_at=now,
        expires_at=now + timedelta(hours=8),
        elevated_until=now + timedelta(minutes=15),
    )
    db.add(sess)
    db.commit()

    # Capture current drift_gate_enabled so we can restore
    prev = db.query(SystemSetting).filter(SystemSetting.key == "drift_gate_enabled").first()
    prev_value = prev.value if prev else None

    try:
        # 1. Page itself renders
        print("\n[1] GET /admin/experimental/drift as elevated SU — page renders")
        code, body = _call("/admin/experimental/drift", session_token)
        _assert(code == 200, f"HTTP {code}")
        _assert("drift" in body.lower(), "drift content on page")
        _assert("_toast(" in body, "VF-316 toast helper embedded in template")
        _assert("alert('Toggle failed.')" not in body, "old native alert removed")

        # 2. Every JSON endpoint that was failing for SU
        print("\n[2] JSON endpoints that were 401 before fix:")
        for ep in [
            "/admin/api/experimental/drift/summary",
            "/admin/api/experimental/drift/timeline",
            "/admin/api/experimental/drift/recent?status=all&limit=10",
            "/admin/api/experimental/drift/by-agent",
        ]:
            code, _ = _call(ep, session_token)
            _assert(code == 200, f"GET {ep} → HTTP {code}")

        # 3. Toggle accepts SU — flip off, verify, flip on, verify
        print("\n[3] POST /admin/api/experimental/drift/toggle")
        code, body = _call("/admin/api/experimental/drift/toggle", session_token,
                           method="POST", body={"enabled": False})
        _assert(code == 200, f"toggle(false) → HTTP {code}")
        db.expire_all()
        row = db.query(SystemSetting).filter(SystemSetting.key == "drift_gate_enabled").first()
        _assert(row is not None and row.value == "false", "setting persisted as 'false'")

        code, body = _call("/admin/api/experimental/drift/toggle", session_token,
                           method="POST", body={"enabled": True})
        _assert(code == 200, f"toggle(true) → HTTP {code}")
        db.expire_all()
        row = db.query(SystemSetting).filter(SystemSetting.key == "drift_gate_enabled").first()
        _assert(row is not None and row.value == "true", "setting persisted as 'true'")

        # 4. Unauth (no cookie) is still blocked — 401 or 403, NOT 200
        print("\n[4] No cookie — still rejected")
        req = urllib.request.Request(
            BASE + "/admin/api/experimental/drift/summary", method="GET",
        )
        try:
            r = urllib.request.urlopen(req, timeout=5)
            raise AssertionError(f"Unauth call returned {r.status}; should have been 401")
        except urllib.error.HTTPError as e:
            _assert(e.code in (401, 403), f"unauth → HTTP {e.code}")

        # 5. Reset-all path also opens for SU (end any created escalations? we don't create any
        # in this test, so a call with confirm=true should succeed and return ended=0)
        print("\n[5] POST /admin/api/experimental/drift/reset-all (confirm=true)")
        code, body = _call("/admin/api/experimental/drift/reset-all", session_token,
                           method="POST", body={"confirm": True})
        _assert(code == 200, f"reset-all → HTTP {code}")
        d = json.loads(body)
        _assert("ended" in d, "reset-all returned 'ended' count")

        print("\n" + "=" * 62)
        print("  ALL CHECKS GREEN")
        print("=" * 62)
        return 0

    finally:
        # Restore prior setting value so we don't mutate env state
        row = db.query(SystemSetting).filter(SystemSetting.key == "drift_gate_enabled").first()
        if prev_value is None and row is not None:
            db.delete(row)
        elif row is not None and row.value != prev_value:
            row.value = prev_value
        db.execute(text("DELETE FROM sessions WHERE id = :id"), {"id": session_token})
        db.commit()
        db.close()


if __name__ == "__main__":
    sys.exit(main())
