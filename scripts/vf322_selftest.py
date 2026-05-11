#!/usr/bin/env python3
"""VF-322 self-test — Proxy Admin read-only surface.

Exercises:
  - /api/v2/proxy/config, /rules, /cert-info as SU cookie → 200 + expected shape
  - /api/v2/proxy/reload as SU (no elevation) → 403
  - /api/v2/proxy/reload as elevated SU → 200 + audit event
  - /api/v2/proxy/ca-bundle when mode != caddy_internal → 404
  - /ui/admin/proxy as SU → 200 HTML
  - /ui/admin/proxy with no cookie → 302 to login
  - /api/v2/proxy/config with no cookie → 401

    docker compose exec app python scripts/vf322_selftest.py
"""
import os, sys, json, uuid, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.models.user import User
from app.models.session import UserSession
from app.models.activity import ActivityEvent

DB_URL = os.environ.get("DATABASE_URL",
                        "postgresql+psycopg2://vibeforge:vibeforge@db:5432/vibeforge")
BASE = "http://localhost:8000"

CHECKS = 0
FAILS = 0


def _ok(cond, msg):
    global CHECKS, FAILS
    CHECKS += 1
    mark = "OK  " if cond else "FAIL"
    if not cond:
        FAILS += 1
    print(f"  {mark} {msg}")


def _section(title):
    print(f"\n[{title}]")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stop urlopen from following 302/303 — the tests assert on redirect
    status codes from auth gates, and the default handler would silently
    follow to /ui/login and return 200 there."""
    def http_error_302(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)
    http_error_303 = http_error_302
    http_error_307 = http_error_302


_opener = urllib.request.build_opener(_NoRedirect())


def _call(path, method="GET", cookie=None, body=None):
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = f"vf_session={cookie}"
    req = urllib.request.Request(
        BASE + path, method=method, headers=headers,
        data=(json.dumps(body).encode() if body is not None else None),
    )
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
    print("  VF-322 SELF-TEST — Proxy Admin read-only surface")
    print("=" * 66)

    # Borrow an SU
    su = db.query(User).filter(User.role == "super_user", User.status == "active").first()
    _ok(su is not None, "active SU exists")

    # Two cookies: plain SU + elevated SU
    now = datetime.now(timezone.utc)
    cookie_su = str(uuid.uuid4())
    db.add(UserSession(id=cookie_su, user_id=su.id, session_type="user",
                       created_at=now, expires_at=now + timedelta(hours=1)))
    cookie_su_elev = str(uuid.uuid4())
    db.add(UserSession(id=cookie_su_elev, user_id=su.id, session_type="user",
                       created_at=now, expires_at=now + timedelta(hours=1),
                       elevated_until=now + timedelta(minutes=15)))
    db.commit()

    # ── Unauthenticated rejections ──
    _section("auth gates")
    s, _, _ = _call("/api/v2/proxy/config")
    _ok(s == 401, f"/config no-cookie → 401 (got {s})")
    s, _, _ = _call("/api/v2/proxy/rules")
    _ok(s == 401, f"/rules no-cookie → 401 (got {s})")
    s, _, _ = _call("/api/v2/proxy/cert-info")
    _ok(s == 401, f"/cert-info no-cookie → 401 (got {s})")
    s, _, _ = _call("/api/v2/proxy/reload", method="POST")
    _ok(s in (401, 403), f"/reload no-cookie → 401 or 403 (got {s})")

    # ── Plain SU can read, cannot reload ──
    _section("SU read-only access")
    s, body, _ = _call("/api/v2/proxy/config", cookie=cookie_su)
    _ok(s == 200, f"/config SU → 200 (got {s})")
    cfg = json.loads(body)
    _ok("apps" in cfg and "http" in cfg.get("apps", {}),
        "config has apps.http (Caddy JSON shape)")

    s, body, _ = _call("/api/v2/proxy/rules", cookie=cookie_su)
    _ok(s == 200, f"/rules SU → 200 (got {s})")
    d = json.loads(body)
    rules = d.get("rules", [])
    _ok(isinstance(rules, list) and len(rules) > 0,
        f"rules is non-empty list (got {len(rules)} entries)")
    _ok(any(r.get("upstream", "").startswith("app:") for r in rules),
        "at least one rule points at app:8000")

    s, body, _ = _call("/api/v2/proxy/cert-info", cookie=cookie_su)
    _ok(s == 200, f"/cert-info SU → 200 (got {s})")
    info = json.loads(body)
    _ok(info.get("mode") in ("file", "acme", "caddy_internal", "self_signed"),
        f"cert mode recognised: {info.get('mode')}")
    _ok(info.get("days_remaining") is not None,
        f"days_remaining computed: {info.get('days_remaining')}")

    s, body, _ = _call("/api/v2/proxy/reload", method="POST", cookie=cookie_su)
    _ok(s == 403, f"/reload plain SU (no elevation) → 403 (got {s})")

    # ── CA bundle behaviour depends on mode ──
    _section("CA bundle endpoint")
    s, body, _ = _call("/api/v2/proxy/ca-bundle", cookie=cookie_su)
    if info.get("mode") == "caddy_internal":
        _ok(s == 200, f"/ca-bundle (caddy_internal) → 200 (got {s})")
        _ok(body.startswith("-----BEGIN"), "CA bundle body is PEM")
    else:
        _ok(s == 404, f"/ca-bundle (non-internal mode) → 404 (got {s})")

    # ── Elevated SU can reload ──
    _section("Elevated SA reload")
    pre_events = (db.query(ActivityEvent)
                  .filter(ActivityEvent.action == "proxy_reloaded")
                  .count())
    s, body, _ = _call("/api/v2/proxy/reload", method="POST", cookie=cookie_su_elev)
    _ok(s == 200, f"/reload elevated-SU → 200 (got {s})")
    d = json.loads(body) if body else {}
    _ok(d.get("ok") is True, "reload response carries ok=true")
    _ok(d.get("reloaded_at") is not None, "reload response carries timestamp")
    db.expire_all()
    post_events = (db.query(ActivityEvent)
                   .filter(ActivityEvent.action == "proxy_reloaded")
                   .count())
    _ok(post_events == pre_events + 1,
        f"audit event emitted (proxy_reloaded count {pre_events}→{post_events})")

    # ── VF-323 / T2 — cert actions ──
    _section("T2 — cert renew/export (SA actions)")
    s, body, _ = _call("/api/v2/proxy/cert/renew", method="POST", cookie=cookie_su)
    _ok(s == 403, f"/cert/renew plain SU → 403 (got {s})")
    s, body, _ = _call("/api/v2/proxy/cert/export", cookie=cookie_su)
    _ok(s == 403, f"/cert/export plain SU → 403 (got {s})")

    # Renew on a file-based cert should 422 with a clear message
    if info.get("mode") in ("file", "self_signed"):
        s, body, _ = _call("/api/v2/proxy/cert/renew", method="POST", cookie=cookie_su_elev)
        _ok(s == 422, f"/cert/renew on file-mode elevated-SA → 422 (got {s})")

    s, body, hdrs = _call("/api/v2/proxy/cert/export", cookie=cookie_su_elev)
    _ok(s == 200, f"/cert/export elevated-SA → 200 (got {s})")
    _ok(body.startswith("-----BEGIN"), "exported body is PEM content")
    _ok("attachment" in (hdrs.get("content-disposition", "") or ""),
        "Content-Disposition attachment header present")

    # ── VF-325 / T4 — any user can see cert info + CA bundle ──
    _section("T4 — user-facing cert-info + ca-bundle (any authenticated user)")
    # Need a plain (non-SU) user cookie. Fallback: if no such user exists, skip.
    plain_user = db.query(User).filter(User.role == "user", User.status == "active").first()
    if plain_user:
        cookie_user = str(uuid.uuid4())
        db.add(UserSession(id=cookie_user, user_id=plain_user.id, session_type="user",
                           created_at=now, expires_at=now + timedelta(hours=1)))
        db.commit()
        s, body, _ = _call("/api/v2/proxy/cert-info", cookie=cookie_user)
        _ok(s == 200, f"/cert-info plain user → 200 (got {s})")
        s, body, _ = _call("/api/v2/proxy/config", cookie=cookie_user)
        _ok(s == 403, f"/config plain user → 403 (got {s} — still SU+ only)")
        db.query(UserSession).filter(UserSession.id == cookie_user).delete(synchronize_session=False)
        db.commit()
    else:
        _ok(True, "no plain user on this env — skipping T4 plain-user checks")

    # ── VF-324 / T3 — Health upstreams endpoint reachable through health container ──
    _section("T3 — Health container exposes caddy-upstreams")
    try:
        import urllib.request as _u
        r = _u.urlopen("http://health:9090/api/health/caddy-upstreams", timeout=5)
        d = json.loads(r.read())
        _ok(d.get("status") == "ok", f"caddy-upstreams status ok (got {d.get('status')})")
        _ok(isinstance(d.get("upstreams"), list) and len(d["upstreams"]) > 0,
            f"upstreams list populated ({len(d.get('upstreams', []))} items)")
    except Exception as e:
        _ok(False, f"caddy-upstreams reachable: {e}")

    # ── UI page rendering ──
    _section("/ui/admin/proxy page")
    s, body, _ = _call("/ui/admin/proxy")
    _ok(s == 302, f"unauthenticated → 302 (got {s})")

    s, body, _ = _call("/ui/admin/proxy", cookie=cookie_su)
    _ok(s == 200, f"SU → 200 (got {s})")
    _ok("Proxy" in body and "Admin" in body, "page title present")
    _ok("pa-role-su" in body, "SU role pill rendered (read-only mode)")
    _ok("disabled" in body, "reload button disabled for non-SA")

    s, body, _ = _call("/ui/admin/proxy", cookie=cookie_su_elev)
    _ok(s == 200, f"elevated SU → 200 (got {s})")
    _ok("pa-role-sa" in body, "SA role pill rendered (write mode)")
    _ok("Reload Proxy" in body, "reload button text is live (not padlocked)")

    # Cleanup
    print("\n[cleanup]")
    db.query(UserSession).filter(UserSession.id.in_([cookie_su, cookie_su_elev])).delete(synchronize_session=False)
    # Remove the proxy_reloaded audit event the elevated-SU reload created
    db.query(ActivityEvent).filter(
        ActivityEvent.action == "proxy_reloaded",
        ActivityEvent.actor_user_id == su.id,
        ActivityEvent.created_at >= now,
    ).delete(synchronize_session=False)
    db.commit()
    print("  sessions + test audit event cleaned")

    print("\n" + "=" * 66)
    if FAILS == 0:
        print("  ALL CHECKS GREEN")
    else:
        print(f"  {FAILS} / {CHECKS} FAILED")
    print("=" * 66)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
