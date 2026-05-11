#!/usr/bin/env python3
"""VF-329 self-test — Users workspace master-detail + NavGuard + reveal pattern.

Runs inside the app container:
    docker compose exec app python scripts/vf329_selftest.py

Tested cases:
  Server contract
    - GET /admin/portal/administration/users renders 200 for elevated SU
    - Page response carries the new master-detail markers (apu-shell)
    - Reset password endpoint returns 'temp_password' key (Bug 2 contract)
  Bug 1 (FLAG FAILED rendering)
    - POST a malformed payload to change-password, confirm response
      .detail is a JSON-serialisable shape the client renderer can format
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.session import UserSession
from app.models.user import User

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://vibeforge:vibeforge@db:5432/vibeforge",
)
BASE = "http://localhost:8000"

CHECKS = 0
FAILS = 0


def _ok(cond, msg):
    global CHECKS, FAILS
    CHECKS += 1
    mark = "OK  " if cond else "FAIL"
    if not cond: FAILS += 1
    print(f"  {mark} {msg}")


def _section(t): print(f"\n[{t}]")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)
    http_error_301 = http_error_302
    http_error_303 = http_error_302
    http_error_307 = http_error_302


_opener = urllib.request.build_opener(_NoRedirect())


def _call(method, path, *, vf=None, sa=None, body=None):
    headers = {}
    cookie_parts = []
    if vf: cookie_parts.append(f"vf_session={vf}")
    if sa: cookie_parts.append(f"vf_sa_session={sa}")
    if cookie_parts: headers["Cookie"] = "; ".join(cookie_parts)
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        r = _opener.open(req, timeout=10)
        return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers) if e.headers else {}


def main():
    engine = create_engine(DB_URL)
    Session = sessionmaker(bind=engine)
    db = Session()

    print("=" * 66)
    print("  VF-329 SELF-TEST — Users workspace + NavGuard + reveal")
    print("=" * 66)

    _section("setup")
    sa = db.query(User).filter(User.role == "super_admin", User.status == "active").first()
    _ok(sa is not None, "active SA exists")
    su = db.query(User).filter(User.role == "super_user", User.status == "active").first()
    _ok(su is not None, "active SU exists")
    if not (sa and su): return 1

    # Sessions
    now = datetime.now(timezone.utc)
    cookie_elev_su = str(uuid.uuid4())
    db.add(UserSession(
        id=cookie_elev_su, user_id=su.id, session_type="user",
        created_at=now, expires_at=now + timedelta(hours=1),
        elevated_until=now + timedelta(minutes=15),
    ))
    cookie_pure_sa = str(uuid.uuid4())
    db.add(UserSession(
        id=cookie_pure_sa, user_id=sa.id, session_type="sa",
        created_at=now, expires_at=now + timedelta(minutes=30),
    ))
    db.commit()

    _section("A. Users workspace renders master-detail")
    s, body, _ = _call("GET", "/admin/portal/administration/users", vf=cookie_elev_su)
    _ok(s == 200, f"GET /admin/portal/administration/users → 200 (got {s})")
    _ok("apu-shell" in body, "  page carries apu-shell marker (master-detail layout)")
    _ok("apu-list-pane" in body and "apu-detail-pane" in body, "  list pane + detail pane both present")
    _ok("vf-navguard-overlay" in body, "  NavGuard dialog DOM present in admin_base")
    _ok("APU_USERS_INIT" in body, "  user payload injected into JS")
    _ok("memberships" in body, "  memberships included in payload (Phase 2 panel)")

    _section("B. Bug 2 contract: Reset password returns temp_password")
    # Find a target user to reset (not the SU we're authed as, not SA)
    target = db.query(User).filter(
        User.status == "active",
        User.role.in_(("user", "super_user", "viewer")),
        User.id != su.id,
        User.id != sa.id,
    ).first()
    if target:
        s, body, _ = _call("POST", f"/admin/api/users/{target.id}/reset-password",
                          vf=cookie_elev_su)
        _ok(s == 200, f"POST reset-password → 200 (got {s})")
        if s == 200:
            d = json.loads(body)
            _ok("temp_password" in d, f"  response carries temp_password key (Bug 2 contract)")
            _ok(d.get("temp_password") and len(d["temp_password"]) >= 12,
                f"  temp_password is non-empty and reasonable length (got {len(d.get('temp_password') or '')} chars)")
    else:
        _ok(False, "no eligible target for reset-password test")

    _section("C. Bug 1 contract: malformed change-password returns parseable error")
    # Force a 422 by sending an empty body (FastAPI/Pydantic validation array)
    if target:
        s, body, _ = _call("POST", f"/admin/api/users/{target.id}/change-password",
                          vf=cookie_elev_su,
                          body={"current_password": "x", "new_password": ""})
        _ok(s in (401, 422), f"malformed change-password → 401 or 422 (got {s})")
        try:
            d = json.loads(body)
            detail = d.get("detail")
            # The new client renderer (_formatError in admin_portal_users.html) handles:
            #   - string detail
            #   - array of {msg, loc, ...} objects (FastAPI validation)
            #   - object with msg
            shape_ok = (
                isinstance(detail, str) or
                (isinstance(detail, list) and all(
                    isinstance(e, str) or (isinstance(e, dict) and "msg" in e)
                    for e in detail
                )) or
                (isinstance(detail, dict))
            )
            _ok(shape_ok, "  detail is in a shape _formatError can render (string / array-of-msg-objects / object)")
        except Exception as e:
            _ok(False, f"  could not parse body: {e}")

    _section("D. NavGuard / RevealPanel infra present in admin_base")
    s, body, _ = _call("GET", "/admin/portal/", vf=cookie_elev_su)
    _ok(s == 200, f"GET portal landing → 200 (got {s})")
    _ok("window.NavGuard" in body, "  window.NavGuard exposed")
    _ok("window.RevealPanel" in body, "  window.RevealPanel exposed")
    _ok("vf-navguard-overlay" in body, "  NavGuard dialog DOM present")
    _ok("vfNavGuard" in body and "vfRevealPanel" in body, "  both IIFEs declared")

    _section("E. Migration banners on Agents + Sessions")
    for path, name in (
        ("/admin/portal/administration/agents", "agents"),
        ("/admin/portal/administration/sessions", "sessions"),
    ):
        s, body, _ = _call("GET", path, vf=cookie_elev_su)
        _ok(s == 200, f"GET {path} → 200 (got {s})")
        _ok("DEV · pre-RC" in body and "Pending master-detail migration" in body,
            f"  {name} carries pending-migration banner")

    _section("cleanup")
    db.query(UserSession).filter(UserSession.id.in_([cookie_elev_su, cookie_pure_sa])).delete(synchronize_session=False)
    db.commit()
    print("  sessions cleaned")

    print("\n" + "=" * 66)
    if FAILS == 0:
        print(f"  ALL {CHECKS} CHECKS GREEN")
    else:
        print(f"  {FAILS} / {CHECKS} FAILED")
    print("=" * 66)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
