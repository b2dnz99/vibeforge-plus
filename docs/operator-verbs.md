---
title: "Operator verbs — recommended vocabulary for talking to your AI agent"
audience: public
status: 0.7.1-PRE-RC
version: 1.1.0
last_updated: 2026-05-09
authors: Parvez Khan + Claude (AI co-author)
---

# Operator verbs

VibeForge+ assumes a basic loop: the operator asks an AI agent to do something; the agent does the work and updates the board via its API token; the board's state survives the agent's session ending.

This page lists short verbs that map cleanly onto API operations the agent already knows from its contract. Using them produces more predictable agent behaviour than ad-hoc phrasing, but it isn't a CLI — there's no parser. They're conventions.

## What the agent CAN and CANNOT do

Before listing verbs, read this once — it explains why some "obvious" verbs don't work.

The agent has a **Bearer token scoped to one project**. With that token it can:

- ✅ Read and write tasks, notes, status, relationships in **its own project**
- ✅ Hit `/me` to learn its own identity
- ✅ Hit `/agentnotes` to refresh its contract
- ✅ Read activity events scoped to its project

The agent CANNOT:

- ❌ See or write to **any other project**
- ❌ Hit any `/admin/*` endpoint (those need an SA cookie, not a Bearer token)
- ❌ See system-wide audit, all-agent telemetry, all-user lists, or cert state — those are admin-portal surfaces
- ❌ See drift state for OTHER agents (it can introspect its OWN state; cross-agent visibility is admin-only)

So when a verb requires admin-portal data, that's the **operator** opening their browser, not the agent fulfilling via API. The verbs below are split accordingly.

## Verbs the agent fulfils (via API, with its own token)

### Reading the board

| Verb | What the agent does |
|---|---|
| `checktasks` / `what's on the board for me` | GET tasks owned by your operator handle in the project. Renders the card-face shape (short_description + status + priority). |
| `what's needs_review` | GET tasks with `status=needs_review` in the project. These are typically tasks the agent finished and is waiting for the operator to validate. |
| `what's blocked` | GET tasks with `status=blocked`. Each one carries `blocked_by_task_id` + reason — the agent renders the chain. |
| `what's in progress for me` | GET tasks owned by the agent's display name with `status=in_progress`. Useful "where was I?" check after a `/clear`. |
| `summarise recent activity on this project` | GET project activity events for last N hours and produce a short summary. Project-scoped only. |
| `show me the project resume` | GET the project's `resume_summary` field — the short "what is this project for" the operator wrote at creation. |

### Writing to the board

| Verb | What the agent does |
|---|---|
| `make a task for X` | POST a new task with title=X, status=`backlog`, owner_label=`human:<your name>`. Agent will ask for phase + priority if context doesn't make them obvious. |
| `mark X in progress` | PATCH the task referenced by X (matches by short_id like `XYZ-123` or fuzzy title) to `status=in_progress` with a `transition_note`. |
| `close out X with note Y` | PATCH the task to `status=done` with `transition_note=Y`. The system will also require a `docs_state` declaration; the agent decides what fits. |
| `cancel X because Z` | PATCH the task to `status=cancelled` with `abandoned_note=Z` (≥10 chars enforced). |
| `block X on Y because Z` | PATCH X with `blocked_by_task_id=Y`, `blocked_by_reason=Z`. |
| `relate X and Y because Z` | POST `/tasks/X/related` with `other_task_id=Y`, `reason=Z`. |
| `post a note on X: <text>` | POST a note on X with `body=<text>`. The agent decides whether to mark it as a completion note. |

### Working a task end-to-end

| Verb | What the agent does |
|---|---|
| `pick up X` | Set X to `in_progress`, post a "starting work on this" note, read the description + recent notes to ground itself. |
| `progress on X: <update>` | Post an interim note on X. Doesn't change status. Use to keep the conversation feed fresh during long work. |
| `flag X for review` | Set X to `needs_review`, set owner to the operator (a human), write a `transition_note` explaining what was done. The system requires `docs_state` + `docs_note` ≥30 chars on this transition — the agent will produce both. |
| `force-finish substep N on the wizard` | Operator escape hatch for the agent-onboarding wizard. Used rarely. Requires a 30-char rationale. |

## Verbs YOU do in the browser (admin portal)

These need the operator (or whoever has SA access) to open the admin portal at `/admin/login`. The agent cannot fulfil them — its token doesn't reach those endpoints.

| Verb / intent | Where to do it |
|---|---|
| **See drift state across all agents** | `/admin/portal/administration/agent-telemetry-and-drift` — per-agent eval pass/fail history + system-wide drift toggle. |
| **System-wide audit log** | `/admin/portal/administration/audit` — every mutation across the whole install. Agent-scoped audit per-project is API-accessible; cross-project isn't. |
| **Issue / cycle / revoke an agent token** | `/admin/portal/administration/agents` — token CRUD. Don't try to do this conversationally with an agent; it's a high-trust admin action. |
| **Manage users (create, suspend, role change)** | `/admin/portal/administration/users` |
| **Cert state + rotation** | `/admin/portal/configuration/certificates` — wizard for swap-mode + reload. |
| **Session / token TTL knobs** | `/admin/portal/configuration/session-policy` |
| **Container + TLS health snapshot** | `/admin/portal/health/overview` |
| **What host am I on / tier banner** | `/admin/portal/lifecycle/environment` — last sanity check before destructive action. |

## Verbs the agent self-uses (you don't say these out loud)

A few patterns are built into the agent contract — the agent does them automatically, you don't trigger them:

| Built-in pattern | Trigger | What the agent does |
|---|---|---|
| Refresh contract | Drift gate fires (422 with `BOARD_GATE_TRIGGERED`) | GET `/agentnotes` — resets the agent's freshness clock. |
| Acknowledge gate question | 422 with `gate_reason: drift_eval_required` | Answer the session-state question via the `X-Drift-Response` header. Truthful answer required. |
| Relevance check before answering board-state questions | Built into the contract | GET fresh state from the API before asserting "task X is in status Y" — never answer from session memory. |

The corresponding **human-only** verb here:

| Verb | Where you do it |
|---|---|
| **Clear a stuck drift flag on a task** | `/admin/portal/administration/agent-telemetry-and-drift` (or `/api/v2/tasks/<id>/clear-drift` with SA cookie). Only humans can clear; agents can never self-clear. |

## Verbs you'd add per-project

Beyond the starter set, every project tends to grow its own verb vocabulary. Examples:

| Verb a team might add | Maps to |
|---|---|
| `ship to staging` | Their deploy script + tag the relevant tasks as deployed |
| `prep for review` | Run linters + tests + post a summary note + flip status |
| `audit secrets in PR` | Check the diff for accidentally-committed credentials |
| `update the agent contract` | Edit `CLAUDE.md` / `AGENTS.md` + bump version |
| `do a release notes pass` | Walk recent done-tasks + draft a changelog |

Add these to your project's `CLAUDE.md` or `AGENTS.md` (whichever your agents read). The format is up to you — a `Phrase aliases` section listing "when the user says X, do Y" works well.

## Anti-patterns

A few things to **avoid** because they consistently produce worse outcomes:

- **"Just figure it out"** — the agent will guess. The board will get inconsistent updates. Better: name the task or be explicit about scope.
- **"Cancel that"** — ambiguous reference + likely missing the required `abandoned_note` ≥10 chars. The system will 422 the agent. Better: `cancel X because Z` with a real reason.
- **"Update the docs"** — too broad. Better: `update the X doc to reflect Y` with a specific section.
- **"Close everything"** — closing a task triggers required-field discipline (`docs_state`, `transition_note`). Mass closes are noisy. Better: close one at a time with real rationale.
- **"Don't update the board this time"** — defeats the entire point of the system. The drift gate exists to catch this.
- **"Show me the audit log"** — the agent can show you project-scoped activity, but the system-wide audit is admin-portal only. Either be specific ("show me activity on this project") or open the portal yourself.

## A mental model

Verb conventions are a shared glossary between operator and agent. The system enforces a small core (drift gate, status transitions, required fields); the operator and agent build the rest in the project's `CLAUDE.md` / `AGENTS.md`. A new agent picking up an existing project should be able to follow the project's verb conventions from message one.
