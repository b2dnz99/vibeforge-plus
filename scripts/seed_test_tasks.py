"""
Seed test tasks across all milestones and statuses for UI testing.
Run with: docker compose exec app python scripts/seed_test_tasks.py [--wipe]
--wipe removes previously seeded test tasks first (those with title starting with [TEST])
"""
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.core.config import settings
from app.models.project import Project
from app.models.task import Task

PROJECT_SLUG = "vibeforge-plus"
WIPE = "--wipe" in sys.argv

MILESTONES = ["Milestone B", "Milestone C", "Milestone D"]

STATUSES = ["backlog", "ready", "in_progress", "needs_review", "blocked", "done", "cancelled"]

TASKS_PER_STATUS = [
    ("Fix token expiry edge case",       "high",   "agent"),
    ("Write unit tests for auth flow",   "medium", "agent"),
    ("Review API contract with human",   "medium", "human"),
]

DONE_NOTE = "Completed and verified against acceptance criteria."
CANCELLED_NOTE = "Descoped — not needed for this milestone."

def now():
    return datetime.now(timezone.utc)

def main():
    engine = create_engine(settings.DATABASE_URL)
    with Session(engine) as db:
        project = db.query(Project).filter(Project.slug == PROJECT_SLUG).first()
        if not project:
            print(f"ERROR: Project '{PROJECT_SLUG}' not found.")
            sys.exit(1)

        if WIPE:
            deleted = db.query(Task).filter(
                Task.project_id == project.id,
                Task.title.like("[TEST]%")
            ).delete(synchronize_session=False)
            db.commit()
            print(f"Wiped {deleted} test tasks.")

        count = 0
        sort = 5000  # high sort_order so test tasks appear after real ones

        for milestone in MILESTONES:
            for status in STATUSES:
                for title_tpl, priority, owner in TASKS_PER_STATUS:
                    sort += 1
                    note = ""
                    if status == "cancelled":
                        note = CANCELLED_NOTE

                    task = Task(
                        id=str(uuid.uuid4()),
                        project_id=project.id,
                        title=f"[TEST] {title_tpl}",
                        description=f"Test task — {milestone}, status={status}. This is placeholder content to visualise the board layout.",
                        status=status,
                        priority=priority,
                        owner_label=owner,
                        milestone_label=milestone,
                        parent_task_id=None,
                        sort_order=sort,
                        abandoned_note=note,
                        created_at=now(),
                        updated_at=now(),
                    )
                    db.add(task)
                    count += 1

        db.commit()
        print(f"Seeded {count} test tasks across {len(MILESTONES)} milestones × {len(STATUSES)} statuses.")
        print("Remove them later with: python scripts/seed_test_tasks.py --wipe")

if __name__ == "__main__":
    main()
