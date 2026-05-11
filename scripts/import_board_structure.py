"""
Import board structure from BOARD-STRUCTURE.json into the vibeforge-plus project.

Usage (run from /app inside container):
    python scripts/import_board_structure.py [--wipe]

--wipe  : DELETE all existing tasks for vibeforge-plus before importing
          (default: dry run, prints what would be created)
"""
import sys
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Allow running from /app in the container or from 0-Code/ locally
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.project import Project
from app.models.task import Task

STRUCTURE_FILE = Path(__file__).parent.parent.parent.parent / "0-MD" / "BOARD-STRUCTURE.json"
# Fallback path for when running inside container (file mounted or copied)
STRUCTURE_FILE_CONTAINER = Path("/app/BOARD-STRUCTURE.json")

PROJECT_SLUG = "vibeforge-plus"
WIPE = "--wipe" in sys.argv


def now():
    return datetime.now(timezone.utc)


def load_structure():
    for p in [STRUCTURE_FILE, STRUCTURE_FILE_CONTAINER]:
        if p.exists():
            with open(p) as f:
                return json.load(f)
    raise FileNotFoundError(
        f"BOARD-STRUCTURE.json not found at {STRUCTURE_FILE} or {STRUCTURE_FILE_CONTAINER}"
    )


def main():
    data = load_structure()
    engine = create_engine(settings.DATABASE_URL)

    with Session(engine) as db:
        project = db.query(Project).filter(Project.slug == PROJECT_SLUG).first()
        if not project:
            print(f"ERROR: Project '{PROJECT_SLUG}' not found in DB.")
            print("Run seed_v1_data.py first to create the project, or create it manually.")
            sys.exit(1)

        print(f"Project found: {project.name} (id={project.id})")

        if WIPE:
            count = db.query(Task).filter(Task.project_id == project.id).count()
            print(f"\nWiping {count} existing tasks for '{PROJECT_SLUG}'...")
            db.query(Task).filter(Task.project_id == project.id).delete()
            db.commit()
            print("Wiped.")
        else:
            existing = db.query(Task).filter(Task.project_id == project.id).count()
            print(f"\nDRY RUN — {existing} tasks currently in board (use --wipe to replace)")

        total_phases = 0
        total_tasks = 0
        phase_sort = 0

        for milestone in data["milestones"]:
            m_label = milestone["label"]        # e.g. "Milestone B"
            m_status = milestone.get("status", "backlog")
            print(f"\n  {m_label}: {milestone['name']}")

            for phase in milestone["phases"]:
                phase_sort += 10

                # Create phase task (parent, no parent_task_id)
                phase_task_id = str(uuid.uuid4())
                phase_task = Task(
                    id=phase_task_id,
                    project_id=project.id,
                    title=f"[{m_label}] {phase['name']}",
                    description=f"Phase grouping for {milestone['name']} — {phase['name']}",
                    status=_map_status(phase.get("status", "backlog")),
                    priority="medium",
                    owner_label="phase",
                    parent_task_id=None,
                    milestone_label=m_label,
                    sort_order=phase_sort,
                    created_at=now(),
                    updated_at=now(),
                )

                if WIPE:
                    db.add(phase_task)
                else:
                    print(f"    [PHASE] {phase_task.title}")

                total_phases += 1
                task_sort = phase_sort

                for t in phase["tasks"]:
                    task_sort += 1
                    leaf = Task(
                        id=str(uuid.uuid4()),
                        project_id=project.id,
                        title=t["title"],
                        description=t.get("description", ""),
                        status=_map_status(t.get("status", "backlog")),
                        priority=t.get("priority", "medium"),
                        owner_label=t.get("owner", "agent"),
                        parent_task_id=phase_task_id,
                        milestone_label=m_label,
                        sort_order=task_sort,
                        created_at=now(),
                        updated_at=now(),
                    )
                    if WIPE:
                        db.add(leaf)
                    else:
                        print(f"      [TASK] {leaf.title}")
                    total_tasks += 1

        if WIPE:
            db.commit()
            print(f"\nImported: {total_phases} phase tasks + {total_tasks} leaf tasks = {total_phases + total_tasks} total")
        else:
            print(f"\nDry run complete: would import {total_phases} phase tasks + {total_tasks} leaf tasks")
            print("Run with --wipe to execute.")


def _map_status(s):
    valid = {"backlog", "ready", "in_progress", "needs_review", "blocked", "done", "cancelled"}
    return s if s in valid else "backlog"


if __name__ == "__main__":
    main()
