"""
Test fixtures helper — runs INSIDE the app container.
Invoked by scripts/test_fixtures.sh.

Usage (inside container):
  python /app/scripts/test_fixtures.py seed
  python /app/scripts/test_fixtures.py wipe
  python /app/scripts/test_fixtures.py status
"""
import sys
import uuid
import json
from datetime import datetime, timezone

import bcrypt
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.user import User
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.agent import Agent
from app.models.task import Task
from app.models.task_note import TaskNote
from app.models.milestone import Milestone
from app.models.phase import Phase
from app.models.activity import ActivityEvent

# ── Constants ──────────────────────────────────────────────
TEST_PROJECT_SLUG = "test-fixture"
TEST_PROJECT_NAME = "TEST: Fixture Project"
TEST_USERS = [
    # username, role, display_name, project_role (None = not a member)
    ("tsu",      "super_user", "TEST: Super User",   None),
    ("towner",   "user",       "TEST: Project Owner", "admin"),
    ("tmember",  "user",       "TEST: Member",       "write"),
    ("tview",    "viewer",     "TEST: Viewer",       "read"),
]
TEST_PASSWORD = "1234"
TEST_AGENT_NAME = "test-agent"
TEST_AGENT_SLUG = f"{TEST_PROJECT_SLUG}-{TEST_AGENT_NAME}"
TOKEN_FILE = "/app/scripts/.test-agent-token"


def hash_pw(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def now():
    return datetime.now(timezone.utc)


# ── STATUS ─────────────────────────────────────────────────
def status(db: Session):
    print("=" * 50)
    print("  Test Fixtures Status")
    print("=" * 50)
    print()
    print("USERS:")
    for username, _, _, _ in TEST_USERS:
        u = db.query(User).filter(User.username == username).first()
        mark = "[OK]" if u else "[--]"
        extra = f"({u.role}, {u.status})" if u else ""
        print(f"  {mark} {username:10} {extra}")
    print()
    p = db.query(Project).filter(Project.slug == TEST_PROJECT_SLUG).first()
    if p:
        owner = db.query(User).filter(User.id == p.owner_id).first()
        print(f"PROJECT: [OK] {p.name} (owner: {owner.username if owner else '?'})")
        tasks = db.query(Task).filter(Task.project_id == p.id).count()
        notes = db.query(TaskNote).join(Task).filter(Task.project_id == p.id).count()
        phases = db.query(Phase).filter(Phase.project_id == p.id).count()
        ms = db.query(Milestone).filter(Milestone.project_id == p.id).count()
        members = db.query(ProjectMember).filter(ProjectMember.project_id == p.id).count()
        print(f"  tasks={tasks} notes={notes} phases={phases} milestones={ms} members={members}")
    else:
        print(f"PROJECT: [--] {TEST_PROJECT_SLUG}")
    print()
    a = db.query(Agent).filter(Agent.slug == TEST_AGENT_SLUG).first()
    if a:
        print(f"AGENT:   [OK] {a.name} ({a.status})")
    else:
        print(f"AGENT:   [--] {TEST_AGENT_NAME}")
    print()


# ── SAFETY CHECK ───────────────────────────────────────────
def safety_check(db: Session) -> bool:
    """Refuse wipe if any non-test user has touched the test project."""
    p = db.query(Project).filter(Project.slug == TEST_PROJECT_SLUG).first()
    if not p:
        return True
    test_user_ids = {u.id for u in db.query(User).filter(User.username.in_(
        [t[0] for t in TEST_USERS]
    )).all()}
    # Activity events with non-test actors? Check actor_id_user
    events = db.query(ActivityEvent).filter(ActivityEvent.project_id == p.id).all()
    for e in events:
        details = {}
        try:
            details = json.loads(e.details) if e.details else {}
        except Exception:
            pass
        actor = details.get("actor", "")
        # If actor is not a test display_name, abort
        if actor and not actor.startswith("TEST:"):
            print(f"SAFETY: Found non-test actor '{actor}' in test project events.")
            print("Refusing to wipe — investigate first.")
            return False
    return True


# ── WIPE ───────────────────────────────────────────────────
def wipe(db: Session, force: bool = False):
    if not force and not safety_check(db):
        return False

    p = db.query(Project).filter(Project.slug == TEST_PROJECT_SLUG).first()
    if p:
        # FK-safe order
        db.query(TaskNote).filter(TaskNote.task_id.in_(
            db.query(Task.id).filter(Task.project_id == p.id)
        )).delete(synchronize_session=False)
        db.query(ActivityEvent).filter(ActivityEvent.project_id == p.id).delete(synchronize_session=False)
        db.query(Task).filter(Task.project_id == p.id).delete(synchronize_session=False)
        db.query(Phase).filter(Phase.project_id == p.id).delete(synchronize_session=False)
        db.query(Milestone).filter(Milestone.project_id == p.id).delete(synchronize_session=False)
        db.query(ProjectMember).filter(ProjectMember.project_id == p.id).delete(synchronize_session=False)
        db.query(Agent).filter(Agent.project_id == p.id).delete(synchronize_session=False)
        db.delete(p)
        print(f"  Deleted project {TEST_PROJECT_SLUG}")

    # Wipe test users (sessions first, FK)
    from app.models.session import UserSession
    for username, _, _, _ in TEST_USERS:
        u = db.query(User).filter(User.username == username).first()
        if u:
            db.query(UserSession).filter(UserSession.user_id == u.id).delete(synchronize_session=False)
            db.delete(u)
            print(f"  Deleted user {username}")

    db.commit()
    print("  Wipe complete.")
    return True


# ── SEED ───────────────────────────────────────────────────
def seed(db: Session):
    # Bail if any test user already exists
    existing = db.query(User).filter(User.username.in_([t[0] for t in TEST_USERS])).count()
    if existing > 0:
        print(f"  {existing} test user(s) already exist. Run wipe or cycle first.")
        return False
    if db.query(Project).filter(Project.slug == TEST_PROJECT_SLUG).first():
        print(f"  Project {TEST_PROJECT_SLUG} already exists. Run wipe or cycle first.")
        return False

    pw_hash = hash_pw(TEST_PASSWORD)

    # 1. Create users
    user_objs = {}
    for username, role, display_name, _ in TEST_USERS:
        u = User(
            id=str(uuid.uuid4()),
            username=username,
            email=f"{username}@test.local",
            display_name=display_name,
            role=role,
            password_hash=pw_hash,
            must_change_password=False,
            status="active",
        )
        db.add(u)
        user_objs[username] = u
        print(f"  Created user {username} ({role})")
    db.flush()

    # 2. Create project owned by towner
    project = Project(
        id=str(uuid.uuid4()),
        slug=TEST_PROJECT_SLUG,
        name=TEST_PROJECT_NAME,
        description="Throwaway project for auth/RBAC validation. Created by test_fixtures.py.",
        status="active",
        owner_id=user_objs["towner"].id,
        created_by_user_id=user_objs["towner"].id,
        agentic_dev=True,
        prefix="TF",
    )
    db.add(project)
    db.flush()
    print(f"  Created project {TEST_PROJECT_SLUG} (owner: towner)")

    # 3. Memberships (tsu is NOT a member — uses SU global access)
    for username, _, _, project_role in TEST_USERS:
        if project_role:
            db.add(ProjectMember(
                id=str(uuid.uuid4()),
                project_id=project.id,
                user_id=user_objs[username].id,
                role=project_role,
            ))
            print(f"  Added {username} as project member ({project_role})")

    # 4. Milestone + phases
    milestone = Milestone(
        id=str(uuid.uuid4()),
        project_id=project.id,
        label="M1: Test",
        name="Test Milestone",
        sort_order=0,
        status="active",
    )
    db.add(milestone)
    db.flush()
    phase1 = Phase(id=str(uuid.uuid4()), project_id=project.id, milestone_id=milestone.id,
                   name="Setup", sort_order=0, status="active")
    phase2 = Phase(id=str(uuid.uuid4()), project_id=project.id, milestone_id=milestone.id,
                   name="Verify", sort_order=1, status="active")
    db.add_all([phase1, phase2])
    db.flush()
    print(f"  Created milestone M1 + 2 phases")

    # 5. Sample tasks — spread assignments across all four roles so My Tasks renders for each
    tasks = [
        ("Verify login as tsu",        "ready",        "human:TEST: Super User",    phase1.id),
        ("Verify login as towner",     "in_progress",  "human:TEST: Project Owner", phase1.id),
        ("Verify padlock on tmember",  "needs_review", "human:TEST: Member",        phase2.id),
        ("Verify viewer is read-only", "ready",        "human:TEST: Viewer",        phase2.id),
        ("Agent picks this up",        "ready",        "agent:test-agent",          phase1.id),
        ("Unassigned triage item",     "backlog",      "",                          phase1.id),
    ]
    for i, (title, status_v, owner, phase_id) in enumerate(tasks):
        t = Task(
            id=str(uuid.uuid4()),
            project_id=project.id,
            title=title,
            description="Test task created by fixture seeder.",
            status=status_v,
            priority="medium",
            owner_label=owner,
            phase_id=phase_id,
            milestone_label="M1: Test",
            sort_order=i * 100,
        )
        db.add(t)
    print(f"  Created {len(tasks)} tasks")

    # 6. Test agent
    raw_token = "vf_test" + uuid.uuid4().hex[:32]
    import hashlib
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    prefix = raw_token[:8]
    agent = Agent(
        id=str(uuid.uuid4()),
        name=TEST_AGENT_NAME,
        slug=TEST_AGENT_SLUG,
        description="Test fixture agent",
        status="active",
        project_id=project.id,
        created_by=user_objs["towner"].id,
        model_type="claude",
        api_token_hash=token_hash,
        token_prefix=prefix,
    )
    db.add(agent)
    db.flush()
    db.add(ProjectMember(
        id=str(uuid.uuid4()),
        project_id=project.id,
        agent_id=agent.id,
        role="write",
    ))

    # Write token file
    try:
        with open(TOKEN_FILE, "w") as f:
            f.write(raw_token + "\n")
        print(f"  Created agent {TEST_AGENT_NAME}, token written to {TOKEN_FILE}")
    except Exception as e:
        print(f"  Created agent (token file write failed: {e})")
        print(f"  TOKEN: {raw_token}")

    db.commit()
    print()
    print("  Seed complete.")
    return True


# ── ENTRY ──────────────────────────────────────────────────
def main():
    if len(sys.argv) < 2:
        print("Usage: test_fixtures.py {seed|wipe|cycle|status}")
        sys.exit(1)

    cmd = sys.argv[1]
    db = SessionLocal()
    try:
        if cmd == "status":
            status(db)
        elif cmd == "wipe":
            wipe(db)
        elif cmd == "seed":
            seed(db)
            print()
            status(db)
        elif cmd == "cycle":
            print("--- WIPE ---")
            wipe(db, force=True)
            print()
            print("--- SEED ---")
            seed(db)
            print()
            status(db)
        else:
            print(f"Unknown command: {cmd}")
            sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
