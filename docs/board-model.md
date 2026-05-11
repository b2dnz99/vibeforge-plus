---
title: "Board model — entities, relationships, state machine"
audience: public
status: 0.7.0-PRE-RC
version: 1.1.0
last_updated: 2026-05-09
authors: Parvez Khan + Claude (AI co-author)
---

# Board model

What's possible on the board, in one document. Read this once when you start, then refer back when you forget what status a task can move into.

## The entities

```
   ┌──────────────────────────────────────────┐
   │                Project                   │
   │  slug, name, description, status         │
   │  resume_summary (the "what is this for") │
   │  lifecycle_log (audit-trailed events)    │
   └────┬───────┬───────┬──────┬──────┬──────┘
        │       │       │      │      │
        ▼       ▼       ▼      ▼      ▼
   Milestones Members Agents  Tasks  Notes (project-level)
        │
        ▼
     Phases (each phase belongs to a milestone)
        │
        ▼
     Tasks (each task belongs to a project; optionally references
            a phase + milestone for grouping)
        │
        ▼
     Notes (per-task; the conversation feed)
```

Five primitives. That's the whole vocabulary.

| Entity | What it is | Cardinality |
|---|---|---|
| **Project** | The container. One project = one bounded effort. | Many per install |
| **Milestone** | Top-level grouping inside a project. Has a label, name, optional target date, status. Think "release markers" or "delivery checkpoints". | Many per project |
| **Phase** | Sub-grouping inside a milestone. Has a name + sort order. Think "named sub-stages of a milestone". | Many per milestone |
| **Task** | The unit of work. Has status, owner, notes, relationships. References its phase + milestone. | Many per project |
| **Note** | The conversation. Posted by humans or agents on tasks. Audit-trailed. | Many per task |

Plus three supporting concepts:

| Concept | What it is |
|---|---|
| **Member** | A user added to a project with a role (admin / write / read) |
| **Agent** | An AI agent token scoped to one project; a non-human board participant |
| **Activity event** | The audit row written when anything mutates (task create, status change, note posted, etc.) |

## Tasks are the central object

Almost everything you do on the board is "do something to a task." A task carries:

- `title`, `short_description` (card face), `description` (full body)
- `status` (see state machine below)
- `owner_label` (who's responsible — `human:Alice` or `agent:Claude`)
- `priority` (`low` / `medium` / `high` / `critical`)
- `phase_id` (which phase it belongs to)
- `milestone_label` (optional sub-grouping)
- `task_type` (`bug` / `feature` / `chore` / etc.)
- `blocked_by_task_id` (if blocked on another task)
- `start_date`, `due_date`
- `notes` (the conversation feed — see below)
- `relationships` (other tasks this one is related to)

## Task status state machine

```
                  ┌──────────┐
                  │ backlog  │
                  └─────┬────┘
                        │ pick up
                        ▼
                  ┌──────────┐
                  │  ready   │  (claimed but not started)
                  └─────┬────┘
                        │ start
                        ▼
                  ┌────────────┐                  ┌─────────┐
       ┌──────────│ in_progress│ ──── blocks ────▶│ blocked │
       │          └─────┬──────┘                  └────┬────┘
       │                │                              │
       │     finish + open for review                  │
       │                ▼                              │
       │          ┌────────────┐                       │
       │          │needs_review│ ◀─────────unblock ────┘
       │          └─────┬──────┘
       │                │ accept
       │                ▼
       │          ┌──────────┐
       └─────────▶│   done   │  (terminal — work shipped, validated)
                  └──────────┘
                       │
                       │ (rare: re-open if regression)
                       ▼
                  back to in_progress

                  ┌────────────┐
                  │  cancelled │  (terminal — abandoned with rationale)
                  └────────────┘
```

**Terminal statuses:** `done` and `cancelled`. Tasks rarely move out of these (a re-open is a deliberate operator action, not normal flow).

**Required-field discipline:** every status transition needs a `transition_note` explaining why. Cancellation needs an `abandoned_note` (≥10 chars). Moving to `needs_review` needs an explicit human owner + a `docs_state` declaration (whether docs are needed/exists/updated/created/skipped, plus a `docs_note` ≥30 chars). The system enforces these with structured 422 responses — the agent retries with the missing field; the operator never has to remember.

## Notes — the conversation feed

Every task has notes. Two kinds:

- **Human notes** — written by an operator, plain text or rich text
- **Agent notes** — posted by an AI agent, supports HTML (so the agent can format lists, headers, code blocks)

Notes are append-only. You can't edit or delete a posted note (audit-trail discipline). If you need to correct, post a new note.

The notes feed is the **primary conversation surface** between operator and agent. The agent reads recent notes when picking up a task; the operator reads notes when reviewing what happened.

## Relationships

Tasks can reference each other in two ways:

- **Hard dependency** — `blocked_by_task_id` on the dependent task, plus a `blocked_by_reason` (≥10 chars). The dependent's status can't progress until the blocker resolves.
- **Soft relation** — recorded via `POST /tasks/{id}/related` with the other task's id + a reason. Bidirectional, queryable, audit-trailed. Use for "this is related but not blocking."

Avoid putting `"related: TASK-123"` in prose — it's unqueryable + unidirectional + drops out of audit signal. Use the structured relationship endpoint.

## Milestones

Milestones are the top-level grouping inside a project. Each milestone has a `label` (short identifier like "M1" or "Auth & Access"), a `name` (human-readable description), an optional `target_date` (renders as a Gantt diamond), and a `status` (`active` / `complete` / `deferred`). Use them as release markers or delivery checkpoints.

## Phases

Phases are the sub-grouping inside a milestone. Each phase has a `name` (e.g. "Identity", "Tokens", "Permissions"), a sort order, and a status. A phase belongs to one milestone via `milestone_id` — the schema enforces that link.

A task without a phase is in the **Triage** default phase — the catch-all. Aim to get every task out of Triage and into a real phase quickly; it's a default, not a destination.

## Activity events

Every mutation writes a row to the activity log:

- task created / updated / status changed
- note posted
- agent token issued / cycled / revoked
- user account created / suspended / restored
- project renamed / archived / reopened
- … etc.

Each row carries `actor_user_id` (who did it), `actor_type` (`human` or `agent`), `action` (the verb), `details` (the payload), and a timestamp. The admin portal renders these in the audit log.

## Cross-project: not really a thing

The board is **project-scoped by design**. Agents are scoped to one project. Members are added per project. Search, filter, and queries default to project-scoped.

There's a thin cross-project surface for operators (you can see all projects you're a member of from the home page), but the work happens inside a project. If you need a "company-wide view" of everything, this isn't that tool.

## What's not in the model

- Story points / effort estimates
- Sprint planning + burndown
- Time tracking
- Custom fields
- Workflows beyond the status state machine above
- Cross-project dependencies

These are not present in this release. If they matter to your workflow, this probably isn't the right tool.
