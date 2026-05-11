"""VF-320 self-test — verify server-side task-card status class emission.

The visual feature (column collapse, card strike/haze, picker openOnly) is
client-side. The ONE server change is that the task-card HTML builder emits
`task-card done` and `task-card cancelled` classes so CSS can style each
distinctly.

Instead of spinning up TestClient (which needs httpx), this test calls the
render function directly and inspects the returned HTML string.

Run inside the app container:
    docker compose exec app python scripts/vf320_selftest.py
"""
from __future__ import annotations
import os, re, sys
from sqlalchemy.orm import Session

sys.path.insert(0, "/app" if os.path.exists("/app/app") else ".")
from app.db.session import SessionLocal  # type: ignore
from app.models.task import Task  # type: ignore
from app.models.project import Project  # type: ignore
from app.api.v2.projects import _render_card  # type: ignore

CHECKS = 0
FAILS = 0


def ok(cond: bool, label: str) -> None:
    global CHECKS, FAILS
    CHECKS += 1
    mark = "OK  " if cond else "FAIL"
    if not cond:
        FAILS += 1
    print(f"  {mark} {label}")


def section(title: str) -> None:
    print(f"\n[{title}]")


def main() -> int:
    db: Session = SessionLocal()
    try:
        proj = db.query(Project).filter(Project.slug == "vibeforge-plus").first()
        if not proj:
            print("FATAL: project vibeforge-plus not found")
            return 2

        # --- Pick representative tasks for each relevant status ---
        def one(status: str) -> Task | None:
            return (db.query(Task)
                    .filter(Task.project_id == proj.id, Task.status == status)
                    .first())

        t_done = one("done")
        t_cancelled = one("cancelled")
        t_backlog = one("backlog") or one("ready") or one("in_progress")

        section("fixture check")
        ok(t_done is not None, "fixture: at least one DONE task exists")
        ok(t_cancelled is not None, "fixture: at least one CANCELLED task exists")
        ok(t_backlog is not None, "fixture: at least one open task exists")

        card_class_re = re.compile(r'<div\s+class="task-card([^"]*)"')

        def card_classes(task: Task) -> str:
            html = _render_card(task, None, db)
            m = card_class_re.search(html)
            return (m.group(1).strip() if m else "")

        if t_done:
            section("render DONE task")
            cls = card_classes(t_done)
            ok(" done" in (" " + cls),
               f'DONE task emits "done" class (classes="{cls}")')
            ok("cancelled" not in cls,
               f'DONE task does NOT emit "cancelled" class')

        if t_cancelled:
            section("render CANCELLED task")
            cls = card_classes(t_cancelled)
            ok(" cancelled" in (" " + cls),
               f'CANCELLED task emits "cancelled" class (classes="{cls}")')
            ok(" done" not in (" " + cls),
               f'CANCELLED task does NOT emit "done" class')

        if t_backlog:
            section("render OPEN task (no status class)")
            cls = card_classes(t_backlog)
            ok("done" not in cls and "cancelled" not in cls,
               f'OPEN task has no done/cancelled class (classes="{cls}")')

        print("\n" + "=" * 66)
        if FAILS == 0:
            print("  ALL CHECKS GREEN")
        else:
            print(f"  {FAILS} / {CHECKS} FAILED")
        print("=" * 66)
        return 1 if FAILS else 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
