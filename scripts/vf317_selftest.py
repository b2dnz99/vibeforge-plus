#!/usr/bin/env python3
"""
VF-317 self-test — SA login affordances on /ui/login and /admin/.

Runs INSIDE the app container. Loopback http, no creds needed.
    docker compose exec app python scripts/vf317_selftest.py
"""
import os, sys, json, uuid, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from app.models.user import User
from app.models.session import UserSession

DB_URL = os.environ.get("DATABASE_URL",
                        "postgresql+psycopg2://vibeforge:vibeforge@db:5432/vibeforge")
BASE = "http://localhost:8000"


def _call(path, cookie=None, method="GET", body=None, cookie_name="vf_sa_session"):
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = f"{cookie_name}={cookie}"
    req = urllib.request.Request(BASE + path, method=method, headers=headers,
                                 data=(json.dumps(body).encode() if body is not None else None),
                                 unverifiable=True)
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
    print("  VF-317 SELF-TEST - SA login affordances")
    print("=" * 62)

    # 1. /ui/login GET — SA recovery hint removed from footer
    print("\n[1] GET /ui/login - SA recovery line removed from footer")
    code, body = _call("/ui/login")
    _ok(code == 200, f"HTTP {code}")
    _ok("reset_sa_password.py" not in body, "SA recovery hint no longer in /ui/login")
    _ok("saBlockChip" in body, "VF-317 SA-block chip element present in template")

    # 2. /ui/login POST as an SA user with valid password — structured 403 SA_LOGIN_BLOCKED.
    # We need a valid password to get past the password check and reach the role gate.
    # Create a transient second SA user (direct ORM, bypassing the "cannot create another SA"
    # endpoint guard) with a known bcrypt hash. Clean up in finally.
    print("\n[2] POST /ui/login as SA with VALID password - structured SA_LOGIN_BLOCKED 403")
    import bcrypt as _bcrypt
    tag = uuid.uuid4().hex[:4]
    pw = "vf317test"
    pw_hash = _bcrypt.hashpw(pw.encode(), _bcrypt.gensalt()).decode()
    test_sa = User(
        id=str(uuid.uuid4()), username=f"satest{tag}",
        display_name=f"VF317 SA {tag}", email=f"vf317sa_{tag}@vf317.local",
        role="super_admin", status="active",
        password_hash=pw_hash, must_change_password=False,
    )
    db.add(test_sa)
    db.commit()

    try:
        code, body = _call("/ui/login", method="POST", body={
            "email": test_sa.username, "password": pw,
        })
        _ok(code == 403, f"HTTP {code}")
        d = json.loads(body)
        detail = d.get("detail")
        _ok(isinstance(detail, dict), f"detail is object (got {type(detail).__name__})")
        _ok(detail.get("code") == "SA_LOGIN_BLOCKED", f"code = SA_LOGIN_BLOCKED (got {detail.get('code')})")
        _ok(detail.get("admin_login_url") == "/admin/login", "admin_login_url points to /admin/login")
        _ok("collaborators" in (detail.get("message") or "").lower(),
            "message explains the board/admin split")
    finally:
        db.execute(text("DELETE FROM users WHERE id = :id"), {"id": test_sa.id})
        db.commit()

    # 3. /admin/login GET no query — redirects
    print("\n[3] GET /admin/login (no ?as=sa) - redirects (302 or 307)")
    # urllib follows redirects by default; we need to see the redirect itself
    req = urllib.request.Request(BASE + "/admin/login", method="GET")
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw): return None
    opener = urllib.request.build_opener(NoRedirect)
    try:
        r = opener.open(req, timeout=5)
        _ok(False, f"expected redirect, got HTTP {r.status}")
    except urllib.error.HTTPError as e:
        _ok(e.code in (301, 302, 303, 307, 308), f"redirect status {e.code}")

    # 4. /admin/login?as=sa unauth — renders SA-login form (no SU elevate)
    print("\n[4] GET /admin/login?as=sa unauth - renders SA-login form with recovery hint")
    code, body = _call("/admin/login?as=sa")
    _ok(code == 200, f"HTTP {code}")
    _ok("Admin Elevation" in body, "header reads 'Admin Elevation' (sa_login mode)")
    _ok("Super Admin credentials required" in body, "sub-line indicates SA-cred form")
    _ok("reset_sa_password.py" in body, "SA recovery hint present on admin login page")
    _ok("Confirm to Enter Admin Mode" not in body, "NOT in SU-elevate mode")

    # 5. /admin/login?as=sa WITH SU session — still renders SA-login form (VF-317 key behaviour)
    print("\n[5] GET /admin/login?as=sa with SU vf_session - STILL SA-login form (not elevate)")
    su = db.query(User).filter(User.role == "super_user", User.status == "active").first()
    _ok(su is not None, "an SU user exists")
    sid = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    db.add(UserSession(id=sid, user_id=su.id, session_type="user",
                       created_at=now,
                       expires_at=now + timedelta(hours=1),
                       elevated_until=now + timedelta(minutes=15)))
    db.commit()
    try:
        code, body = _call("/admin/login?as=sa", cookie=sid, cookie_name="vf_session")
        _ok(code == 200, f"HTTP {code}")
        _ok("Admin Elevation" in body, "header still reads 'Admin Elevation' even with SU cookie")
        _ok("Confirm to Enter Admin Mode" not in body,
            "SU-elevate form NOT rendered (force_sa worked)")
        _ok('onsubmit="doSALogin' in body, "form points at doSALogin handler")

        # 6. /admin/login without ?as=sa but with SU cookie — dual-mode picks elevate (existing behaviour)
        print("\n[6] /admin/ with SU cookie (no ?as=sa) - nav contains 'Login as SA' link")
        code, body = _call("/admin/", cookie=sid, cookie_name="vf_session")
        _ok(code == 200, f"HTTP {code}")
        _ok("Login as SA" in body, "'Login as SA' nav link present when acting SU-elevated")
        _ok("/admin/login?as=sa" in body, "link points at /admin/login?as=sa")

    finally:
        db.execute(text("DELETE FROM sessions WHERE id = :id"), {"id": sid})
        db.commit()

    # 7. /admin/ as actual SA - 'Login as SA' nav link NOT present (already SA)
    print("\n[7] /admin/ as real SA - no 'Login as SA' link (already SA)")
    sa_real = db.query(User).filter(User.role == "super_admin", User.status == "active").first()
    _ok(sa_real is not None, "real SA user found")
    sa_sid = str(uuid.uuid4())
    db.add(UserSession(id=sa_sid, user_id=sa_real.id, session_type="sa",
                       created_at=datetime.now(timezone.utc),
                       expires_at=datetime.now(timezone.utc) + timedelta(minutes=30)))
    db.commit()
    try:
        code, body = _call("/admin/", cookie=sa_sid, cookie_name="vf_sa_session")
        _ok(code == 200, f"HTTP {code}")
        _ok("Login as SA" not in body, "'Login as SA' link correctly hidden for real SA session")
    finally:
        db.execute(text("DELETE FROM sessions WHERE id = :id"), {"id": sa_sid})
        db.commit()

    print("\n" + "=" * 62)
    print("  ALL CHECKS GREEN")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        pass
