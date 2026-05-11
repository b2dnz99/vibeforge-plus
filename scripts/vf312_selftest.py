"""VF-312 self-test — suspend/unsuspend + login copy."""
import json
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
from sqlalchemy import text

from app.db.session import SessionLocal


def run():
    print("=" * 60)
    print("VF-312 self-test")
    print("=" * 60)

    with SessionLocal() as db:
        sa = db.execute(text("SELECT id FROM users WHERE role='super_admin' LIMIT 1")).fetchone()
    if not sa:
        print("FAIL: no SA on this env")
        return 1
    sa_id = sa[0]

    # SA session
    sa_sess = str(uuid.uuid4())
    with SessionLocal() as db:
        db.execute(text("""
            INSERT INTO sessions (id, user_id, session_type, expires_at, ip_address, user_agent)
            VALUES (:id, :uid, 'sa', :exp, '127.0.0.1', 'vf312-selftest')
        """), {"id": sa_sess, "uid": sa_id, "exp": datetime.now(timezone.utc) + timedelta(minutes=30)})
        db.commit()

    # Test user
    u_id = str(uuid.uuid4())
    u_email = f"vf312-{u_id[:8]}@t.local"
    u_username = f"vf312{u_id[:4]}"
    u_pw = "Vf312Test!123"
    u_hash = bcrypt.hashpw(u_pw.encode(), bcrypt.gensalt()).decode()
    now = datetime.now(timezone.utc)

    try:
        with SessionLocal() as db:
            db.execute(text("""
                INSERT INTO users (id, username, email, display_name, role, status, password_hash, created_at, updated_at)
                VALUES (:id, :u, :e, 'VF-312 Target', 'user', 'active', :h, :now, :now)
            """), {"id": u_id, "u": u_username[:10], "e": u_email, "h": u_hash, "now": now})
            db.commit()
        print(f"[setup] test user {u_username} active")

        def call(method, path, body=None, cookie=None):
            data = json.dumps(body).encode() if body is not None else None
            headers = {"Content-Type": "application/json"}
            if cookie:
                headers["Cookie"] = cookie
            req = urllib.request.Request(f"http://localhost:8000{path}",
                                         data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", errors="replace")
                try:
                    return e.code, json.loads(raw)
                except Exception:
                    return e.code, raw

        sa_cookie = f"vf_sa_session={sa_sess}"

        # 1. Baseline: login works
        status, body = call("POST", "/ui/login", {"email": u_email, "password": u_pw})
        if status != 200:
            print(f"FAIL: baseline login expected 200, got {status}: {body}")
            return 1
        print(f"[ok] baseline login -> 200")

        # 2. Suspend SA → 422
        status, body = call("POST", f"/admin/api/users/{sa_id}/suspend", cookie=sa_cookie)
        if status != 422:
            print(f"FAIL: suspend SA expected 422, got {status}: {body}")
            return 1
        print(f"[ok] suspend SA rejected with 422")

        # 3. Suspend last active SU → 422 (if only one SU exists; skip if multiple)
        with SessionLocal() as db:
            su_rows = db.execute(text("SELECT id FROM users WHERE role='super_user' AND status='active'")).fetchall()
        if len(su_rows) == 1:
            status, body = call("POST", f"/admin/api/users/{su_rows[0][0]}/suspend", cookie=sa_cookie)
            if status != 422:
                print(f"FAIL: suspend last SU expected 422, got {status}: {body}")
                return 1
            print(f"[ok] suspend last SU rejected with 422")
        else:
            print(f"[skip] multiple SUs on this env ({len(su_rows)}); last-SU guard not exercised here")

        # 4. Suspend test user → 200
        status, body = call("POST", f"/admin/api/users/{u_id}/suspend", cookie=sa_cookie)
        if status != 200:
            print(f"FAIL: suspend test user expected 200, got {status}: {body}")
            return 1
        print(f"[ok] suspend test user -> 200")

        # 5. Login after suspend → 403 with disabled copy
        status, body = call("POST", "/ui/login", {"email": u_email, "password": u_pw})
        if status != 403:
            print(f"FAIL: suspended login expected 403, got {status}: {body}")
            return 1
        detail = body.get("detail") if isinstance(body, dict) else str(body)
        if "disabled" not in detail.lower() or "admin" not in detail.lower():
            print(f"FAIL: suspended login detail missing expected copy: {detail}")
            return 1
        print(f"[ok] suspended login -> 403 with disabled-contact-admin copy")

        # 6. Audit events
        with SessionLocal() as db:
            evt = db.execute(text("""
                SELECT action FROM activity_events
                WHERE action='user_suspended' AND details LIKE :p
                ORDER BY created_at DESC LIMIT 1
            """), {"p": f"%{u_username[:10]}%"}).fetchone()
            login_fail = db.execute(text("""
                SELECT details FROM activity_events
                WHERE action='login_failed' AND details LIKE :p
                ORDER BY created_at DESC LIMIT 1
            """), {"p": f"%account_suspended%"}).fetchone()
        if not evt:
            print("FAIL: no user_suspended audit event recorded")
            return 1
        if not login_fail:
            print("FAIL: login_failed audit with reason=account_suspended not recorded")
            return 1
        print(f"[ok] audit events recorded: user_suspended and login_failed/account_suspended")

        # 7. Unsuspend → 200
        status, body = call("POST", f"/admin/api/users/{u_id}/unsuspend", cookie=sa_cookie)
        if status != 200:
            print(f"FAIL: unsuspend expected 200, got {status}: {body}")
            return 1
        print(f"[ok] unsuspend -> 200")

        # 8. Login works again
        status, body = call("POST", "/ui/login", {"email": u_email, "password": u_pw})
        if status != 200:
            print(f"FAIL: re-enabled login expected 200, got {status}: {body}")
            return 1
        print(f"[ok] login works again after unsuspend")

        # 9. Double-suspend idempotency: suspend, suspend again → 422
        call("POST", f"/admin/api/users/{u_id}/suspend", cookie=sa_cookie)
        status, body = call("POST", f"/admin/api/users/{u_id}/suspend", cookie=sa_cookie)
        if status != 422:
            print(f"FAIL: double suspend expected 422, got {status}: {body}")
            return 1
        print(f"[ok] double suspend rejected with 422")

    finally:
        with SessionLocal() as db:
            db.execute(text("DELETE FROM sessions WHERE user_id = :id"), {"id": u_id})
            db.execute(text("DELETE FROM activity_events WHERE details LIKE :p"), {"p": f"%{u_username[:10]}%"})
            db.execute(text("DELETE FROM users WHERE id = :id"), {"id": u_id})
            db.execute(text("DELETE FROM sessions WHERE id = :s"), {"s": sa_sess})
            db.commit()
        print("[cleanup] test user, sessions, audit events, SA session all removed")

    print("=" * 60)
    print("ALL CHECKS PASSED")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(run())
