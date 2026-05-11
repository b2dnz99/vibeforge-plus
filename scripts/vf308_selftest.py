"""VF-308 self-test — owner transfer + soft-delete guardrail. DEV-only."""
import json
import os
import sys
import uuid
from datetime import datetime, timezone

import bcrypt
from sqlalchemy import text

from app.db.session import SessionLocal


def run():
    print("=" * 60)
    print("VF-308 self-test")
    print("=" * 60)

    # Locate the SA (we'll need to simulate SA-gated calls via direct endpoint invocation.
    # For this self-test we call the transfer logic via the API path using a synthesised
    # SA session. Simpler: we hit the endpoint through a cookie jar after seeding a
    # session row directly.
    with SessionLocal() as db:
        sa = db.execute(text("SELECT id FROM users WHERE role='super_admin' LIMIT 1")).fetchone()
    if not sa:
        print("FAIL: no SA on this env")
        return 1
    sa_id = sa[0]

    # Create an SA session row so our HTTP calls can auth as SA
    sa_sess = str(uuid.uuid4())
    with SessionLocal() as db:
        db.execute(text("""
            INSERT INTO sessions (id, user_id, session_type, expires_at, ip_address, user_agent)
            VALUES (:id, :uid, 'sa', :exp, '127.0.0.1', 'vf308-selftest')
        """), {"id": sa_sess, "uid": sa_id, "exp": datetime.now(timezone.utc).replace(microsecond=0).isoformat()})
        # fix: expires_at needs to be in the future
        from datetime import timedelta
        db.execute(text("UPDATE sessions SET expires_at = :e WHERE id = :id"),
                   {"e": datetime.now(timezone.utc) + timedelta(minutes=30), "id": sa_sess})
        db.commit()
    print(f"[setup] SA session created: {sa_sess[:8]}...")

    # Create two test users + a test project with existing members
    alice_id = str(uuid.uuid4())
    bob_id = str(uuid.uuid4())
    viewer_id = str(uuid.uuid4())
    project_id = str(uuid.uuid4())
    proj_slug = f"vf308-test-{uuid.uuid4().hex[:8]}"
    pw = bcrypt.hashpw(b"Vf308Test!123", bcrypt.gensalt()).decode()
    now = datetime.now(timezone.utc)

    try:
        with SessionLocal() as db:
            # Users
            db.execute(text("""
                INSERT INTO users (id, username, email, display_name, role, status, password_hash, created_at, updated_at)
                VALUES
                  (:a, 'vf308alice', 'vf308alice@t.local', 'Alice VF-308', 'user',       'active', :pw, :now, :now),
                  (:b, 'vf308bob',   'vf308bob@t.local',   'Bob VF-308',   'user',       'active', :pw, :now, :now),
                  (:v, 'vf308vue',   'vf308vue@t.local',   'Viewer VF-308','viewer',     'active', :pw, :now, :now)
            """), {"a": alice_id, "b": bob_id, "v": viewer_id, "pw": pw, "now": now})
            # Project owned by Alice
            db.execute(text("""
                INSERT INTO projects (id, slug, name, description, status, owner_id, created_by_user_id, resume_summary, lifecycle_log, agentic_dev, pinned, pin_order, card_order, created_at, updated_at)
                VALUES (:id, :slug, 'VF-308 Test Project', '', 'active', :owner, :owner, '', '[]', false, false, 0, 0, :now, :now)
            """), {"id": project_id, "slug": proj_slug, "owner": alice_id, "now": now})
            # ProjectMember rows: Alice owner, Bob admin, Viewer read
            for (mid, uid, role) in [
                (str(uuid.uuid4()), alice_id, "admin"),
                (str(uuid.uuid4()), bob_id, "admin"),
                (str(uuid.uuid4()), viewer_id, "read"),
            ]:
                db.execute(text("""
                    INSERT INTO project_members (id, project_id, user_id, role, created_at)
                    VALUES (:mid, :pid, :uid, :r, :now)
                """), {"mid": mid, "pid": project_id, "uid": uid, "r": role, "now": now})
            db.commit()
        print(f"[setup] Alice (owner), Bob (admin member), Viewer on {proj_slug}")

        # --- Test HTTP calls ---
        import http.cookiejar
        import urllib.request
        import urllib.error

        def call(method, path, body=None):
            data = json.dumps(body).encode() if body is not None else None
            headers = {
                "Content-Type": "application/json",
                "Cookie": f"vf_sa_session={sa_sess}",
            }
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

        # 1. Eligible-new-owners returns Bob (admin human) but not Alice (current owner) or Viewer (viewer role)
        status, body = call("GET", f"/admin/api/projects/{proj_slug}/eligible-new-owners")
        if status != 200:
            print(f"FAIL: eligible-new-owners returned {status}: {body}")
            return 1
        eligible_ids = {row["user_id"] for row in body}
        if bob_id not in eligible_ids:
            print(f"FAIL: eligible list missing Bob ({eligible_ids})")
            return 1
        if alice_id in eligible_ids:
            print(f"FAIL: eligible list includes current owner Alice ({eligible_ids})")
            return 1
        if viewer_id in eligible_ids:
            print(f"FAIL: eligible list includes a Viewer ({eligible_ids})")
            return 1
        print(f"[ok] eligible-new-owners: Bob included; Alice+Viewer excluded")

        # 2. Transfer to Viewer → 422
        status, body = call("POST", f"/admin/api/projects/{proj_slug}/transfer-owner",
                            {"new_owner_user_id": viewer_id})
        if status != 422:
            print(f"FAIL: transfer to viewer expected 422, got {status}: {body}")
            return 1
        print(f"[ok] transfer to viewer rejected with 422")

        # 3. Self-transfer → 422
        status, body = call("POST", f"/admin/api/projects/{proj_slug}/transfer-owner",
                            {"new_owner_user_id": alice_id})
        if status != 422:
            print(f"FAIL: self-transfer expected 422, got {status}: {body}")
            return 1
        print(f"[ok] self-transfer rejected with 422")

        # 4. Transfer to SA (super_admin) → 422
        status, body = call("POST", f"/admin/api/projects/{proj_slug}/transfer-owner",
                            {"new_owner_user_id": sa_id})
        if status != 422:
            print(f"FAIL: transfer to SA expected 422, got {status}: {body}")
            return 1
        print(f"[ok] transfer to super_admin rejected with 422")

        # 5. Pre-transfer soft-delete of Alice → 422 OWNED_PROJECTS_BLOCKING
        status, body = call("POST", f"/admin/api/users/{alice_id}/soft-delete")
        if status != 422:
            print(f"FAIL: soft-delete of owner expected 422, got {status}: {body}")
            return 1
        detail = body.get("detail", {}) if isinstance(body, dict) else {}
        if not isinstance(detail, dict) or detail.get("code") != "OWNED_PROJECTS_BLOCKING":
            print(f"FAIL: 422 detail missing OWNED_PROJECTS_BLOCKING code: {detail}")
            return 1
        if not any(p["slug"] == proj_slug for p in detail.get("projects", [])):
            print(f"FAIL: blocking projects list missing our test project: {detail}")
            return 1
        print(f"[ok] soft-delete of owner blocked with OWNED_PROJECTS_BLOCKING + project list")

        # 6. Transfer to Bob → 200
        status, body = call("POST", f"/admin/api/projects/{proj_slug}/transfer-owner",
                            {"new_owner_user_id": bob_id})
        if status != 200:
            print(f"FAIL: transfer to Bob expected 200, got {status}: {body}")
            return 1
        with SessionLocal() as db:
            updated = db.execute(text(
                "SELECT owner_id FROM projects WHERE id = :id"
            ), {"id": project_id}).fetchone()
            if not updated or updated[0] != bob_id:
                print(f"FAIL: project.owner_id did not update to Bob: {updated}")
                return 1
            # Bob's PM role upgraded to admin (was already admin, so still admin — check it)
            bob_pm = db.execute(text(
                "SELECT role FROM project_members WHERE project_id = :p AND user_id = :u"
            ), {"p": project_id, "u": bob_id}).fetchone()
            if not bob_pm or bob_pm[0] != "admin":
                print(f"FAIL: Bob's PM row role not admin: {bob_pm}")
                return 1
            # Alice's PM row still admin (unchanged)
            alice_pm = db.execute(text(
                "SELECT role FROM project_members WHERE project_id = :p AND user_id = :u"
            ), {"p": project_id, "u": alice_id}).fetchone()
            if not alice_pm or alice_pm[0] != "admin":
                print(f"FAIL: Alice's PM row changed unexpectedly: {alice_pm}")
                return 1
            # Audit event written
            evt = db.execute(text("""
                SELECT action, details FROM activity_events
                WHERE action='project_owner_transferred'
                  AND details LIKE :p
                ORDER BY created_at DESC LIMIT 1
            """), {"p": f"%{proj_slug}%"}).fetchone()
            if not evt:
                print("FAIL: audit event project_owner_transferred not written")
                return 1
        print(f"[ok] transfer succeeded: owner_id=Bob, roles preserved, audit event written")

        # 7. Post-transfer soft-delete of Alice (she no longer owns anything active) → 200
        status, body = call("POST", f"/admin/api/users/{alice_id}/soft-delete")
        if status != 200:
            print(f"FAIL: soft-delete of Alice post-transfer expected 200, got {status}: {body}")
            return 1
        print(f"[ok] soft-delete of Alice succeeds after ownership transferred away")

    finally:
        # Cleanup — delete everything we created
        with SessionLocal() as db:
            db.execute(text("DELETE FROM activity_events WHERE details LIKE :p"), {"p": f"%{proj_slug}%"})
            db.execute(text("DELETE FROM project_members WHERE project_id = :p"), {"p": project_id})
            db.execute(text("DELETE FROM projects WHERE id = :p"), {"p": project_id})
            db.execute(text("DELETE FROM users WHERE id IN (:a, :b, :v)"),
                       {"a": alice_id, "b": bob_id, "v": viewer_id})
            db.execute(text("DELETE FROM sessions WHERE id = :s"), {"s": sa_sess})
            db.commit()
        print(f"[cleanup] test users, project, PM rows, audit events, SA session all removed")

    print("=" * 60)
    print("ALL CHECKS PASSED")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(run())
