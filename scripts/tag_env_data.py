#!/usr/bin/env python3
"""
Tag VibeForge+ project data with [DEV] / [UAT] visual canary prefix.

DEV/UAT ONLY. Never run against prod.

Iterates the VibeForge+ project (slug='vibeforge-plus') and prepends a tag
to project name, task title, and task description. Idempotent — won't
double-prepend if the tag is already present.

Usage:
    docker exec vibeforge-app-1 python scripts/tag_env_data.py --env dev
    docker exec vibeforge-app-1 python scripts/tag_env_data.py --env uat
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True, choices=["dev", "uat"])
    ap.add_argument("--project-slug", default="vibeforge-plus")
    args = ap.parse_args()

    env = os.environ.get("VIBEFORGE_ENV", "").lower()
    if env == "prod":
        print("ERROR: refusing to run against prod (VIBEFORGE_ENV=prod). Dev/UAT only.")
        sys.exit(2)

    tag = f"[{args.env.upper()}]"

    db_url = os.environ.get("DATABASE_URL", "postgresql+psycopg2://vibeforge:vibeforge@db:5432/vibeforge")
    engine = create_engine(db_url)
    Session = sessionmaker(bind=engine)
    db = Session()

    try:
        from app.models.project import Project
        from app.models.task import Task

        proj = db.query(Project).filter(Project.slug == args.project_slug).first()
        if not proj:
            print(f"ERROR: project slug '{args.project_slug}' not found.")
            sys.exit(1)

        tagged_proj = 0
        if not proj.name.startswith(tag):
            proj.name = f"{tag} {proj.name}"
            tagged_proj = 1

        tasks = db.query(Task).filter(Task.project_id == proj.id).all()
        tagged_tasks = 0
        for t in tasks:
            changed = False
            if not (t.title or "").startswith(tag):
                t.title = f"{tag} {t.title or ''}".strip()
                changed = True
            desc = t.description or ""
            if not desc.startswith(tag):
                t.description = f"{tag} {desc}".strip()
                changed = True
            if changed:
                tagged_tasks += 1

        db.commit()
        print(f"OK: tagged project={tagged_proj} tasks={tagged_tasks}/{len(tasks)} with {tag}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
