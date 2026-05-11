#!/usr/bin/env python3
"""
DEV/UAT data reset. Deletes non-kept users + projects, reseeds memberships,
applies [DEV]/[UAT] prepend via tag_env_data.py.

DEV/UAT ONLY. Explicit --env flag required. No default, no auto-detect.

Usage (inside container):
    python scripts/reset_dev_uat_data.py --env dev --dry-run
    python scripts/reset_dev_uat_data.py --env dev --commit
    python scripts/reset_dev_uat_data.py --env uat --dry-run
    python scripts/reset_dev_uat_data.py --env uat --commit

KEEPS:
    Users:   sa, pkhan, su, po, pu, pv  (run seed_dev_accounts.py first)
    Projects: vibeforge-plus, forgebase, ghosttypepaste,
              demo-market-tracker-vibe-market, wifi-heatmap-analyser-dummy

DELETES everything else (users, projects, tasks, notes, members, agents,
activity events scoped to deleted projects, sessions on deleted users).

Kept projects get ProjectMember rows reset to: po=admin (owner), pu=write, pv=read.
"""
import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

KEPT_USERS = {"sa", "pkhan", "su", "po", "pu", "pv"}
KEPT_PROJECT_SLUGS = {
    "vibeforge-plus",
    "forgebase",
    "ghosttypepaste",
    "demo-market-tracker-vibe-market",
    "wifi-heatmap-analyser-dummy",
}


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--env", required=True, choices=["dev", "uat"],
                   help="Target environment. 'prod' is explicitly not accepted.")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Report only; no DB writes.")
    mode.add_argument("--commit",  action="store_true", help="Execute the reset.")
    return p.parse_args()


def main():
    args = parse_args()
    dry = args.dry_run

    db_url = os.environ.get("DATABASE_URL",
                            "postgresql+psycopg2://vibeforge:vibeforge@db:5432/vibeforge")
    engine = create_engine(db_url)
    Session = sessionmaker(bind=engine)
    db = Session()

    print("=" * 60)
    print(f"  DEV/UAT DATA RESET  [env={args.env}  mode={'DRY-RUN' if dry else 'COMMIT'}]")
    print("=" * 60)

    from app.models.user import User
    from app.models.project import Project
    from app.models.project_member import ProjectMember
    from app.models.task import Task
    from app.models.task_note import TaskNote
    from app.models.milestone import Milestone
    from app.models.phase import Phase
    from app.models.agent import Agent
    from app.models.activity import ActivityEvent
    from app.models.session import UserSession

    try:
        # --- USERS ---
        all_users = db.query(User).all()
        to_delete_users = [u for u in all_users if u.username not in KEPT_USERS]
        kept_users = [u for u in all_users if u.username in KEPT_USERS]

        print()
        print(f"USERS: {len(all_users)} total, keeping {len(kept_users)}, deleting {len(to_delete_users)}")
        print("  KEEP:")
        for u in kept_users:
            print(f"    [keep]   {u.username:<14} {u.role:<12} {u.display_name}")
        print("  DELETE:")
        for u in to_delete_users:
            print(f"    [delete] {u.username or '(null)':<14} {(u.role or '?'):<12} {u.display_name}")

        # --- PROJECTS ---
        all_projects = db.query(Project).all()
        to_delete_projects = [p for p in all_projects if p.slug not in KEPT_PROJECT_SLUGS]
        kept_projects = [p for p in all_projects if p.slug in KEPT_PROJECT_SLUGS]
        missing_from_kept = KEPT_PROJECT_SLUGS - {p.slug for p in kept_projects}

        print()
        print(f"PROJECTS: {len(all_projects)} total, keeping {len(kept_projects)}, deleting {len(to_delete_projects)}")
        print("  KEEP:")
        for p in kept_projects:
            tcount = db.query(Task).filter(Task.project_id == p.id).count()
            print(f"    [keep]   {p.slug:<40} status={p.status:<10} tasks={tcount}")
        print("  DELETE:")
        for p in to_delete_projects:
            tcount = db.query(Task).filter(Task.project_id == p.id).count()
            print(f"    [delete] {p.slug:<40} status={p.status:<10} tasks={tcount}")
        if missing_from_kept:
            print(f"  MISSING (expected but not on this env): {sorted(missing_from_kept)}")

        # --- Plan membership reset for kept projects ---
        print()
        print("MEMBERSHIP RESET (for each kept project): po=admin, pu=write, pv=read")

        if dry:
            print()
            print("[DRY-RUN] No changes made. Re-run with --commit to execute.")
            return 0

        # --- COMMIT: delete non-kept projects (cascade) ---
        print()
        print("Executing...")
        for p in to_delete_projects:
            # Delete in FK-safe order: notes → events → tasks → phases → milestones → members → agents → project
            task_ids = [r[0] for r in db.query(Task.id).filter(Task.project_id == p.id).all()]
            if task_ids:
                db.query(TaskNote).filter(TaskNote.task_id.in_(task_ids)).delete(synchronize_session=False)
            db.query(ActivityEvent).filter(ActivityEvent.project_id == p.id).delete(synchronize_session=False)
            db.query(Task).filter(Task.project_id == p.id).delete(synchronize_session=False)
            db.query(Phase).filter(Phase.project_id == p.id).delete(synchronize_session=False)
            db.query(Milestone).filter(Milestone.project_id == p.id).delete(synchronize_session=False)
            db.query(ProjectMember).filter(ProjectMember.project_id == p.id).delete(synchronize_session=False)
            db.query(Agent).filter(Agent.project_id == p.id).delete(synchronize_session=False)
            db.delete(p)
        print(f"  Deleted {len(to_delete_projects)} project(s) and cascaded content")

        # --- COMMIT: delete non-kept users (cascade sessions, nullify FKs) ---
        for u in to_delete_users:
            db.query(UserSession).filter(UserSession.user_id == u.id).delete(synchronize_session=False)
            # Nullify any lingering FK refs in activity_events (FK has no constraint but we keep clean)
            db.query(ActivityEvent).filter(ActivityEvent.actor_user_id == u.id).update(
                {ActivityEvent.actor_user_id: None}, synchronize_session=False)
            # Any agents created_by this user → revoke rather than delete (agents have FK to users?)
            db.query(Agent).filter(Agent.created_by == u.id).update(
                {Agent.status: "revoked", Agent.created_by: None}, synchronize_session=False)
            # ProjectMember rows for this user
            db.query(ProjectMember).filter(ProjectMember.user_id == u.id).delete(synchronize_session=False)
            db.delete(u)
        print(f"  Deleted {len(to_delete_users)} user(s)")

        # --- COMMIT: reset memberships on kept projects ---
        po = db.query(User).filter(User.username == "po").first()
        pu = db.query(User).filter(User.username == "pu").first()
        pv = db.query(User).filter(User.username == "pv").first()
        if not (po and pu and pv):
            print("  WARNING: po/pu/pv not all present. Run seed_dev_accounts.py first, then re-run this script.")
            db.rollback()
            return 2

        import uuid as _uuid
        for p in kept_projects:
            # Delete ALL existing ProjectMember rows for this project (reset to clean)
            db.query(ProjectMember).filter(ProjectMember.project_id == p.id).delete(synchronize_session=False)
            # Re-create: po=admin, pu=write, pv=read
            for (uid, role) in [(po.id, "admin"), (pu.id, "write"), (pv.id, "read")]:
                db.add(ProjectMember(
                    id=str(_uuid.uuid4()),
                    project_id=p.id,
                    user_id=uid,
                    role=role,
                ))
            # Set owner_id to po
            p.owner_id = po.id
            p.updated_at = datetime.now(timezone.utc)
        print(f"  Reset memberships on {len(kept_projects)} kept project(s): po=admin(owner), pu=write, pv=read")

        db.commit()
        print()
        print("RESET COMPLETE. Next step: run scripts/tag_env_data.py --env", args.env,
              "to prepend [" + args.env.upper() + "] on task titles + descriptions.")
        return 0

    except Exception as e:
        db.rollback()
        print(f"ERROR: {e}", file=sys.stderr)
        raise
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
