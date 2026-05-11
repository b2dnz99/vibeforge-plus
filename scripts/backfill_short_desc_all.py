"""Backfill short_description for ALL projects' tasks."""
import json
import os
import sys
import urllib.request

API = os.environ.get("VIBEFORGE_API", "https://localhost/api/v2")
TOKEN = os.environ.get("VIBEFORGE_TOKEN", "")
if not TOKEN:
    print("ERROR: source .agent-config first (need VIBEFORGE_API + VIBEFORGE_TOKEN)"); sys.exit(1)
HEADERS = {"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"}

SLUGS = ["forgebase", "vibeforge-mvp", "vibeforge-1-5", "wifi-heatmap-analyser-dummy"]

# Hand-crafted summaries by project
SD = {
    # ── ForgeBase ──
    "Define the first meaningful step": "Identify the core API primitive to build first.",
    "Plan ForgeBase - requirements, milestones, phases": "Define project scope, milestones, and phase structure.",

    # ── VibeForge MVP ──
    "Add in-app project creation": "Create new projects from within the app UI.",
    "Use local session time and timezone in the UI": "Display all timestamps in user's local timezone.",
    "Clarify primary navigation and add Home path": "Clear nav hierarchy with Home as default landing.",
    "Polish board movement": "Smooth drag animations and column transitions.",
    "Define MVP done line": "Establish what 'done' means for the MVP milestone.",
    "Tighten task detail editing": "Improve inline editing UX for task fields.",
    "Shape agent summaries beyond raw activity": "Human-readable agent session summaries, not just raw logs.",
    "Fix crowded board card actions": "Reduce button clutter on board cards.",
    "Fix timezone mismatch in UI time display": "All times render in local timezone consistently.",
    "Fix task creation flow": "Task creation validates required fields and auto-assigns defaults.",
    "Fix broken Back to Board button": "Navigation back to board from detail view works.",
    "Add global preferences layer": "User prefs for theme, font, locale persisted across sessions.",
    "Add American localization support": "Cancelled - single locale sufficient for now.",
    "Define completion and archive primitive": "Archive/complete states with reason tracking.",
    "Add archive lifecycle for completed and abandoned projects": "Projects can be archived as completed or abandoned with reason.",
    "Define agent notes artifact for VS Code resume": "Agent session notes exportable for VS Code handoff.",
    "Generate final project Markdown docs on completion": "Auto-generate project docs on completion.",
    "Add export bundle and standalone HTML export": "Export project as self-contained HTML bundle.",
    "Add project import flow with conflict checks": "Import projects with duplicate detection.",
    "Build navigable read-only snapshot bundle": "Browsable offline snapshot of project state.",
    "Match snapshot export styling to app UI": "Exported snapshots match the MC theme.",
    "Fix export snapshot bugs and edge cases": "Edge cases in export: empty projects, special chars, large data.",
    "Capture VibeForge 2.0 directions pack": "Document v2.0 vision, architecture, and migration path.",
    "Add task-level abandoned status with note": "Tasks can be cancelled with mandatory reason note.",
    "Validate import/export with a dummy bundle project": "Round-trip test: export then import a project.",
    "Move project creation into a dedicated flow": "Project creation as a focused workflow, not inline form.",
    "Move archived projects to a dedicated archive page": "Archived projects separated from active project list.",
    "Improve path, import, and export file controls": "Better file picker UX for paths and import/export.",
    "Move task creation into its own workflow": "Task creation as modal/drawer, not inline.",
    "Split closed work into completed and abandoned decks": "Done and cancelled tasks in separate visual groups.",
    "Add review status and pending validation lane": "needs_review status for tasks awaiting human sign-off.",
    "Define VibeForge 2.0 MVP done line": "Criteria for v2.0 MVP completion.",
    "MVP Done Celebration": "Celebration moment when MVP milestone completes.",
    "v1.1 Task Feature 1 - Fix the paths": "Fix file path handling for project root and docs.",
    "v1.1 Task Feature 2 - Package as EXE": "Package VibeForge MVP as standalone Windows executable.",

    # ── VibeForge 1.5 ──
    "Set up v1.5 project and port v1 data": "Created v1.5 from scratch, migrated v1 data to SQLite.",
    "Build MC theme for v1.5": "Mission Control dark theme with neon accents.",
    "Build Gantt view": "Collapsible milestone/phase/task Gantt chart.",
    "Build notes system": "Task notes with author attribution and timestamps.",
    "Port board view from v1": "Kanban board with drag-and-drop status changes.",
    "Build agent contract endpoint": "Two-tier /agentnotes endpoint for agent onboarding.",
    "Build v1.5 to v2 migration pipeline": "SQL generation script for v1.5 SQLite to v2 Postgres sync.",
    "Wire milestone filtering": "Milestone filter chips on board, client-side.",
    "Add phase badges to cards": "Phase name shown as chip on board cards.",
    "Fix v1 data migration edge cases": "Handle Unicode, orphaned IDs, FK mapping in migration.",
    "Add audit trail": "Activity events for all task field changes.",
    "Wire user identity": "User display_name from DB, not hardcoded.",
    "Add @mention autocomplete": "Keyboard-navigable @mention dropdown in note editor.",
    "Build config page": "Appearance settings and API token management.",
    "Extract editor to shared partial": "Reusable _editor.html for board and gantt.",
    "Add project lifecycle": "Archive/reopen with reasons and activity logging.",
    "Wire ITIL task IDs": "PRJ00004-TSK00072 format with short VF-72 display.",

    # ── WiFi Heatmap ──
    "Define mobile scanner capture model": "Data model for WiFi signal capture during walkthroughs.",
    "Build calibration start-point workflow": "Calibrate scanner position against known floor plan points.",
    "Generate crude 3D building reconstruction from first pass": "Basic 3D model from initial walkthrough data.",
    "Capture AP sightings and candidate access-point markers": "Record detected access points with signal strength.",
    "Record dead zones and high-fidelity drops during forward passes": "Map areas with poor or no WiFi coverage.",
    "Overlay previous pass in AR with live strength bar": "AR view showing prior scan data with live signal overlay.",
    "Guide the third pass toward uncertain signal areas": "AI-guided routing to fill coverage gaps.",
    "Import captured walkthroughs into the PC app": "Transfer mobile scan data to desktop for analysis.",
    "Allow manual drift correction against 2D plans": "Correct positional drift by aligning to floor plans.",
    "Model known AP positions in desktop analysis": "Place known access points on the 3D/2D model.",
    "Render 3D and flat-plan heatmaps from calibrated data": "Visualize WiFi coverage as colour-coded heatmaps.",
    "Generate AI-assisted WiFi audit report": "AI-generated report with coverage analysis and recommendations.",
    "Prototype Bluetooth triangulation assist": "Cancelled - Bluetooth positioning deferred.",
    "Define pass completion and export bundle rules": "Rules for when a scan pass is complete and exportable.",
}

total_updated = 0
total_failed = 0

for slug in SLUGS:
    print(f"\n=== {slug} ===")
    req = urllib.request.Request(f"{API}/projects/{slug}/tasks", headers=HEADERS)
    tasks = json.loads(urllib.request.urlopen(req).read())

    for t in tasks:
        if t.get("short_description"):
            continue

        title = t["title"]
        sd = SD.get(title, "")

        # Partial match fallback
        if not sd:
            for k, v in SD.items():
                if title.startswith(k[:50]) or k.startswith(title[:50]):
                    sd = v
                    break

        # Final fallback: clean title
        if not sd:
            sd = title.replace("[VERIFY] ", "Verified: ")
            if len(sd) > 120:
                sd = sd[:117] + "..."

        data = json.dumps({"short_description": sd}).encode()
        req = urllib.request.Request(
            f"{API}/tasks/{t['id']}", data=data, headers=HEADERS, method="PATCH"
        )
        try:
            urllib.request.urlopen(req)
            total_updated += 1
            print(f"  OK: {title[:65]}")
        except Exception as e:
            total_failed += 1
            print(f"  FAIL: {title[:65]} -- {e}")

print(f"\nTotal: {total_updated} updated, {total_failed} failed")
