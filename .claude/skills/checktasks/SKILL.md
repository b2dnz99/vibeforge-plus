---
name: checktasks
description: Check tasks assigned to the agent in the current project
disable-model-invocation: true
---

# /checktasks — My Assigned Tasks

Check the board for tasks assigned to you (the agent) in the active project.

## Steps

1. Source credentials from `.agent-config` (never display tokens)
2. Fetch all tasks from the active project:
   ```
   GET $VIBEFORGE_API/projects/vibeforge-plus/tasks/
   ```
3. Filter for tasks where `assigned_to` contains "claude" or "agent" (case-insensitive)
4. Group results by status and display:

```
═══ MY TASKS (Agent) ═══

In Progress:
  • VF-XX: {title}

Ready:
  • VF-XX: {title}

Needs Review:
  • VF-XX: {title}

Backlog:
  • VF-XX: {title}

Total: {count} assigned
```

5. If no tasks are assigned, say so and suggest asking the user what to pick up.

## Rules

- Only show non-terminal tasks (exclude done, cancelled)
- Always fetch fresh from the API — never use cached data
- Never display tokens in commands or output
