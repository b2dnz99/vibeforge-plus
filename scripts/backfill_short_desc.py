"""Backfill short_description for all VF+ tasks missing one."""
import json
import os
import sys
import urllib.request

API = os.environ.get("VIBEFORGE_API", "https://localhost/api/v2")
TOKEN = os.environ.get("VIBEFORGE_TOKEN", "")
if not TOKEN:
    print("ERROR: source .agent-config first (need VIBEFORGE_API + VIBEFORGE_TOKEN)"); sys.exit(1)
HEADERS = {"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"}

# Fetch all tasks
req = urllib.request.Request(f"{API}/projects/vibeforge-plus/tasks", headers=HEADERS)
tasks = json.loads(urllib.request.urlopen(req).read())

# Logical short descriptions by title
SD = {
    "Connect and review VibeForge 1.x": "Reviewed v1 codebase, identified upgrade path to v2 Postgres stack.",
    "Define the first meaningful step": "Cancelled - superseded by structured milestone planning.",
    "Accept VibeForge agent contract": "Agent authenticated via /agentnotes, received full contract with endpoints and rules.",
    "Read VibeForge+ 0-MD-Initial docs": "Reviewed product vision, session handoff, and board hierarchy docs.",
    "Identify and satisfy human prerequisites": "Confirmed VM access, Docker stack, DNS, TLS certs, and Postgres connectivity.",
    "[VERIFY] Docker Compose full stack starts cleanly": "Verified app + db + nginx containers start and interconnect.",
    "[VERIFY] Nginx serves app over HTTP on local network": "Confirmed Nginx reverse proxy routes to FastAPI on port 80.",
    "[VERIFY] HTTPS works with wildcard cert, no browser warnings": "Wildcard TLS cert for *.hydra.net.au verified, no browser warnings.",
    "[VERIFY] FastAPI health endpoint responds": "GET /api/v2/health returns 200 with DB connectivity check.",
    "[VERIFY] PostgreSQL container is up and app connects": "Postgres 17 healthy, app connects via SQLAlchemy.",
    "Milestone A - Hosted foundation skeleton": "Docker + Postgres + Nginx + FastAPI stack deployed on VM.",
    "Add bcrypt + passlib to requirements and rebuild": "Password hashing dependencies added, Docker image rebuilt.",
    "Create User model and first Alembic migration": "Users table with UUID pk, email, bcrypt hash, ui_prefs.",
    "Build auth endpoints - register and login": "POST /auth/register and /auth/login with JWT response.",
    "Create API token model and migration": "api_tokens table with SHA256 hash, prefix, kind, status.",
    "Build token management endpoints": "CRUD for API tokens with issue/revoke/list.",
    "Add bearer token auth middleware": "Bearer token validation on protected endpoints.",
    "Create Project model and migration": "Projects table with slug, status, paths, resume, ITIL fields.",
    "TLS cert strategy  -  self-signed / PEM+Key / ACME": "Evaluate TLS options for production. Currently using wildcard PEM.",
    "Create token_project_permissions model and migration": "Per-project token permissions: read/write/admin.",
    "First-run onboarding wizard": "Guided setup for new instances: user, project, locale, timezone.",
    "Protect /api/v2 endpoints with auth and permission checks": "All API endpoints require valid auth, permissions enforced.",
    "Progressive security posture  -  Level 0/1/2 runtime config": "Configurable security levels: dev (open) to prod (strict).",
    "Seed Postgres with v1 project data": "Migrated 4 projects, 126 tasks from v1 JSON to Postgres.",
    "Unified snapshot and backup pipeline": "System + project snapshots, scheduled backups, retention policy.",
    "Encryption key management and recovery flow": "Encrypted keys in snapshots, recovery flow for server migration.",
    "[VERIFY] Register a user and receive 200": "User registration endpoint returns 200 with valid payload.",
    "[VERIFY] Login returns a JWT": "Login returns signed JWT token for session auth.",
    "[VERIFY] Create a named API token": "Token CRUD verified: create, list, view prefix.",
    "[VERIFY] Token is rejected after revoke": "Revoked tokens return 401 on subsequent requests.",
    "[VERIFY] v1 seed data visible in Postgres": "All v1 projects and tasks visible in Postgres after migration.",
    "B.5  -  FastAPI Jinja2 shell + CSS token layer": "Base template with design tokens, Plus Jakarta Sans + JetBrains Mono.",
    "B.5  -  Sidebar + three-zoom navigation shell": "App shell with sidebar nav: workspace, projects, config sections.",
    "B.5  -  Home dashboard (project grid)": "Project grid with status, description, slug on home page.",
    "B.5  -  Project view (milestone rows + task chips)": "Project overview with milestone grouping and task chip display.",
    "B.5  -  Kanban board view": "7-column board: backlog through cancelled with glass panel columns.",
    "B.5  -  Theme picker + dark/light toggle": "Dark/light toggle, accent hue slider, 5 colour presets.",
    "C  -  Agent notes model + migration + endpoints": "Structured agent notes with author attribution and timestamps.",
    "B.5  -  SSE event stream endpoint": "Server-sent events for live board updates without polling.",
    "C  -  Named agent token provisioning flow": "Self-service token provisioning for agents via Config UI.",
    "[VERIFY] B.5  -  Board renders live data at all three zoom levels": "Board, project, and home views all render live Postgres data.",
    "Sync 0-Code to GitHub": "Git repo initialized, pushed to github.com/b2dnz99/vibeforge-plus.",
    "C  -  Agent context endpoint GET /api/v2/projects/{slug}/agent-context": "Project context endpoint for agent discovery and onboarding.",
    "Board milestone filter - filter by milestone/phase": "Client-side milestone chips, debounced, no page reload.",
    "Board phase swimlanes - group tasks by phase within milestone view": "Phase divider headers group tasks within board columns.",
    "Onboarding - locale and timezone selection": "First-run locale/timezone picker, persisted in user prefs.",
    "Board layout - Kanbanchi-style independent column scroll": "Independent vertical scroll per column, horizontal board scroll.",
    "Gantt chart view - timeline primitives": "Collapsible milestone/phase/task hierarchy with progress bars.",
    "Add short_description field for card face": "Card Summary field (120 chars) shown on board card, full desc in editor.",
    "Cancel reason required - enforce abandoned_note on status=cancelled": "API + UI enforce reason when cancelling. JSON status log format.",
    "Configuration page - theme controls relocated from sidebar": "Appearance settings: dark mode, accent, font size, card animations.",
    "Board column task count - live count per column": "Column headers show task count, updated on HTMX swap.",
    "Enforce state transition rules in API": "ITIL-aligned transitions enforced at API, invalid moves return 422.",
    "Project archive/reopen UI flow on v2 board": "Archive from project drawer with lifecycle_log. Force bypass available.",
    "AUDIT - v2 source of truth readiness assessment": "Formal checklist before v2 SOT cutover. All blockers resolved.",
    "User display preferences - persist font size and theme to DB": "ui_prefs JSON on users table, synced via localStorage + DB.",
    "[VERIFY] Cancel drag - prompt and save works": "Drag to Cancelled prompts reason, saves to abandoned_note.",
    "Rich text editor for notes - Quill.js + @mention pills + HTML storage": "Quill.js integration deferred. Needs mockup-first approach.",
    "[VERIFY] Reopen drag - prompt and save works": "Drag from Cancelled prompts reopen reason, appends to status log.",
    "Mission Control Home - full command center dashboard": "MC-03 command center: stats bar, project cards, drawer, activity timeline.",
    "[VERIFY] No Tasks placeholder not draggable": "Empty column placeholder excluded from Sortable drag targets.",
    "[VERIFY] Phase grouping after drag": "Tasks retain phase assignment after drag between columns.",
    "[VERIFY] Add Task auto-assigns to Miscellaneous phase": "New tasks auto-assigned to Triage or Miscellaneous phase.",
    "Floating frosted card editor - replace drawer with animated card": "Frosted glass card editor with grow/shrink animation, drag + resize.",
    "Drag pulse animations - visual card tracking on status change": "Water-drop glow effect on cards after successful status change.",
    "Drag flow animation speed - configurable slider in Appearance": "Configurable animation duration and pulse ratio in Appearance.",
    "[VERIFY] Floating card editor - grow animation timing": "Card grows from source element with cubic-bezier easing.",
    "[VERIFY] Drag pulse - brand/green/amber timing": "Pulse colours match status: green=success, red=rejected.",
    "[VERIFY] Card editor - drag and resize": "Editor card draggable by header, resizable from corner.",
    "[VERIFY] Reason dialogs - cancel/reopen/phase change": "MC-styled dialogs for all reason-required transitions.",
    "Unified activity feed + audit logging - deployed": "Notes + Log tab shows unified feed with actor icons and timestamps.",
    "Mission Control theme - implement dark + light from mockups": "MC dark (#0f172a) + light theme with neon accents and glass effects.",
    "Task editor - implement MC tabbed layout from mockup": "Neon topbar, radar header, Notes/Notes+Log tabs, Transmit button.",
    "CRITICAL - Board hierarchy display rules (Milestone vs Phase vs Task)": "Milestone=filter chips, Phase=card badge. See CRITICAL-BOARD-HIERARCHY.md.",
    "v1.5 schema upgrade - migrate v1 to structured notes, phases, audit trail": "Built v1.5 from scratch: SQLite, MC theme, Gantt, notes, migration.",
    "T1 three-tab Gantt chart (Overview + Dependencies + Timeline)": "Gantt with collapsible milestones, phase sections, task bars.",
    "Wire logged-in user identity to notes and activity": "User display_name from DB, initials avatar, actor attribution.",
    "Extract MC editor to shared partial - open drawer from Gantt": "Shared _editor.html/_editor_js.html partials for board + gantt.",
    "Audit trail for all task field changes (priority, owner, dates, etc)": "ActivityEvent logged for every field change with from/to values.",
    "RBAC scaffold - project members, agents table, @mentions, auto-login": "Agents table, project_members, @mention autocomplete with keyboard nav.",
    "Add Cancel button next to Save in editor drawer": "Cancel + Save footer with dirty detection and confirm dialog.",
    "Agent API access - service token or unauthenticated endpoint": "Bearer token auth via SHA256 hash lookup on /agentnotes.",
    "Port /agentnotes contract endpoint to v2": "Two-tier contract: unauthenticated=minimal, authenticated=full.",
    "Project CRUD API + Home page + workspace protocol": "Project create/update/archive/reopen + workspace path protocol.",
    "Project + milestone lifecycle - archive/complete/abandon/reopen with strict rule": "Full lifecycle with lifecycle_log, force bypass, celebration.",
    "Replace all prompt()/alert() with MC-styled overlay dialogs": "MC confirm/prompt/alert/select dialogs replace all browser natives.",
    "Bug: Select dropdown black flash on open (Chromium rendering)": "Native select flashes black on open. Chromium-specific, cosmetic.",
    "Task dependencies - add blocked_by_task_id to tasks": "blocked_by_task_id FK on tasks, shown in editor + card chip.",
    "ITIL schema primitives - milestones table, task_type, assignee_id, estimated_hours": "Milestones, task_type, assignee, estimated_hours, start/due dates.",
}

# Also handle titles that might be truncated in the API response
count = 0
failed = 0
for t in tasks:
    if t.get("short_description"):
        continue

    title = t["title"]
    sd = SD.get(title, "")

    # Try partial match for truncated titles
    if not sd:
        for k, v in SD.items():
            if title.startswith(k[:60]):
                sd = v
                break

    # Fallback: generate from title
    if not sd:
        # Clean up title for card summary
        sd = title.replace("[VERIFY] ", "Verified: ").replace("B.5  -  ", "").replace("C  -  ", "")
        if len(sd) > 120:
            sd = sd[:117] + "..."

    data = json.dumps({"short_description": sd}).encode()
    req = urllib.request.Request(
        f"{API}/tasks/{t['id']}",
        data=data,
        headers=HEADERS,
        method="PATCH"
    )
    try:
        urllib.request.urlopen(req)
        count += 1
        print(f"  OK: {title[:60]}")
    except Exception as e:
        failed += 1
        print(f"  FAIL: {title[:60]} -- {e}")

print(f"\nDone: {count} updated, {failed} failed")
