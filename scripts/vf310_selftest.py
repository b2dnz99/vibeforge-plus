"""VF-310 self-test. Run inside app container with PYTHONPATH=/app."""
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
from sqlalchemy import text

from app.db.session import SessionLocal


def run():
    print("=" * 60)
    print("VF-310 self-test")
    print("=" * 60)

    # Part 1: verify no (super_user, super_admin) tuples remain in project-level paths
    # We simply import the modules and confirm they compile + expected strings absent.
    import importlib
    for mod_name in ("app.api.v2.projects", "app.api.v2.ui", "app.api.v2.members"):
        mod = importlib.import_module(mod_name)
        src = open(mod.__file__).read()
        if 'super_user", "super_admin"' in src or "super_user', 'super_admin'" in src:
            print(f"FAIL: {mod_name} still contains super_admin in a project-level tuple")
            return 1
    print("[ok] no project-level (super_user, super_admin) tuples remain")

    # Part 2: verify user.py comment is current
    src = open("/app/app/models/user.py").read()
    if "'super_admin', 'admin', 'user'" in src:
        print("FAIL: user.py still has stale comment listing (super_admin, admin, user)")
        return 1
    print("[ok] user.py role comment updated")

    # Part 3: verify admin.py change-role no longer accepts 'admin'
    src = open("/app/app/api/v2/admin.py").read()
    if '"viewer", "user", "super_user", "admin"' in src:
        print("FAIL: admin.py still allows 'admin' as a change-role value")
        return 1
    print("[ok] admin.py change-role rejects 'admin' role")

    # Part 4: startup hook cleanup test
    # Get the actual SA user
    with SessionLocal() as db:
        sa = db.execute(text("SELECT id, display_name FROM users WHERE role='super_admin' LIMIT 1")).fetchone()
        if not sa:
            print("SKIP: no SA user on this env")
        else:
            sa_id, sa_name = sa
            # Insert a fake legacy SA board session
            fake_sess = str(uuid.uuid4())
            db.execute(text("""
                INSERT INTO sessions (id, user_id, session_type, expires_at, ip_address, user_agent)
                VALUES (:id, :uid, 'user', :exp, '127.0.0.1', 'vf310-selftest')
            """), {"id": fake_sess, "uid": sa_id, "exp": datetime.now(timezone.utc) + timedelta(hours=1)})
            db.commit()
            # Invoke the startup hook directly
            from app.main import _vf310_cleanup_legacy_sa_board_sessions
            _vf310_cleanup_legacy_sa_board_sessions()
            # Verify gone
            with SessionLocal() as db2:
                still_there = db2.execute(text(
                    "SELECT id FROM sessions WHERE id = :id"
                ), {"id": fake_sess}).fetchone()
            if still_there:
                print("FAIL: startup hook did not delete the legacy SA board session")
                return 1
            print(f"[ok] startup hook deleted legacy SA board session for {sa_name}")

    # Part 5: verify real SA sessions (session_type=sa) are untouched
    with SessionLocal() as db:
        sa = db.execute(text("SELECT id FROM users WHERE role='super_admin' LIMIT 1")).fetchone()
        if sa:
            # Create a legitimate SA session (session_type=sa) and ensure hook doesn't touch it
            sa_id = sa[0]
            sa_sess = str(uuid.uuid4())
            db.execute(text("""
                INSERT INTO sessions (id, user_id, session_type, expires_at, ip_address, user_agent)
                VALUES (:id, :uid, 'sa', :exp, '127.0.0.1', 'vf310-selftest-sa')
            """), {"id": sa_sess, "uid": sa_id, "exp": datetime.now(timezone.utc) + timedelta(minutes=30)})
            db.commit()
            try:
                from app.main import _vf310_cleanup_legacy_sa_board_sessions
                _vf310_cleanup_legacy_sa_board_sessions()
                with SessionLocal() as db2:
                    preserved = db2.execute(text(
                        "SELECT id FROM sessions WHERE id = :id"
                    ), {"id": sa_sess}).fetchone()
                if not preserved:
                    print("FAIL: startup hook wrongly deleted a legitimate session_type=sa session")
                    return 1
                print("[ok] legitimate SA admin sessions (session_type=sa) are untouched")
            finally:
                with SessionLocal() as db3:
                    db3.execute(text("DELETE FROM sessions WHERE id = :id"), {"id": sa_sess})
                    db3.commit()

    print("=" * 60)
    print("ALL CHECKS PASSED")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(run())
