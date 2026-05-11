"""Populate milestones table from BOARD-STRUCTURE.json and backfill milestone_id on tasks.
Run inside container: python scripts/populate_milestones.py
"""
import json
import uuid
from pathlib import Path
from sqlalchemy import create_engine, text
from app.core.config import settings

engine = create_engine(settings.DATABASE_URL)

structure_path = Path(__file__).parent.parent / "BOARD-STRUCTURE.json"
if not structure_path.exists():
    print("BOARD-STRUCTURE.json not found")
    exit(1)

data = json.loads(structure_path.read_text())

with engine.begin() as conn:
    # Get project id
    row = conn.execute(text("SELECT id FROM projects WHERE slug = 'vibeforge-plus'")).fetchone()
    if not row:
        print("Project vibeforge-plus not found")
        exit(1)
    project_id = row[0]

    for i, m in enumerate(data.get("milestones", [])):
        label = m["label"]
        name = m["name"]

        # Check if already exists
        existing = conn.execute(
            text("SELECT id FROM milestones WHERE project_id = :pid AND label = :label"),
            {"pid": project_id, "label": label},
        ).fetchone()

        if existing:
            ms_id = existing[0]
            print(f"  EXISTS: {label} → {ms_id}")
        else:
            ms_id = str(uuid.uuid4())
            conn.execute(
                text(
                    "INSERT INTO milestones (id, project_id, label, name, sort_order) "
                    "VALUES (:id, :pid, :label, :name, :sort)"
                ),
                {"id": ms_id, "pid": project_id, "label": label, "name": name, "sort": i * 10},
            )
            print(f"  CREATED: {label} ({name}) → {ms_id}")

        # Backfill milestone_id on tasks
        result = conn.execute(
            text(
                "UPDATE tasks SET milestone_id = :ms_id "
                "WHERE project_id = :pid AND milestone_label = :label AND milestone_id IS NULL"
            ),
            {"ms_id": ms_id, "pid": project_id, "label": label},
        )
        print(f"    Linked {result.rowcount} tasks")

print("Done.")
