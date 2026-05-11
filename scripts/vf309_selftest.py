"""VF-309 self-test. Run inside app container with PYTHONPATH=/app."""
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone

import bcrypt
from sqlalchemy import text

from app.db.session import SessionLocal, engine

BASE = "http://localhost:8000"


def http(method: str, path: str, body=None, cookies=None, expect=None):
    import http.cookiejar
    import urllib.request

    cj = http.cookiejar.CookieJar()
    if cookies:
        for name, val in cookies.items():
            cj.set_cookie(http.cookiejar.Cookie(
                version=0, name=name, value=val, port=None, port_specified=False,
                domain="localhost", domain_specified=False, domain_initial_dot=False,
                path="/", path_specified=True, secure=False, expires=None,
                discard=True, comment=None, comment_url=None, rest={}, rfc2109=False,
            ))
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    headers = {"Content-Type": "application/json"}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method, headers=headers)
    try:
        with opener.open(req) as r:
            status = r.status
            raw = r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        status = e.code
        raw = e.read().decode("utf-8", errors="replace")
    body_out = raw
    try:
        body_out = json.loads(raw)
    except Exception:
        pass
    if expect is not None and status != expect:
        print(f"FAIL {method} {path} expected {expect} got {status}: {body_out}")
        return False, status, body_out
    return True, status, body_out


def run():
    print("=" * 60)
    print("VF-309 self-test")
    print("=" * 60)

    # Step 1: verify /bootstrap/status — install should be closed (DEV has SA + SU)
    # Note: /bootstrap/status is gated with _gate_open_or_sa; when install is closed
    # it requires SA auth, so we don't hit it directly. Instead check DB state.
    with SessionLocal() as db:
        sa_count = db.execute(text("SELECT COUNT(*) FROM users WHERE role='super_admin' AND status='active'")).scalar()
        su_count = db.execute(text("SELECT COUNT(*) FROM users WHERE role='super_user' AND status='active'")).scalar()
    print(f"[state] active SA={sa_count}, active SU={su_count}")
    if sa_count < 1 or su_count < 1:
        print("WARN: DEV does not have both SA and SU. Install banner will be on.")
        return 1

    # Step 2: normal user login regression — only runs on DEV where autot seed exists.
    # On UAT/PROD the user is absent and we expect 401; skip instead of failing.
    ok, status, body = http("POST", "/ui/login", {"email": "autot", "password": "AutoT3st-2026!"})
    if status == 200:
        print(f"[ok] autot login -> {status} (non-SA login still works)")
    elif status == 401:
        print(f"[skip] autot regression (user absent on this env, expected on UAT/PROD)")
    else:
        print(f"FAIL: autot login returned unexpected {status}: {body}")
        return 1

    # Step 3: create a temp super_admin, attempt /ui/login, expect 403
    tmp_id = str(uuid.uuid4())
    tmp_email = f"tmpsa-{tmp_id[:8]}@vibeforge.local"
    tmp_username = f"tmpsa{tmp_id[:4]}"
    tmp_password = "VF309TestBlock!"
    tmp_hash = bcrypt.hashpw(tmp_password.encode(), bcrypt.gensalt()).decode()
    try:
        with SessionLocal() as db:
            db.execute(text("""
                INSERT INTO users (id, username, email, display_name, role, status, password_hash, created_at, updated_at)
                VALUES (:id, :u, :e, 'Temp SA VF309', 'super_admin', 'active', :h, :now, :now)
            """), {"id": tmp_id, "u": tmp_username[:10], "e": tmp_email, "h": tmp_hash, "now": datetime.now(timezone.utc)})
            db.commit()
        print(f"[setup] temp super_admin created: {tmp_email}")

        # Attempt board login — must be blocked with 403
        ok, status, body = http("POST", "/ui/login", {"email": tmp_email, "password": tmp_password}, expect=403)
        if not ok:
            print(f"FAIL: temp SA login was not blocked (got {status})")
            return 1
        detail = body.get("detail") if isinstance(body, dict) else str(body)
        if "Super Admin" not in detail:
            print(f"FAIL: 403 detail missing 'Super Admin' hint: {detail}")
            return 1
        print(f"[ok] temp SA board login blocked -> 403 '{detail}'")

        # Verify audit event recorded
        time.sleep(0.2)
        with SessionLocal() as db:
            evt = db.execute(text("""
                SELECT action, details FROM activity_events
                WHERE action='login_blocked_sa' AND details LIKE :p
                ORDER BY created_at DESC LIMIT 1
            """), {"p": f"%{tmp_email}%"}).fetchone()
        if not evt:
            # Fallback: look up by username since details may use username
            with SessionLocal() as db:
                evt = db.execute(text("""
                    SELECT action, details FROM activity_events
                    WHERE action='login_blocked_sa' AND details LIKE :p
                    ORDER BY created_at DESC LIMIT 1
                """), {"p": f"%{tmp_username[:10]}%"}).fetchone()
        if not evt:
            print("FAIL: no login_blocked_sa audit event recorded")
            return 1
        print(f"[ok] audit event recorded: action={evt[0]}")

    finally:
        # Cleanup regardless of test outcome
        with SessionLocal() as db:
            db.execute(text("DELETE FROM users WHERE id=:id"), {"id": tmp_id})
            db.commit()
        print(f"[cleanup] temp super_admin deleted")

    # Step 4: install_open logic check — simulate zero-SU state (rollback test only)
    # We don't actually mutate DB. We invoke install_open with a transaction we rollback.
    from app.api.v2.bootstrap import install_open as _io
    with SessionLocal() as db:
        current = _io(db)
    print(f"[state] install_open right now: {current} (should be False since SA+SU exist)")
    if current:
        print("FAIL: install_open should be False when SA + SU both exist")
        return 1

    # Simulate: flip the SU status to 'suspended' in a transaction, check install_open, rollback
    with SessionLocal() as db:
        try:
            db.execute(text("UPDATE users SET status='suspended' WHERE role='super_user' AND status='active'"))
            check = _io(db)
            if not check:
                print("FAIL: install_open did not flip to True when no active SU")
                return 1
            print(f"[ok] install_open flips True when no active SU (simulated)")
        finally:
            db.rollback()

    print("=" * 60)
    print("ALL CHECKS PASSED")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(run())
