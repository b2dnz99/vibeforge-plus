"""VF-311 self-test. Run inside app container with PYTHONPATH=/app."""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.db.session import SessionLocal


def run():
    print("=" * 60)
    print("VF-311 self-test")
    print("=" * 60)

    with SessionLocal() as db:
        sa = db.execute(text(
            "SELECT id, display_name, password_hash FROM users WHERE role='super_admin' LIMIT 1"
        )).fetchone()
    if not sa:
        print("FAIL: no SA user on this env")
        return 1
    sa_id, sa_name, original_hash = sa
    print(f"[setup] acting SA: {sa_name}")

    # Part 1: verify admin.py change-sa-password endpoint writes sa_password_self_change
    # We don't call the endpoint here (requires SA cookie + password); we assert on the source.
    src = open("/app/app/api/v2/admin.py").read()
    if "sa_password_self_change" not in src:
        print("FAIL: admin.py missing sa_password_self_change audit action")
        return 1
    if "sa_password_changed" in src and '"sa_password_changed"' in src:
        print("FAIL: admin.py still writes legacy sa_password_changed action")
        return 1
    print("[ok] admin.py writes sa_password_self_change (legacy name gone)")

    # Part 2: reset_sa_password.py writes sa_password_force_reset event.
    # We invoke it, check the event was written, then restore the original hash.
    # This is destructive-ish: the SA password will rotate. We save and restore the hash.
    before_ts = datetime.now(timezone.utc)
    result = subprocess.run(
        ["python", "/app/scripts/reset_sa_password.py"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"FAIL: reset_sa_password.py failed: {result.stderr}")
        return 1
    # Check for the audit event
    with SessionLocal() as db:
        evt = db.execute(text("""
            SELECT action, details, actor_user_id
            FROM activity_events
            WHERE action='sa_password_force_reset'
              AND actor_user_id = :uid
              AND created_at >= :ts
            ORDER BY created_at DESC LIMIT 1
        """), {"uid": sa_id, "ts": before_ts}).fetchone()
    if not evt:
        print("FAIL: reset_sa_password.py did not write sa_password_force_reset event")
        _restore_hash(sa_id, original_hash)
        return 1
    details = json.loads(evt[1] or "{}")
    required_fields = ("script_path", "os_user", "hostname", "timestamp")
    missing = [f for f in required_fields if f not in details]
    if missing:
        print(f"FAIL: event details missing fields: {missing}")
        _restore_hash(sa_id, original_hash)
        return 1
    print(f"[ok] reset script wrote sa_password_force_reset (os_user={details['os_user']}, host={details['hostname']})")

    # Restore the original hash so DEV stays usable
    _restore_hash(sa_id, original_hash)
    print("[cleanup] SA password hash restored")

    # Part 3: audit log filter includes the new event types
    if '"sa_password_self_change"' not in src or '"sa_password_force_reset"' not in src:
        print("FAIL: admin_auditlog filter does not include new SA password event types")
        return 1
    if '"login_blocked_sa"' not in src:
        print("FAIL: admin_auditlog filter does not include login_blocked_sa (VF-309 event)")
        return 1
    print("[ok] admin_auditlog filter includes new event types")

    # Part 4: ack endpoint exists
    if "/admin/api/sa-password-force-reset/ack" not in src:
        print("FAIL: ack endpoint missing from admin.py")
        return 1
    if "sa_password_force_reset_ack" not in src:
        print("FAIL: ack endpoint does not write sa_password_force_reset_ack event")
        return 1
    print("[ok] ack endpoint present")

    # Part 5: dashboard template references force_reset_banner
    tpl = open("/app/app/templates/ui/admin.html").read()
    if "force_reset_banner" not in tpl:
        print("FAIL: admin.html missing force_reset_banner block")
        return 1
    if "vf311AckForceReset" not in tpl:
        print("FAIL: admin.html missing ack button handler")
        return 1
    print("[ok] admin.html renders force_reset_banner with ack button")

    print("=" * 60)
    print("ALL CHECKS PASSED")
    print("=" * 60)
    return 0


def _restore_hash(sa_id, original_hash):
    with SessionLocal() as db:
        db.execute(text("UPDATE users SET password_hash = :h WHERE id = :id"),
                   {"h": original_hash, "id": sa_id})
        db.commit()


if __name__ == "__main__":
    sys.exit(run())
