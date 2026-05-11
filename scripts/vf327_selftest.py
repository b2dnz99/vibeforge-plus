#!/usr/bin/env python3
"""VF-327 self-test — Escalated Admin Portal shell + workspaces.

Exercises:
  - /admin/portal/ gates: unauth -> login, plain SU -> login, elevated SA -> 200
  - All live workspace routes render 200 for elevated SA
  - Placeholder renderer works for known + unknown sub IDs
  - VF-335: legacy /admin/* HTML surfaces all return 301 to /admin/portal/*
  - VF-335: /ui/admin/proxy[/] returns 301 to /admin/portal/
  - VF-335: /ui/admin/proxy/change-cert sub-redirect still resolves
  - Shell elements present: SA pill, Back-to-Board button, nav tree primes

Run via:
    docker compose exec app python scripts/vf327_selftest.py
"""
from __future__ import annotations

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


def _call(path, cookie=None):
    headers = {}
    if cookie: headers["Cookie"] = f"vf_session={cookie}"
    req = urllib.request.Request(BASE + path, headers=headers)
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
    print("  VF-327 SELF-TEST — Escalated Admin Portal")
    print("=" * 66)

    su = db.query(User).filter(User.role == "super_user", User.status == "active").first()
    _ok(su is not None, "active SU exists")

    now = datetime.now(timezone.utc)
    cookie_su = str(uuid.uuid4())
    db.add(UserSession(id=cookie_su, user_id=su.id, session_type="user",
                       created_at=now, expires_at=now + timedelta(hours=1)))
    cookie_sa = str(uuid.uuid4())
    db.add(UserSession(id=cookie_sa, user_id=su.id, session_type="user",
                       created_at=now, expires_at=now + timedelta(hours=1),
                       elevated_until=now + timedelta(minutes=30)))
    db.commit()

    # ── A. Gate checks ──
    _section("A. portal gates")
    s, _, _ = _call("/admin/portal/")
    _ok(s == 302, f"unauth /admin/portal/ -> 302 (got {s})")

    # VF-335 regression guard: walk the unauth chain end-to-end. Pre-VF-335 the
    # chain was /admin/portal/ -> /admin/login -> /admin/ -> render(login).
    # Post-VF-335 /admin/ now 301s to /admin/portal/, so /admin/login MUST
    # render directly or the chain loops. Following up to 8 hops catches any
    # regression of that contract.
    import urllib.request as _ur
    _follower = _ur.build_opener(_ur.HTTPRedirectHandler())
    try:
        r = _follower.open(_ur.Request(BASE + "/admin/login"), timeout=10)
        _ok(r.status == 200, f"unauth GET /admin/login terminates at 200 (got {r.status})")
        body200 = r.read().decode()
        _ok("password" in body200.lower() or "Sign in" in body200,
            "  unauth /admin/login renders a login form (no loop)")
    except urllib.error.HTTPError as e:
        _ok(False, f"unauth /admin/login chain failed: {e.code}")
    except Exception as e:
        # urllib raises HTTPError for too-many-redirects with code matching the loop
        msg = str(e)
        _ok("redirect" not in msg.lower() and "loop" not in msg.lower(),
            f"unauth /admin/login chain looped: {msg[:120]}")

    s, _, _ = _call("/admin/portal/", cookie=cookie_su)
    _ok(s == 302, f"plain SU -> 302 login (got {s})")

    s, body, _ = _call("/admin/portal/", cookie=cookie_sa)
    _ok(s == 200, f"elevated SA -> 200 (got {s})")
    _ok("Elevated Portal" in body, "topbar says Elevated Portal")
    _ok("SA &middot; Elevated" in body or "SA · Elevated" in body, "SA pill present")
    _ok("Back to Board" in body, "Back to Board button present")
    _ok("Workspaces" in body, "sidebar label 'Workspaces' present")
    _ok("Configuration" in body and "Administration" in body and "Health" in body, "all primes in sidebar")

    # ── B. Live workspaces ──
    _section("B. live workspaces")
    endpoints = [
        ("/admin/portal/", "Elevated Portal"),
        ("/admin/portal/configuration/certificates", "Certificates"),
        ("/admin/portal/configuration/certificates/change-cert", "STAGE 1"),
        ("/admin/portal/administration/users", "Users"),
        ("/admin/portal/administration/agents", "Agents"),
        ("/admin/portal/administration/sessions", "Active sessions"),
        # Health collapsed to one canonical "System" page (Task-Manager
        # thesis) + Audit log. Proxy/DB sub-pages were folded in and deleted.
        ("/admin/portal/health/overview", "System Health"),
        ("/admin/portal/health/audit", "Audit log"),
        ("/admin/portal/lifecycle/environment", "Environment info"),
    ]
    for path, expected_text in endpoints:
        s, body, _ = _call(path, cookie=cookie_sa)
        _ok(s == 200, f"{path} -> 200 (got {s})")
        _ok(expected_text in body, f"  marker '{expected_text}' in body")

    # ── C. Placeholder renderer ──
    _section("C. placeholder renderer")
    s, body, _ = _call("/admin/placeholder/cfg-sso", cookie=cookie_sa)
    _ok(s == 200, f"/admin/placeholder/cfg-sso -> 200 (got {s})")
    _ok("OIDC" in body or "SAML" in body, "SSO placeholder copy present")
    _ok("Roadmap" in body, "roadmap badge present")

    s, body, _ = _call("/admin/placeholder/made-up-id", cookie=cookie_sa)
    _ok(s == 200, f"unknown sub -> 200 fallback (got {s})")
    _ok("placeholder" in body.lower() or "roadmap" in body.lower(), "fallback copy renders")

    # ── D. Sub-redirect: cert wizard alias ──
    _section("D. cert-wizard alias redirect")
    s, _, hdrs = _call("/ui/admin/proxy/change-cert")
    _ok(s == 301, f"old change-cert URL -> 301 (got {s})")
    loc = hdrs.get("location") or hdrs.get("Location", "")
    _ok("/admin/portal/configuration/certificates/change-cert" in loc,
        f"redirects to new portal URL (got {loc})")

    # ── E. VF-335 legacy admin surface graduation ──
    _section("E. VF-335 legacy /admin/* + /ui/admin/proxy graduations")
    # Map: old URL -> expected /admin/portal/* (or sub-string check) target.
    # Each must respond 301 + Location matching, regardless of auth state
    # (the redirect is unconditional; the target enforces its own gate).
    legacy_map = [
        ("/admin/",                       "/admin/portal/"),
        ("/admin/users/new",              "/admin/portal/administration/users/__new__"),
        ("/admin/users/00000000-0000-0000-0000-000000000000",
                                          "/admin/portal/administration/users/00000000-0000-0000-0000-000000000000"),
        ("/admin/agents/new",             "/admin/portal/administration/agents/__new__"),
        ("/admin/agents/00000000-0000-0000-0000-000000000000",
                                          "/admin/portal/administration/agents/00000000-0000-0000-0000-000000000000"),
        ("/admin/auditlog",               "/admin/portal/health/audit"),
        ("/admin/auditlog?actor_user_id=abc",
                                          "/admin/portal/health/audit?actor_user_id=abc"),
        ("/ui/admin/proxy",               "/admin/portal/"),
        ("/ui/admin/proxy/",              "/admin/portal/"),
    ]
    for old, expected_in_loc in legacy_map:
        s, _, hdrs = _call(old)
        _ok(s == 301, f"{old} -> 301 (got {s})")
        loc = hdrs.get("location") or hdrs.get("Location", "")
        _ok(expected_in_loc in loc, f"  Location contains {expected_in_loc!r} (got {loc!r})")

    # ── F. SU cannot reach portal ──
    _section("F. SU isolation")
    for path in ["/admin/portal/configuration/certificates",
                 "/admin/portal/administration/users",
                 "/admin/portal/health/overview",
                 "/admin/portal/lifecycle/environment"]:
        s, _, _ = _call(path, cookie=cookie_su)
        _ok(s == 302, f"SU on {path} -> 302 (got {s})")

    # ── G. Cleanup ──
    _section("G. cleanup")
    db.query(UserSession).filter(UserSession.id.in_([cookie_su, cookie_sa])).delete(synchronize_session=False)
    db.commit()
    print("  sessions cleaned")

    print("\n" + "=" * 66)
    if FAILS == 0:
        print("  ALL CHECKS GREEN")
    else:
        print(f"  {FAILS} / {CHECKS} FAILED")
    print("=" * 66)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
