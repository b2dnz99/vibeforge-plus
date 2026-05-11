"""VF-341 — Session policy + lifetime knob accessors.

Single source of truth for the 6 operator-tunable knobs from
SESSION-LIFECYCLE-PROPOSAL §3. All session-creation / token-validation /
heartbeat / sweep sites read from here so the policy can be changed at
runtime via the Session Policy workspace (Phase 2) without restarts.

Constants below are the documented defaults + bounds. Settings keys use
dot.notation so future grouping by prefix stays clean. Reads go through
admin_experimental.get_int_setting which clamps to bounds defensively.

NOT a class — module-level functions only. The session lifetimes layer is
not stateful enough to need an instance.
"""
from sqlalchemy.orm import Session

from app.api.v2.admin_experimental import get_int_setting


# ── Knob defaults + bounds (mirror proposal §3) ────────────────────────────

# Board session sliding window — extension applied per heartbeat.
SLIDE_WINDOW_HOURS_DEFAULT = 24
SLIDE_WINDOW_HOURS_MIN = 1
SLIDE_WINDOW_HOURS_MAX = 7 * 24

# Board session absolute cap — hard ceiling from created_at.
ABSOLUTE_CAP_DAYS_DEFAULT = 7
ABSOLUTE_CAP_DAYS_MIN = 1
ABSOLUTE_CAP_DAYS_MAX = 30

# Elevation TTL — applies to BOTH SA and SU per §3.1 (one knob, expire-together).
ELEVATION_TTL_MINUTES_DEFAULT = 15
ELEVATION_TTL_MINUTES_MIN = 5
ELEVATION_TTL_MINUTES_MAX = 60

# Concurrent active sessions per user per session_type.
CONCURRENT_CAP_DEFAULT = 5
CONCURRENT_CAP_MIN = 1
CONCURRENT_CAP_MAX = 10

# Agent token TTL — applies on issuance + on validation (existing column).
TOKEN_TTL_DAYS_DEFAULT = 90
TOKEN_TTL_DAYS_MIN = 7
TOKEN_TTL_DAYS_MAX = 365

# Auto-revoke-stale (sessions only — tokens excluded by design per §3.5).
# 0 = disabled.
AUTO_REVOKE_STALE_DAYS_DEFAULT = 30
AUTO_REVOKE_STALE_DAYS_MIN = 0
AUTO_REVOKE_STALE_DAYS_MAX = 180


# ── Settings keys (dot.notation; all int-valued) ───────────────────────────

KEY_SLIDE_WINDOW_HOURS = "session.slide_window_hours"
KEY_ABSOLUTE_CAP_DAYS = "session.absolute_cap_days"
KEY_ELEVATION_TTL_MINUTES = "session.elevation_ttl_minutes"
KEY_CONCURRENT_CAP = "session.concurrent_cap"
KEY_TOKEN_TTL_DAYS = "token.ttl_days"
KEY_AUTO_REVOKE_STALE_DAYS = "session.auto_revoke_stale_days"


# ── Read accessors ─────────────────────────────────────────────────────────

def slide_window_hours(db: Session) -> int:
    return get_int_setting(db, KEY_SLIDE_WINDOW_HOURS,
        SLIDE_WINDOW_HOURS_DEFAULT, SLIDE_WINDOW_HOURS_MIN, SLIDE_WINDOW_HOURS_MAX)


def absolute_cap_days(db: Session) -> int:
    return get_int_setting(db, KEY_ABSOLUTE_CAP_DAYS,
        ABSOLUTE_CAP_DAYS_DEFAULT, ABSOLUTE_CAP_DAYS_MIN, ABSOLUTE_CAP_DAYS_MAX)


def elevation_ttl_minutes(db: Session) -> int:
    return get_int_setting(db, KEY_ELEVATION_TTL_MINUTES,
        ELEVATION_TTL_MINUTES_DEFAULT, ELEVATION_TTL_MINUTES_MIN, ELEVATION_TTL_MINUTES_MAX)


def concurrent_cap(db: Session) -> int:
    return get_int_setting(db, KEY_CONCURRENT_CAP,
        CONCURRENT_CAP_DEFAULT, CONCURRENT_CAP_MIN, CONCURRENT_CAP_MAX)


def token_ttl_days(db: Session) -> int:
    return get_int_setting(db, KEY_TOKEN_TTL_DAYS,
        TOKEN_TTL_DAYS_DEFAULT, TOKEN_TTL_DAYS_MIN, TOKEN_TTL_DAYS_MAX)


def auto_revoke_stale_days(db: Session) -> int:
    """Returns 0 when disabled. Sweep helpers must short-circuit on 0."""
    return get_int_setting(db, KEY_AUTO_REVOKE_STALE_DAYS,
        AUTO_REVOKE_STALE_DAYS_DEFAULT, AUTO_REVOKE_STALE_DAYS_MIN, AUTO_REVOKE_STALE_DAYS_MAX)


# ── Helpers used at session-creation + sweep sites ─────────────────────────

def enforce_concurrent_cap(db: Session, user_id: str, session_type: str) -> int:
    """Per SESSION-LIFECYCLE-PROPOSAL §4.4 + §6.5: before inserting a new
    session, count active rows for (user, session_type). If count >= cap,
    auto-revoke the oldest until under cap. Returns the count of rows
    auto-revoked (0 when under cap; usually 1 when exactly at cap).

    Cap is per-session-type (board vs sa) so an SA elevation doesn't push
    the operator's board sessions over the limit.
    """
    from datetime import datetime, timezone
    from app.models.session import UserSession
    now = datetime.now(timezone.utc)
    cap = concurrent_cap(db)
    active = (
        db.query(UserSession)
        .filter(
            UserSession.user_id == user_id,
            UserSession.session_type == session_type,
            UserSession.expires_at > now,
            UserSession.revoked_at.is_(None),
        )
        .order_by(UserSession.created_at.asc())
        .all()
    )
    revoked = 0
    while len(active) >= cap:
        oldest = active.pop(0)
        oldest.revoked_at = now
        oldest.revoke_reason = "auto-cap"
        revoked += 1
    if revoked:
        db.flush()
    return revoked


def slide_session_expiry(sess, now=None):
    """Per §4.2: expires_at = min(now + slide, created_at + cap). Mutates
    the session in place; caller commits. Pure for board sessions —
    elevation cookies have their own renewal via elevation_ttl_minutes.

    Reads slide + cap fresh per call so policy changes take effect on the
    next heartbeat without a restart.
    """
    from datetime import datetime, timezone, timedelta
    from sqlalchemy.orm import object_session
    if now is None:
        now = datetime.now(timezone.utc)
    db = object_session(sess)
    if db is None:
        return  # detached row; caller messed up
    slide_h = slide_window_hours(db)
    cap_d = absolute_cap_days(db)
    sliding = now + timedelta(hours=slide_h)
    capped = sess.created_at + timedelta(days=cap_d)
    sess.expires_at = min(sliding, capped)


def classify_ip(ip_str: str) -> str:
    """Per §5.4: free signal from stdlib only. Returns one of:
    loopback / private / public / unknown. No geoip dep.
    """
    if not ip_str:
        return "unknown"
    from ipaddress import ip_address, ip_network
    try:
        ip = ip_address(ip_str)
    except (ValueError, TypeError):
        return "unknown"
    LOOPBACK = [ip_network("127.0.0.0/8"), ip_network("::1/128")]
    PRIVATE = [ip_network("10.0.0.0/8"), ip_network("172.16.0.0/12"),
               ip_network("192.168.0.0/16"), ip_network("fc00::/7")]
    if any(ip in n for n in LOOPBACK): return "loopback"
    if any(ip in n for n in PRIVATE): return "private"
    return "public"


def derive_session_state(sess, now=None) -> str:
    """Per §4.1: pure read-time derivation. Returns one of:
    revoked / expired / active / away / idle.

    No state column on the row — the auth filter (expires_at + revoked_at)
    is the truth; this is for display only.
    """
    from datetime import datetime, timezone, timedelta
    if now is None:
        now = datetime.now(timezone.utc)
    if sess.revoked_at is not None:
        return "revoked"
    if sess.expires_at <= now:
        return "expired"
    if sess.last_activity_at is None:
        return "idle"
    age = (now - sess.last_activity_at).total_seconds()
    if age < 5 * 60:
        return "active"
    if age < 30 * 60:
        return "away"
    return "idle"


def sweep_stale_sessions(db: Session) -> int:
    """Per §4.6: lazy-on-read sweep. Marks sessions as revoked when
    `last_activity_at < now - stale_days`. Returns the count revoked.

    SESSIONS ONLY — tokens are explicitly excluded per §3.5 / §4.6.
    Returns 0 when the knob is disabled (auto_revoke_stale_days == 0) or
    when there's nothing stale.
    """
    from datetime import datetime, timezone, timedelta
    from app.models.session import UserSession
    stale_days = auto_revoke_stale_days(db)
    if stale_days <= 0:
        return 0
    now = datetime.now(timezone.utc)
    threshold = now - timedelta(days=stale_days)
    n = (
        db.query(UserSession)
        .filter(
            UserSession.expires_at > now,
            UserSession.revoked_at.is_(None),
            UserSession.last_activity_at.isnot(None),
            UserSession.last_activity_at < threshold,
        )
        .update(
            {"revoked_at": now, "revoke_reason": "auto-stale"},
            synchronize_session=False,
        )
    )
    if n:
        db.commit()
    return n
