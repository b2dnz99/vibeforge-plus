#!/usr/bin/env python3
"""
VF-303 self-test — server-streamed agent-token download.

Runs INSIDE the app container. Loopback only, no creds needed.

Scenarios:
  1. POST /admin/api/agents returns a download_url + token.
  2. GET the download_url → 200, Content-Disposition attachment, body == token + \\n.
  3. Second GET on the same nonce → 410 (single-use).
  4. Nonce with wrong agent_id → 404.
  5. Expired nonce → 410 (simulated by rewinding expires_at in DB).
  6. Cycle endpoint also returns a fresh download_url that works.

Cleans up the transient test agent and nonces.

    docker compose exec app python scripts/vf303_selftest.py
"""
import os, sys, json, uuid, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.models.user import User
from app.models.project import Project
from app.models.session import UserSession
from app.models.agent import Agent
from app.models.agent_token_download import AgentTokenDownload

DB_URL = os.environ.get("DATABASE_URL",
                        "postgresql+psycopg2://vibeforge:vibeforge@db:5432/vibeforge")
BASE = "http://localhost:8000"


def _call(path, cookie=None, method="GET", body=None, cookie_name="vf_sa_session"):
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = f"{cookie_name}={cookie}"
    req = urllib.request.Request(BASE + path, method=method, headers=headers,
                                 data=(json.dumps(body).encode() if body is not None else None))
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _ok(c, m):
    print(("  OK   " if c else "  FAIL ") + m)
    if not c:
        raise AssertionError(m)


def main():
    engine = create_engine(DB_URL)
    Session = sessionmaker(bind=engine)
    db = Session()

    print("=" * 66)
    print("  VF-303 SELF-TEST — server-streamed agent-token download")
    print("=" * 66)

    sa = db.query(User).filter(User.role == "super_admin", User.status == "active").first()
    _ok(sa is not None, "SA user exists")

    # Transient SA session
    sa_sid = str(uuid.uuid4())
    db.add(UserSession(id=sa_sid, user_id=sa.id, session_type="sa",
                       created_at=datetime.now(timezone.utc),
                       expires_at=datetime.now(timezone.utc) + timedelta(minutes=15)))
    db.commit()

    # Pick any eligible user to own the test agent (first SU or user)
    owner = db.query(User).filter(User.role.in_(("super_user", "user")),
                                  User.status == "active").first()
    _ok(owner is not None, "at least one eligible owner (SU/user)")

    # Pick any active project
    proj = db.query(Project).filter(Project.status == "active").first()
    _ok(proj is not None, "at least one active project")

    agent_id = None
    try:
        tag = uuid.uuid4().hex[:4]
        agent_name = f"vf303-{tag}"

        # === 1. Create agent → response has token + download_url ===
        print("\n[1] POST /admin/api/agents → token + download_url in response")
        code, headers, raw = _call("/admin/api/agents", sa_sid, method="POST", body={
            "name": agent_name, "project_slug": proj.slug,
            "model_type": "claude", "description": "vf303 test agent",
            "target_user_id": owner.id,
        })
        _ok(code == 201, f"HTTP {code}")
        d = json.loads(raw)
        token = d.get("token")
        durl = d.get("download_url")
        agent_id = d.get("id")
        _ok(bool(token and token.startswith("vf_")), "token in response, vf_ prefix")
        _ok(bool(durl), f"download_url present: {durl}")
        _ok(durl.startswith(f"/ui/api/agents/{agent_id}/token-file?nonce="),
            "download_url points to token-file endpoint")

        # === 2. GET download_url → 200, attachment, body == token + \n ===
        print("\n[2] GET download_url → 200 attachment with token body")
        code, headers, raw = _call(durl)
        _ok(code == 200, f"HTTP {code}")
        cd = headers.get("content-disposition") or headers.get("Content-Disposition") or ""
        _ok("attachment" in cd.lower() and "agent-token.txt" in cd,
            f"Content-Disposition is attachment filename=agent-token.txt (got {cd!r})")
        body_text = raw.decode()
        _ok(body_text == token + "\n",
            f"body equals token + newline (len={len(body_text)}, token_len={len(token)})")

        # === 3. Second GET on same nonce → 410 ===
        print("\n[3] Second GET on same nonce → 410 (single-use)")
        code, _, _ = _call(durl)
        _ok(code == 410, f"HTTP {code} (expected 410)")

        # === 4. Create another agent, wrong-agent-id against the nonce → 404 ===
        print("\n[4] Mismatched agent_id on nonce → 404")
        code2, _, raw2 = _call("/admin/api/agents", sa_sid, method="POST", body={
            "name": f"vf303b-{tag}", "project_slug": proj.slug,
            "model_type": "claude", "description": "vf303 agent 2",
            "target_user_id": owner.id,
        })
        _ok(code2 == 201, f"second create HTTP {code2}")
        d2 = json.loads(raw2)
        wrong_path = d2["download_url"].replace(d2["id"], agent_id)
        code, _, _ = _call(wrong_path)
        _ok(code == 404, f"mismatched agent_id → HTTP {code} (expected 404)")
        # Clean up the 2nd agent + its nonce (still valid for the RIGHT agent)
        db.execute(text("DELETE FROM agent_token_downloads WHERE agent_id = :a"),
                   {"a": d2["id"]})
        db.execute(text("DELETE FROM project_members WHERE agent_id = :a"),
                   {"a": d2["id"]})
        db.execute(text("DELETE FROM agents WHERE id = :a"), {"a": d2["id"]})
        db.commit()

        # === 5. Expired nonce → 410 ===
        print("\n[5] Expired nonce → 410")
        # Create a 3rd agent, rewind its row's expires_at, hit the endpoint
        code3, _, raw3 = _call("/admin/api/agents", sa_sid, method="POST", body={
            "name": f"vf303c-{tag}", "project_slug": proj.slug,
            "model_type": "claude", "description": "vf303 agent 3 (expired)",
            "target_user_id": owner.id,
        })
        _ok(code3 == 201, f"third create HTTP {code3}")
        d3 = json.loads(raw3)
        # Rewind expires_at to 1 minute ago
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.execute(text("UPDATE agent_token_downloads SET expires_at = :p WHERE agent_id = :a"),
                   {"p": past, "a": d3["id"]})
        db.commit()
        code, _, _ = _call(d3["download_url"])
        _ok(code == 410, f"expired → HTTP {code} (expected 410)")
        # Clean up agent 3
        db.execute(text("DELETE FROM agent_token_downloads WHERE agent_id = :a"),
                   {"a": d3["id"]})
        db.execute(text("DELETE FROM project_members WHERE agent_id = :a"),
                   {"a": d3["id"]})
        db.execute(text("DELETE FROM agents WHERE id = :a"), {"a": d3["id"]})
        db.commit()

        # === 6. Cycle endpoint also returns a fresh working download_url ===
        print("\n[6] Cycle token → fresh download_url that works")
        code, _, raw = _call(f"/admin/api/agents/{agent_id}/cycle", sa_sid, method="POST", body={})
        _ok(code == 200, f"cycle HTTP {code}")
        d = json.loads(raw)
        cyc_url = d.get("download_url")
        cyc_tok = d.get("token")
        _ok(bool(cyc_url), "cycle returned download_url")
        code, headers, raw = _call(cyc_url)
        _ok(code == 200, f"GET cycle download_url HTTP {code}")
        _ok(raw.decode() == cyc_tok + "\n", "cycle download body matches cycled token")

        # === 7. Plaintext scrub on consume ===
        print("\n[7] After consume, token_plaintext is wiped in DB")
        db.expire_all()
        rows = db.query(AgentTokenDownload).filter(AgentTokenDownload.agent_id == agent_id).all()
        _ok(len(rows) >= 1, "at least one row exists for this agent")
        consumed = [r for r in rows if r.consumed_at is not None]
        _ok(all(r.token_plaintext == "" for r in consumed),
            "consumed rows have empty token_plaintext")

        print("\n" + "=" * 66)
        print("  ALL CHECKS GREEN")
        print("=" * 66)
        return 0

    finally:
        if agent_id:
            db.execute(text("DELETE FROM agent_token_downloads WHERE agent_id = :a"),
                       {"a": agent_id})
            db.execute(text("DELETE FROM project_members WHERE agent_id = :a"),
                       {"a": agent_id})
            db.execute(text("DELETE FROM activity_events WHERE details LIKE :lk"),
                       {"lk": f"%{agent_id}%"})
            db.execute(text("DELETE FROM agents WHERE id = :a"), {"a": agent_id})
        db.execute(text("DELETE FROM sessions WHERE id = :s"), {"s": sa_sid})
        db.commit()
        db.close()


if __name__ == "__main__":
    sys.exit(main())
