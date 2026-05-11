"""Populate phases table from existing phase tasks and backfill phase_id on tasks.
Run inside container: PYTHONPATH=/app python scripts/populate_phases.py
"""
import uuid
from sqlalchemy import create_engine, text
from app.core.config import settings

engine = create_engine(settings.DATABASE_URL)

with engine.begin() as conn:
    # Get project id
    row = conn.execute(text("SELECT id FROM projects WHERE slug = 'vibeforge-plus'")).fetchone()
    if not row:
        print("Project vibeforge-plus not found")
        exit(1)
    project_id = row[0]

    # Get all phase tasks
    phase_tasks = conn.execute(text(
        "SELECT id, title, milestone_label FROM tasks "
        "WHERE project_id = :pid AND owner_label = 'phase' "
        "ORDER BY milestone_label, sort_order"
    ), {"pid": project_id}).fetchall()

    # Get milestone ID lookup
    milestones = conn.execute(text(
        "SELECT id, label FROM milestones WHERE project_id = :pid"
    ), {"pid": project_id}).fetchall()
    ms_lookup = {m[1]: m[0] for m in milestones}

    for i, pt in enumerate(phase_tasks):
        phase_task_id = pt[0]
        raw_title = pt[1]
        ms_label = pt[2]

        # Extract phase name from title (strip "[Milestone X] " prefix)
        name = raw_title
        if "] " in name:
            name = name.split("] ", 1)[1]

        ms_id = ms_lookup.get(ms_label) if ms_label else None

        # Check if phase already exists
        existing = conn.execute(text(
            "SELECT id FROM phases WHERE project_id = :pid AND name = :name "
            "AND (milestone_id = :ms_id OR (milestone_id IS NULL AND :ms_id IS NULL))"
        ), {"pid": project_id, "name": name, "ms_id": ms_id}).fetchone()

        if existing:
            phase_id = existing[0]
            print(f"  EXISTS: {name} → {phase_id}")
        else:
            phase_id = str(uuid.uuid4())
            conn.execute(text(
                "INSERT INTO phases (id, project_id, milestone_id, name, sort_order) "
                "VALUES (:id, :pid, :ms_id, :name, :sort)"
            ), {"id": phase_id, "pid": project_id, "ms_id": ms_id, "name": name, "sort": i * 10})
            print(f"  CREATED: {name} (ms={ms_label}) → {phase_id}")

        # Backfill phase_id on child tasks (those with parent_task_id = phase_task_id)
        result = conn.execute(text(
            "UPDATE tasks SET phase_id = :phase_id "
            "WHERE parent_task_id = :pt_id AND phase_id IS NULL"
        ), {"phase_id": phase_id, "pt_id": phase_task_id})
        print(f"    Linked {result.rowcount} tasks")

    # Check for orphan tasks (no phase_id set)
    orphans = conn.execute(text(
        "SELECT COUNT(*) FROM tasks WHERE project_id = :pid "
        "AND owner_label <> 'phase' AND phase_id IS NULL"
    ), {"pid": project_id}).fetchone()
    print(f"\nOrphan tasks (no phase_id): {orphans[0]}")

print("Done.")
