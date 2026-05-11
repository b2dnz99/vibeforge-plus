"""
Seed script: import VibeForge 1.x JSON data into VibeForge+ Postgres.

Usage (from /opt/vibeforge inside the container):
    python scripts/seed_v1_data.py --data-dir /v1data --clear

Runs via:
    docker compose exec -T app python scripts/seed_v1_data.py --data-dir /v1data

The v1 data directory is mounted into the container at /v1data via docker compose run
or by copying the data to the VM first.

This is a dev/test seed only. Safe to re-run with --clear to wipe and reload.
"""

import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

# Bootstrap app context
import sys
sys.path.insert(0, "/app")

from app.db.session import SessionLocal
from app.models.project import Project
from app.models.task import Task
from app.models.activity import ActivityEvent


# --- v1 status → v2 status mapping ---
STATUS_MAP = {
    "backlog":   "backlog",
    "todo":      "ready",
    "doing":     "in_progress",
    "review":    "needs_review",
    "blocked":   "blocked",
    "done":      "done",
    "abandoned": "cancelled",
}

PRIORITY_MAP = {
    "low":    "low",
    "medium": "medium",
    "high":   "high",
}

ACTOR_MAP = {
    "human":  "human",
    "agent":  "agent",
    "system": "system",
}


def parse_dt(s: str | None) -> datetime:
    if not s:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return datetime.now(timezone.utc)


def seed_project(db, project_dir: Path, dry_run: bool) -> str | None:
    proj_file = project_dir / "project.json"
    if not proj_file.exists():
        print(f"  [skip] no project.json in {project_dir.name}")
        return None

    p = json.loads(proj_file.read_text(encoding="utf-8"))
    slug = p.get("slug") or project_dir.name

    existing = db.query(Project).filter(Project.slug == slug).first()
    if existing:
        print(f"  [skip] project '{slug}' already exists (id={existing.id})")
        return existing.id

    status_raw = p.get("status", "active")
    archived_reason_raw = p.get("archived_reason")

    project = Project(
        id=str(uuid.uuid4()),
        slug=slug,
        name=p.get("name", slug),
        description=p.get("description", ""),
        status=status_raw if status_raw in ("active", "archived") else "active",
        archived_reason=archived_reason_raw if archived_reason_raw in ("completed", "abandoned") else None,
        root_path=p.get("root_path"),
        docs_path=p.get("docs_path"),
        project_url=p.get("project_url") or None,
        resume_summary=p.get("resume_summary", ""),
        agentic_dev=True,
        created_at=parse_dt(p.get("created_at")),
        updated_at=parse_dt(p.get("updated_at")),
        archived_at=parse_dt(p.get("archived_at")) if p.get("archived_at") else None,
    )

    if not dry_run:
        db.add(project)
        db.flush()

    print(f"  [project] '{slug}' ({project.id})")
    return project.id


def seed_tasks(db, project_dir: Path, project_id: str, dry_run: bool):
    tasks_file = project_dir / "tasks.json"
    if not tasks_file.exists():
        return

    tasks = json.loads(tasks_file.read_text(encoding="utf-8"))
    count = 0

    for t in tasks:
        v1_id = t.get("id")
        v1_status = t.get("status", "backlog")
        v1_priority = t.get("priority", "medium")
        v1_owner = t.get("owner", "agent")

        task = Task(
            id=str(uuid.uuid4()),
            project_id=project_id,
            external_number=v1_id if isinstance(v1_id, int) else None,
            title=t.get("title", "Untitled"),
            description=t.get("description", ""),
            status=STATUS_MAP.get(v1_status, "backlog"),
            priority=PRIORITY_MAP.get(v1_priority, "medium"),
            owner_label=v1_owner if v1_owner in ("human", "agent") else "agent",
            sort_order=t.get("sort_order", 0),
            abandoned_note=t.get("abandoned_note", ""),
            created_at=parse_dt(t.get("created_at")),
            updated_at=parse_dt(t.get("updated_at")),
        )

        if not dry_run:
            db.add(task)
        count += 1

    print(f"  [tasks]   {count} tasks queued")


def seed_activity(db, project_dir: Path, project_id: str, dry_run: bool):
    activity_file = project_dir / "activity.jsonl"
    if not activity_file.exists():
        return

    lines = activity_file.read_text(encoding="utf-8").strip().splitlines()
    count = 0

    for line in lines:
        if not line.strip():
            continue
        try:
            a = json.loads(line)
        except json.JSONDecodeError:
            continue

        actor_type = a.get("actor_type", a.get("actor", "system"))
        event = ActivityEvent(
            id=str(uuid.uuid4()),
            project_id=project_id,
            task_id=None,  # v1 doesn't reliably link task UUIDs
            actor_type=ACTOR_MAP.get(actor_type, "system"),
            action=a.get("action", a.get("type", "unknown")),
            details=a.get("details", a.get("summary", "")),
            created_at=parse_dt(a.get("created_at", a.get("timestamp"))),
        )

        if not dry_run:
            db.add(event)
        count += 1

    print(f"  [activity] {count} events queued")


def main():
    parser = argparse.ArgumentParser(description="Seed VibeForge+ Postgres with v1 JSON data")
    parser.add_argument("--data-dir", default="/v1data", help="Path to v1 data directory")
    parser.add_argument("--clear", action="store_true", help="Wipe existing seeded data before import")
    parser.add_argument("--dry-run", action="store_true", help="Parse and report only, no DB writes")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.exists():
        print(f"ERROR: data directory not found: {data_dir}")
        sys.exit(1)

    db = SessionLocal()

    try:
        if args.clear and not args.dry_run:
            print("Clearing existing seeded data...")
            db.execute(text("DELETE FROM activity_events"))
            db.execute(text("DELETE FROM tasks"))
            db.execute(text("DELETE FROM projects"))
            db.commit()
            print("Cleared.\n")

        project_dirs = [d for d in sorted(data_dir.iterdir()) if d.is_dir() and not d.name.startswith("_")]

        for project_dir in project_dirs:
            print(f"\n--- {project_dir.name} ---")
            project_id = seed_project(db, project_dir, args.dry_run)
            if not project_id:
                continue
            seed_tasks(db, project_dir, project_id, args.dry_run)
            seed_activity(db, project_dir, project_id, args.dry_run)

        if not args.dry_run:
            db.commit()
            print("\nCommitted.")
        else:
            print("\n[dry-run] No changes written.")

    except Exception as e:
        db.rollback()
        print(f"\nERROR: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
