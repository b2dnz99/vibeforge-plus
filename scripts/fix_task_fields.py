"""Fix missing/generic fields on open tasks."""
import json
import urllib.request

with open("C:/0-APP/ViveForge+/0-Code/.agent-config") as f:
    config = dict(line.strip().split("=", 1) for line in f if "=" in line)

API = config["VIBEFORGE_API"]
TOKEN = config["VIBEFORGE_TOKEN"]
HEADERS = {"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"}

# Get tasks
req = urllib.request.Request(f"{API}/projects/vibeforge-plus/tasks", headers=HEADERS)
tasks = json.loads(urllib.request.urlopen(req).read())

# Get phases for lookup
req = urllib.request.Request(f"{API}/projects/vibeforge-plus/phases", headers=HEADERS)
phases = json.loads(urllib.request.urlopen(req).read())
phase_by_name = {}
for p in phases:
    key = f"{p.get('milestone_label', '')}_{p['name']}".lower()
    phase_by_name[key] = p["id"]
    phase_by_name[p["name"].lower()] = p["id"]

# Get members for proper owner mapping
req = urllib.request.Request(f"{API}/projects/vibeforge-plus/members", headers=HEADERS)
members = json.loads(urllib.request.urlopen(req).read())
claude_owner = "agent:Claude"
parvez_owner = "human:Parvez Khan"

# Fixes per task
fixes = {
    "VF-28":  {"owner_label": parvez_owner},  # TLS - human decision
    "VF-29":  {"owner_label": claude_owner},   # Onboarding wizard - agent builds
    "VF-30":  {"owner_label": claude_owner},   # Security posture
    "VF-31":  {"owner_label": claude_owner},   # Backup pipeline
    "VF-32":  {"owner_label": claude_owner},   # Encryption
    "VF-42":  {"owner_label": claude_owner},   # Agent notes model
    "VF-43":  {"owner_label": claude_owner},   # Token provisioning
    "VF-57":  {"owner_label": claude_owner},   # Onboarding locale
    "VF-72":  {"owner_label": claude_owner},   # short_description - already done, stale
    "VF-81":  {"owner_label": claude_owner, "phase_id": phase_by_name.get("triage", "")},  # State transitions
    "VF-82":  {"owner_label": claude_owner, "phase_id": phase_by_name.get("triage", "")},  # Archive UI
    "VF-88":  {"owner_label": claude_owner, "phase_id": phase_by_name.get("triage", "")},  # Rich text editor
    "VF-96":  {"owner_label": parvez_owner},   # Gantt UX - human design decision
    "VF-93":  {"owner_label": claude_owner},   # Dropdown bug
    "VF-99":  {"owner_label": claude_owner, "phase_id": phase_by_name.get("triage", "")},  # Notification pipeline
    "VF-100": {"owner_label": claude_owner, "phase_id": phase_by_name.get("triage", "")},  # Needs review auto-notify
    "VF-101": {"owner_label": claude_owner, "phase_id": phase_by_name.get("triage", "")},  # Infra dashboard
    "VF-102": {"owner_label": claude_owner},   # Refresh bug
}

count = 0
for t in tasks:
    sid = t.get("short_id", "")
    if sid not in fixes:
        continue

    fix = fixes[sid]

    # Phase change needs reason
    if "phase_id" in fix and fix["phase_id"] and fix["phase_id"] != (t.get("phase_id") or ""):
        fix["phase_change_reason"] = "Assigned to Triage phase during task cleanup"

    data = json.dumps(fix).encode()
    req = urllib.request.Request(f"{API}/tasks/{t['id']}", data=data, headers=HEADERS, method="PATCH")
    try:
        urllib.request.urlopen(req)
        count += 1
        print(f"  OK {sid}: {list(fix.keys())}")
    except Exception as e:
        print(f"  FAIL {sid}: {e}")

print(f"\n{count} tasks fixed")
