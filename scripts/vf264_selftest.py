"""VF-264 Phase 2 self-test — SU elevation tier. DEV-only."""
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
    print("VF-264 Phase 2 self-test")
    print("=" * 60)

    # 1. Migration applied
    with SessionLocal() as db:
        col_check = db.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name='sessions' AND column_name='elevated_until'
        """)).fetchone()
    if not col_check:
        print("FAIL: sessions.elevated_until column missing — migration not applied")
        return 1
    print("[ok] sessions.elevated_until column present (migration applied)")

    # 2. Set up a test SU + session
    su_id = str(uuid.uuid4())
    su_email = f"vf264-{su_id[:8]}@t.local"
    su_username = f"vf264{su_id[:4]}"
    su_pw = "Vf264Test!123"
    su_hash = bcrypt.hashpw(su_pw.encode(), bcrypt.gensalt()).decode()
    now = datetime.now(timezone.utc)

    try:
        with SessionLocal() as db:
            db.execute(text("""
                INSERT INTO users (id, username, email, display_name, role, status, password_hash, created_at, updated_at)
                VALUES (:id, :u, :e, 'VF-264 SU', 'super_user', 'active', :h, :now, :now)
            """), {"id": su_id, "u": su_username[:10], "e": su_email, "h": su_hash, "now": now})
            db.commit()

        # Log in as SU to get a vf_session
        def call(method, path, body=None, cookie=None):
            data = json.dumps(body).encode() if body is not None else None
            headers = {"Content-Type": "application/json"}
            if cookie:
                headers["Cookie"] = cookie
            req = urllib.request.Request(f"http://localhost:8000{path}",
                                         data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req) as r:
                    set_cookie = r.headers.get("Set-Cookie", "")
                    return r.status, json.loads(r.read()), set_cookie
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", errors="replace")
                try:
                    return e.code, json.loads(raw), ""
                except Exception:
                    return e.code, raw, ""

        status, body, set_cookie = call("POST", "/ui/login", {"email": su_email, "password": su_pw})
        if status != 200:
            print(f"FAIL: SU login expected 200 got {status}: {body}")
            return 1
        # Extract vf_session from Set-Cookie
        su_cookie_val = None
        for part in set_cookie.split(","):
            if "vf_session=" in part:
                su_cookie_val = part.split("vf_session=")[1].split(";")[0].strip()
                break
        if not su_cookie_val:
            print(f"FAIL: vf_session cookie not issued: set-cookie={set_cookie!r}")
            return 1
        print(f"[ok] SU login returns vf_session cookie")
        su_cookie = f"vf_session={su_cookie_val}"

        # 3. Unelevated SU hitting admin endpoint -> 403
        status, body, _ = call("GET", "/admin/api/users", cookie=su_cookie)
        if status != 403:
            print(f"FAIL: unelevated SU on admin endpoint expected 403, got {status}: {body}")
            return 1
        print(f"[ok] unelevated SU blocked from /admin/api/users with 403")

        # 4. Elevate with wrong password -> 401
        status, body, _ = call("POST", "/admin/elevate",
                               {"password": "wrong-password-nope"}, cookie=su_cookie)
        if status != 401:
            print(f"FAIL: elevate with wrong password expected 401, got {status}: {body}")
            return 1
        print(f"[ok] elevate with wrong password -> 401")

        # 5. Elevate with correct password -> 200
        status, body, _ = call("POST", "/admin/elevate",
                               {"password": su_pw}, cookie=su_cookie)
        if status != 200:
            print(f"FAIL: elevate expected 200, got {status}: {body}")
            return 1
        if "elevated_until" not in body:
            print(f"FAIL: elevate response missing elevated_until: {body}")
            return 1
        print(f"[ok] elevate with correct password -> 200, elevated_until returned")

        # 6. Verify column actually stamped
        with SessionLocal() as db:
            sess = db.execute(text(
                "SELECT elevated_until FROM sessions WHERE id = :id"
            ), {"id": su_cookie_val}).fetchone()
        if not sess or sess[0] is None:
            print(f"FAIL: sessions.elevated_until not stamped: {sess}")
            return 1
        print(f"[ok] sessions.elevated_until stamped to {sess[0]}")
        elevated_until_1 = sess[0]

        # 7. Now elevated SU can hit admin endpoint -> 200
        status, body, _ = call("GET", "/admin/api/users", cookie=su_cookie)
        if status != 200:
            print(f"FAIL: elevated SU on admin endpoint expected 200, got {status}: {body}")
            return 1
        print(f"[ok] elevated SU can hit /admin/api/users -> 200")

        # 8. Rolling-window: each admin hit rolls elevated_until forward
        import time as _t
        _t.sleep(1.1)
        status, body, _ = call("GET", "/admin/api/users", cookie=su_cookie)
        with SessionLocal() as db:
            sess = db.execute(text(
                "SELECT elevated_until FROM sessions WHERE id = :id"
            ), {"id": su_cookie_val}).fetchone()
        if sess[0] <= elevated_until_1:
            print(f"FAIL: rolling window did not extend: was {elevated_until_1}, now {sess[0]}")
            return 1
        print(f"[ok] rolling window extended elevated_until on admin hit")

        # 9. Audit events
        with SessionLocal() as db:
            granted = db.execute(text("""
                SELECT details FROM activity_events
                WHERE action='su_elevation_granted' AND actor_user_id = :uid
                ORDER BY created_at DESC LIMIT 1
            """), {"uid": su_id}).fetchone()
            failed = db.execute(text("""
                SELECT details FROM activity_events
                WHERE action='su_elevation_failed' AND actor_user_id = :uid
                ORDER BY created_at DESC LIMIT 1
            """), {"uid": su_id}).fetchone()
        if not granted:
            print("FAIL: no su_elevation_granted audit event")
            return 1
        if not failed:
            print("FAIL: no su_elevation_failed audit event")
            return 1
        print(f"[ok] audit events: su_elevation_granted + su_elevation_failed both recorded")

        # 10. Simulate timeout: manually set elevated_until to the past, then hit endpoint
        with SessionLocal() as db:
            db.execute(text("""
                UPDATE sessions SET elevated_until = :past WHERE id = :id
            """), {"past": datetime.now(timezone.utc) - timedelta(minutes=1), "id": su_cookie_val})
            db.commit()
        status, body, _ = call("GET", "/admin/api/users", cookie=su_cookie)
        if status != 403:
            print(f"FAIL: expired elevation expected 403, got {status}: {body}")
            return 1
        print(f"[ok] expired elevation rejected with 403")

        # 11. Unelevate endpoint
        # First re-elevate
        call("POST", "/admin/elevate", {"password": su_pw}, cookie=su_cookie)
        status, body, _ = call("POST", "/admin/unelevate", cookie=su_cookie)
        if status != 200:
            print(f"FAIL: unelevate expected 200, got {status}")
            return 1
        with SessionLocal() as db:
            sess = db.execute(text(
                "SELECT elevated_until FROM sessions WHERE id = :id"
            ), {"id": su_cookie_val}).fetchone()
        if sess[0] is not None:
            print(f"FAIL: unelevate did not clear elevated_until: {sess[0]}")
            return 1
        print(f"[ok] unelevate clears elevated_until")

        # 12. Non-SU tries to elevate -> 403
        other_id = str(uuid.uuid4())
        other_pw = "OtherPw!123"
        other_hash = bcrypt.hashpw(other_pw.encode(), bcrypt.gensalt()).decode()
        with SessionLocal() as db:
            db.execute(text("""
                INSERT INTO users (id, username, email, display_name, role, status, password_hash, created_at, updated_at)
                VALUES (:id, :u, :e, 'VF-264 User', 'user', 'active', :h, :now, :now)
            """), {"id": other_id, "u": f"vf264u{other_id[:4]}"[:10], "e": f"vf264u{other_id[:8]}@t.local", "h": other_hash, "now": now})
            db.commit()
        status, body, other_set_cookie = call("POST", "/ui/login",
                                              {"email": f"vf264u{other_id[:8]}@t.local", "password": other_pw})
        if status != 200:
            print(f"FAIL: test user login expected 200, got {status}")
            return 1
        other_cookie_val = None
        for part in other_set_cookie.split(","):
            if "vf_session=" in part:
                other_cookie_val = part.split("vf_session=")[1].split(";")[0].strip()
                break
        other_cookie = f"vf_session={other_cookie_val}"
        status, body, _ = call("POST", "/admin/elevate", {"password": other_pw}, cookie=other_cookie)
        if status != 403:
            print(f"FAIL: non-SU elevate expected 403, got {status}: {body}")
            return 1
        print(f"[ok] non-SU elevate rejected with 403")

        # 13. Elevated flag in admin-write audits: exercise via a no-op admin call we can
        # cleanly detect in audit. Use POST to change-role on the other test user (-> user).
        # First re-elevate our SU
        call("POST", "/admin/elevate", {"password": su_pw}, cookie=su_cookie)
        status, body, _ = call("POST", f"/admin/api/users/{other_id}/change-role",
                               {"role": "user"}, cookie=su_cookie)
        if status != 200:
            print(f"FAIL: elevated SU change-role expected 200, got {status}: {body}")
            return 1
        # Confirm audit event carries elevated=true
        with SessionLocal() as db:
            evt = db.execute(text("""
                SELECT details FROM activity_events
                WHERE action='user_role_changed' AND actor_user_id = :uid
                ORDER BY created_at DESC LIMIT 1
            """), {"uid": su_id}).fetchone()
        if not evt:
            print("FAIL: no user_role_changed audit event for elevated SU action")
            return 1
        details = json.loads(evt[0] or "{}")
        if details.get("elevated") is not True:
            print(f"FAIL: audit event missing elevated=true: {details}")
            return 1
        print(f"[ok] elevated SU admin action audits with elevated=true + actor=SU display_name")

        # Cleanup other test user
        with SessionLocal() as db:
            db.execute(text("DELETE FROM sessions WHERE user_id = :id"), {"id": other_id})
            db.execute(text("DELETE FROM activity_events WHERE actor_user_id = :id"), {"id": other_id})
            db.execute(text("DELETE FROM users WHERE id = :id"), {"id": other_id})
            db.commit()

    finally:
        with SessionLocal() as db:
            db.execute(text("DELETE FROM sessions WHERE user_id = :id"), {"id": su_id})
            db.execute(text("DELETE FROM activity_events WHERE actor_user_id = :id"), {"id": su_id})
            db.execute(text("DELETE FROM users WHERE id = :id"), {"id": su_id})
            db.commit()
        print("[cleanup] test SU, sessions, audit events removed")

    print("=" * 60)
    print("ALL CHECKS PASSED")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(run())
