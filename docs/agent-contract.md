---
title: VibeForge+ Agent Contract Architecture
audience: public
status: 0.7.0-PRE-RC
version: 1.1.0
contract_version_at_writing: 2.14.3
last_updated: 2026-05-09
authors: Parvez Khan + Claude (AI co-author)
siblings:
 - 0-MD/0-Documentation/public/user-agent-model.md (agent identity, v3)
 - 0-MD/0-Documentation/public/identity-roles.md (human identity tiers — SA / SU / User / Viewer)
 - 0-MD/0-Documentation/public/activity-model.md (effort / period tracking)
 - 0-MD/0-Documentation/proposed/BOARD-PURPOSE-AND-PACT-PROPOSAL.md (purpose + reciprocal pact, future)
references:
 - 0-MD/0-Documentation/public/AGENT-CONTRACT.html (auto-generated machine-derived contract content — operational reference)
 - app/api/v2/contract.py (the source code that produces the JSON contract)
ip: novel
style: technical
ip_first_dated: 2026-04-07
ip_authors: [pkhan, claude]
ip_disclosure_path: published
ip_summary: Two-tier API-served agent contract pattern: a single endpoint (/agentnotes) returns either minimal token-acquisition instructions (unauthenticated) or a full operational contract with embedded workflows, CLAUDE.md template, and project-specific context (authenticated). Workflows are embedded in the contract response itself rather than living as external skill files, making the contract self-contained and editor-agnostic. Versioned, auto-rendered to HTML, and live-served — the agent fetches the current contract on every session start, eliminating drift between deployed code and agent expectations.
---

# Agent Contract Architecture

> **What this is.** A the maintainer-level explanation of how AI agents become participants on VibeForge+: how they connect, what the contract gives them, what the rules require, how the rules are enforced, and how the contract evolves. Hand-written architectural artefact, sibling to [user-agent-model.md](user-agent-model.md) (v3, supersedes former auth-agent.md), [identity-roles.md](identity-roles.md) (human role tiers), and [activity-model.md](activity-model.md).
>
> **What this is NOT.** It is not the contract itself. The actual contract content (every endpoint, every rule, every field) is served live at `/agentnotes` and rendered to a separate auto-generated reference at `0-MD/AGENT-CONTRACT.html`. This doc explains the *model*; that one shows the *content*.

---

## Overview

Most APIs treat AI agents as anonymous clients with API keys. VibeForge+ treats them as **first-class identities with a documented operating contract**. The contract is the deal we strike with the agent at the moment it connects: here is who you are, here is what you can do, here is what you must always do, here is what you cannot do, here is how to participate honestly.

The contract is structured, machine-readable, served live from a single endpoint, and **enforced by the API at the route level**. An agent that doesn't follow the contract gets `422`'d, not scolded. The contract is physical — every rule has a corresponding gate in code.

There are three artefacts that together define the agent experience:

1. **The JSON contract** at `/agentnotes` (and `/agentnotes/{slug}` for project-scoped) — generated dynamically by `app/api/v2/contract.py`. Machine-first. ~35KB authenticated. This is what agents fetch.
2. **The HTML rendering** at `0-MD/AGENT-CONTRACT.html` — auto-generated from the JSON via `scripts/generate_contract_html.js`. Human-readable but mechanically derived. Use this when you need to look up "what does the contract literally say right now."
3. **This doc** (`AGENT-CONTRACT-ARCHITECTURE.md`) — hand-written, explanatory, structural. Use this when you need to understand why the contract is shaped the way it is, what the lifecycle of an agent looks like, what problems the contract solves, and how it ties into the rest of the architecture.

This separation matters. The JSON is the **source**. The auto-HTML is the **reference**. This doc is the **map**. Three audiences, one model.

---

## 1. What an agent is, in this system

An agent on VibeForge+ is an authenticated identity with these properties:

- A **name** (e.g. "Claude", "test-agent", "Codex")
- A **slug** that's globally unique and follows `{project_slug}-{name_lower}`
- A **project scope** — every agent belongs to exactly one project and cannot read or write any other project
- A **status** — `active`, `suspended`, or `revoked`
- A **token** — bcrypt-hashed in the DB, never stored in plaintext
- An **operator** (post an internal release) — the human who owns the active token
- A **model_type** — `claude`, `codex`, `custom`, etc — set by the human at creation
- A **model_name** — self-reported by the agent (tells the board "I'm running Claude Opus 4.6")
- A **`last_seen_at`** heartbeat timestamp updated on every authenticated API call
- A **`created_by`** field naming the human who provisioned it

Agents are **first-class participants**. They appear as members of their project, get @mentioned in notes, post visible work, get assigned to tasks, get held accountable in the audit trail. They're not anonymous API clients — they have identities the board recognises and tracks.

What an agent **is not**:

- It is not a project manager. It executes work, it doesn't decide what to work on.
- It is not autonomous. It always works under a human's direction.
- It is not anonymous. Every action is attributed to it personally and (post an internal release) to its operator.
- It is not trusted to close work. It moves things to `needs_review`; only humans mark `done`.

This shapes everything else in the contract.

---

## 2. The three contract artefacts — JSON, auto-HTML, this doc

```
                         ┌─────────────────────────┐
                         │    contract.py          │
                         │  (Python source)        │
                         └───────────┬─────────────┘
                                     │
                          dynamic generation
                                     │
                                     ▼
                    ┌────────────────────────────┐
                    │   /agentnotes  JSON         │  ← agents fetch this
                    │   ~35KB authenticated       │     on every session start
                    │   Live, real-time content   │
                    └─────────┬───────────────┬───┘
                              │               │
            ┌─────────────────┘               └────────────────┐
            │ generate_contract_html.js                        │
            ▼                                                   ▼
  ┌──────────────────────┐                       ┌─────────────────────────┐
  │  AGENT-CONTRACT.html │                       │  THIS DOC               │
  │  Auto-generated      │                       │  AGENT-CONTRACT-        │
  │  Operational ref     │                       │  ARCHITECTURE.md        │
  │  (machine-derived)   │                       │  (hand-written)         │
  └──────────────────────┘                       └─────────────────────────┘
       ▲                                                   ▲
       │                                                   │
   "what does it                                    "why is it this
    literally say"                                   way and how does
                                                     it work as a model"
```

Three artefacts, one underlying reality.

| Artefact | Source | Audience | Question answered |
|---|---|---|---|
| `/agentnotes` JSON | dynamic from `contract.py` | Agents | "Give me the operating manual right now" |
| `AGENT-CONTRACT.html` | generated from JSON | Humans needing exact content | "What does the contract literally say?" |
| This doc | hand-written | the maintainer, architects, integrators, agents wanting context | "Why is the contract structured this way? What's the model?" |

When the contract content changes, `contract.py` is the place to edit. `AGENT-CONTRACT.html` is regenerated automatically as part of the sync workflow. This doc is updated when the *model* changes — new lifecycle states, new enforcement categories, new architectural decisions — not every time a rule wording is tweaked.

---

## 3. Agent lifecycle

An agent goes through a lifecycle from creation to retirement. Understanding the states and the transitions is the foundation for everything else.

```
       (none)
          │
          │  human creates agent
          │  (project owner / admin / SU / SA)
          ▼
   ┌────────────────────┐
   │  active, no token  │  ← agent exists, can't authenticate yet
   └─────────┬──────────┘
             │
             │  human issues token
             │  (any project member with write+)
             │  caller becomes operator
             ▼
   ┌────────────────────┐
   │  active, has token │  ← agent can call the API
   │  participating     │     hits /agentnotes, runs bootstrap,
   │                    │     picks up tasks, posts notes
   └────┬───────┬───────┘
        │       │
        │       │  human cycles token
        │       │  (any project member with write+)
        │       │  caller becomes new operator
        │       │  ─────────────────┐
        │       │                   │
        │       │                   ▼
        │       │       ┌────────────────────┐
        │       │       │  active, has new   │
        │       │       │  token (different  │
        │       │       │  operator)          │
        │       │       └────────────────────┘
        │       │
        │       │  human revokes token
        │       │  (token owner / project admin / SU / SA)
        │       ▼
        │  ┌────────────────────┐
        │  │  active, no token  │  ← back to start of active life
        │  └────────────────────┘     awaiting re-issue
        │
        │  human revokes agent
        │  (project admin / SU / SA)
        ▼
   ┌────────────────────┐
   │  revoked           │  ← agent dead, token destroyed
   │                    │     can be restored
   └─────────┬──────────┘
             │
             │  human restores agent
             │  (project admin / SU / SA)
             │  agent comes back with NO token
             │  must explicitly issue
             ▼
   ┌────────────────────┐
   │  active, no token  │
   └────────────────────┘
```

The **active-without-token** state is real and intentional. It happens when:

1. An agent has just been created but no token has been issued yet
2. A revoked agent has been restored but no new token has been issued
3. The token was explicitly revoked but the agent itself was kept active

In all three cases the UI shows an "Issue New Token" button and the agent cannot make API calls until that happens.

> **v3 update (2026-04-19):** The "cycle = ownership transfer" mechanic described below was removed in v3. Under the current model, cycling a token issues a fresh token for the **same** agent; operator does not change. See [user-agent-model.md](user-agent-model.md) §1 and §4 for the current model. The paragraph below is retained for historical context only.

~~The **token cycle is an ownership transfer**. Whoever cycles becomes the operator. This is unusual — most systems treat cycling as a noop on identity — but VibeForge+ ties effort attribution to whoever holds the active token.~~ *(Removed in v3.)*

---

## 4. Bootstrap — how an agent becomes a participant

> **Two onboarding pathways co-exist** *(CONTRACT 2.10.0+, R2.5/R2.6 wave)*:
>
> 1. **Wizard pathway (primary for new projects).** A human visits `/ui/test-wizard`, picks a slug, optionally provides a build prompt, and the wizard provisions: project record, owner membership, prefix, default phases/milestones, the agent identity, and a one-shot token. The wizard then surfaces a copy-pasteable bootstrap prompt the human gives the agent. The agent's job from there is just to *land* — read its token from the file the human just dropped, then walk the 7-step ritual below to verify it. This is the path almost every fresh customer onboard takes.
> 2. **Direct provisioning (legacy + power-user).** A human (the maintainer or board admin) creates the project + agent + token via API or admin portal directly, hands the agent the token, and the agent walks the 7-step ritual cold. Used when scripting a new project from CI, or when iterating on dogfood without going through the wizard UI.
>
> The 7 steps below describe the **agent-side technical contract** in both cases — what the agent must verify before declaring itself ready. The wizard pathway just front-loads the human-side provisioning so the agent doesn't have to ask the human a series of "what's your slug? what's your project prefix?" questions. Either way, the same `/agentnotes/{slug}` fetch + `/me` round-trip + first task lookup happens.

Bootstrap is the **first-session ritual** an agent runs to go from "I have a token" to "I'm ready to work." It's seven steps. Each step exists to prevent a specific failure mode.

```
   ┌───────────────────┐
   │  Step 1:          │
   │  Identify project │  Which project am I working on?
   │                   │  Single-project agents skip past automatically.
   └─────────┬─────────┘
             │
   ┌─────────▼─────────┐
   │  Step 2:          │
   │  Get token        │  Read .agent-config OR temp agent-token.txt file.
   │  securely         │  NEVER ask the human to paste the token in chat.
   │                   │  Add both files to .gitignore. Delete the temp.
   └─────────┬─────────┘
             │
   ┌─────────▼─────────┐
   │  Step 3:          │
   │  Create CLAUDE.md │  Write the per-agent rules file from the
   │  (or AGENTS.md)   │  agents_md_template returned by the contract.
   │                   │  This is the agent's local copy of the rules.
   └─────────┬─────────┘
             │
   ┌─────────▼─────────┐
   │  Step 4:          │
   │  Verify identity  │  GET /api/v2/me with the bearer token.
   │                   │  Confirms: my name, my project, my task counts.
   │                   │  If 401, the token is wrong.
   └─────────┬─────────┘
             │
   ┌─────────▼─────────┐
   │  Step 5:          │
   │  Fetch project    │  GET /api/v2/agentnotes/{slug}
   │  contract         │  Gets the FULL contract: endpoints, rules,
   │                   │  reviewers list, project context.
   │                   │  Public /agentnotes is just a stub.
   └─────────┬─────────┘
             │
   ┌─────────▼─────────┐
   │  Step 6:          │
   │  Verify           │  GET /api/v2/projects/{slug}/tasks
   │  connectivity     │  200 OK with task array means everything works.
   └─────────┬─────────┘
             │
   ┌─────────▼─────────┐
   │  Step 7:          │
   │  Check in         │  Filter tasks for ones assigned to me. Pick one
   │                   │  up if any are 'ready'. Otherwise ask the human.
   │                   │  Agent is now bootstrapped and participating.
   └───────────────────┘
```

**Why each step exists:**

| Step | Failure mode it prevents |
|---|---|
| 1 | Agent works on the wrong project (cross-project leak attempt) |
| 2 | Token leaks via chat history, git, or screen share |
| 3 | Agent forgets the rules mid-session and improvises |
| 4 | Agent works for an hour with a bad token then 401s on every call |
| 5 | Agent uses the public stub instead of the rich project-scoped contract |
| 6 | Agent assumes the API works without verifying |
| 7 | Agent starts work without knowing what's already in flight |

The bootstrap is the **first chance** to fail honestly — and the cheapest place to catch problems before they pollute the board.

---

## 5. The CLAUDE.md / AGENTS.md template

Step 3 of bootstrap generates a per-agent **discipline manifest** from a template inside the contract response. The naming is intentional:

- **`AGENTS.md`** is the convention most AI coding tools (Codex, Cursor, generic) read by default
- **`CLAUDE.md`** is the Claude-vendor native name; the editor only auto-loads its native filename
- Content is identical regardless of filename — the wizard writes it under the vendor's expected name

The file lives in the agent's working directory (the repo root or wherever the editor session opens). It is **deliberately thin** (~25 lines from `_claude_md_template` at `app/api/v2/contract.py:877`):

```
# Discipline manifest — <project name>
# Board: <base_url> | Project: <slug> (<prefix>)
# Write this file as CLAUDE.md (Claude vendors) or AGENTS.md (Codex / Cursor / generic)

## Security — MANDATORY
  (4 lines: source from .agent-config, never display tokens, gitignore .agent-config,
   ask the human if creds missing)

## Board
  API + GET /agentnotes/{slug} (contract) + GET /me (identity).
  "Read your project contract at the endpoint above for all API endpoints,
   task rules, enforcement gates, workflows, and session protocol."

## Project Rules
  (placeholder for the human + agent to fill in with project-specific local context —
   tech stack, conventions, human preferences. Survives across contract refreshes.)
```

The template is **generated per-agent per-project** because it embeds the project slug, prefix, and base URL inline. But it is **not the contract** — it is a **bootstrap pointer**. Three security rules + the contract endpoint + a placeholder for project-local context. That's it.

**The rules live at `/agentnotes`, not in this file.** The agent fetches `/agentnotes/{slug}` on every session start to get the current contract (rules, endpoints, workflows, gates, design principles, project membership). When `CONTRACT_VERSION` bumps, the agent re-reads `/agentnotes` and gets the new shape automatically — the on-disk manifest does not need rewriting (that's the historical anchor that drove the restructure: when Codex hit a 10k-char limit on contract responses, the model flipped from "embed the rules in the manifest" to "thin pointer + rules at /agentnotes"). For the same reason, contract drift surfaces as `BOARD_GATE_TRIGGERED gate_reason=contract_drift` (the agent acted on stale contract knowledge) — the recovery is `GET /agentnotes` + capture `refresh_nonce`, not "rewrite my CLAUDE.md."

**Why the section under `## Project Rules` matters more than it looks:** that's where customer-specific context accretes. Tech stack ("we're on Postgres + FastAPI + Vue"), conventions ("commit messages start with the ticket id"), human preferences ("don't auto-commit; surface the diff first"). It's the one section the customer's discipline survives across contract refreshes — the OUR-block above gets re-rendered on `CONTRACT_VERSION` bumps, the project rules don't. Treat it as the local working surface; treat the rest as immutable bootstrap framing.

---

## 5.5 Lifecycle and dogfood/customer asymmetry

This section captures a load-bearing thesis that fresh agents have repeatedly needed re-teaching. It governs how to read everything else in this doc, so it goes here, ahead of the rules categories.

### The asymmetry

The codebase running this contract — VibeForge+ itself — is a **dogfood project**. We use VibeForge+ to develop VibeForge+. That makes our setup structurally different from the customer setup the contract is designed for, in three ways that matter:

| Dimension | Dogfood (this repo) | Customer (a project using VibeForge+) |
|---|---|---|
| **Who writes the contract** | We do. `app/api/v2/contract.py` is the source. | They don't. The contract is `pip install`-style infrastructure they receive. |
| **CLAUDE.md shape** | Hand-curated, ~200 lines, evolves frequently. | Thin pointer (~25 lines from `_claude_md_template`). Evolves rarely. |
| **Contract evolution cadence** | Contract bumps every few days. We are the rule-evolvers. | Contract is functionally static for the customer. They `GET /agentnotes` and receive whatever we have shipped. |

If a fresh agent (or fresh human reader) models the customer setup on what they see in this repo's parent `CLAUDE.md`, they will write a 200-line discipline manifest for the customer. That is wrong. The customer's manifest is the thin pointer. Our manifest is fat because we evolve the rules, not because rules belong in the manifest.

### The board as canonical source

The board is the canonical source of agent rules. `/agentnotes` is the API surface. `CLAUDE.md` is **intentionally thin** — a bootstrap pointer to the board. Rules do not get duplicated locally; they live at `/agentnotes` and the agent re-reads them on every session start. This is what makes the contract drift gate work (`gate_reason=contract_drift` fires when the board detects an agent acted on stale contract knowledge — recovery is to re-`GET /agentnotes`, not to rewrite a local file).

### Why this matters operationally

Two design moves follow directly from the asymmetry:

1. **Anything that goes into the contract goes into `contract.py` first** — not into a CLAUDE.md somewhere. Customer agents will never see a rule that lives only in our parent `CLAUDE.md`. The minute a discipline rule is general (vs project-specific), its home is `contract.py` so it ships to every customer's `/agentnotes`.

2. **The customer's CLAUDE.md is treated as immutable bootstrap framing** by the wizard. The customer doesn't iterate it. They iterate the **`## Project Rules`** section at the bottom (their tech stack, conventions, human preferences). Everything above stays as the wizard wrote it. The OUR-block content is refreshed by the board on `CONTRACT_VERSION` bumps; the local section accumulates project-specific context.

### The 10-year context claim

The thesis the board carries is this: **agent compliance ~85% × structured-via-mechanical-features = the board becomes a queryable durable artefact.** Rules enforced by gates (not just narrated) get followed. Rules that get followed leave audit trails. Audit trails accumulated over months become portable history when a project outgrows VibeForge+ and migrates to Jira / Linear / GitHub Projects / wherever.

Without this discipline the board is decoration (notes go in chat, transitions are guesses, relationships are prose). With it the board carries the *why* of every decision across every session of every agent on the project. That's the bet. Every gate, every required-reason field, every structured `/related` over inline-prose, every drift-eval question — all of those are forcing functions in service of this 10-year-context claim.

### Practical takeaways for an agent reading this doc

- **You are reading a contract.** The rules in `/agentnotes` are not aspirational; they are gated. Acting on stale knowledge will trigger the drift gate.
- **Your `CLAUDE.md` is a pointer, not the contract.** When in doubt, `GET /agentnotes`. Do not "remember" rules from a local file — they may be stale.
- **Your `## Project Rules` section is yours.** Everything else is the board's. Don't edit the OUR-block; it gets re-rendered on the next refresh.
- **If you're a Claude session reading this doc inside the dogfood repo (VibeForge+ developing itself), you'll see a 200-line parent `CLAUDE.md`. That is not the model for what you write into a customer project.** The customer's manifest is `_claude_md_template`'s output (~25 lines).

---

## 6. Categories of rules in the contract

The contract content groups its rules into a small number of categories. Knowing the categories is more useful than memorising the rules — when something feels wrong you can ask "which category does this fall into?" and find the answer.

### 6a. Identity enforcement

Rules that prevent agents from impersonating humans or each other.

- Notes posted by an agent token always have `author_type` forced to `agent` and `author_name` forced to the agent's registered name. No payload field can override this.
- The `is_completion_note` flag is forced to `false` on agent-posted notes. Only humans can mark notes as completion.
- The Bearer token is the identity. There is no way for an agent to "act as" a different agent or as a human via the API.

### 6b. Scope enforcement

Rules that prevent agents from reading or writing data outside their assigned project.

- Every authenticated route checks `agent.project_id` against the requested project. Wrong project = 403.
- Cross-project endpoints (`/users`, cross-project `/members`, cross-project `/dashboard`) are gated for agents.
- The agent's `available_projects` list in the contract response is filtered to only its own project.

### 6c. Lifecycle enforcement

Rules that prevent agents from making decisions reserved for humans.

- Agent cannot move a task to `done`. 422.
- Agent cannot move a task to `cancelled`. 422.
- Agent cannot move a task to `needs_review` without setting `owner_label = "human:<Display Name>"` of a real project member. 422.
- Agent cannot supersede or revert another author's notes — only its own.
- Supersede and revert are blocked on `done` tasks — must reopen first.
- Reverted notes lose their completion flag — closure authority requires a fresh completion note.

### 6d. Quality enforcement

Rules that make the board self-documenting as a side effect of normal work.

- Every status transition requires a `transition_note` explaining why.
- `cancelled` requires a non-empty `abandoned_note`.
- Notes should be structured (Problem / Fix / Scope / Test) for scannability.
- `needs_review` notes should include an `@mention` of the reviewer and numbered test steps the human can follow.
- `short_description` (max 120 chars) should be filled for every task — it's what shows on the card face.
- **Plain-text gate (added contract 2.4.0):** `title`, `short_description`, and `description` are plain text only. The API rejects HTML tags in these fields with 422. Captured reasoning, lists, sections, code blocks, and any rich content live in **notes** (which support HTML), not in description. Description answers *"what is this task"*; notes answer *"what was discussed, decided, or done"*.

### 6e. Discipline rules (read before writing)

Rules that govern how an agent fetches state before mutating it.

- Before starting work on any task, GET its notes. Notes contain prior context.
- Before any status update, GET the fresh task state. Don't trust cached or remembered state.
- If a task moved since the last read, the human change takes priority.
- Notes are additive — always safe to post.
- Status changes are competitive — latest human action wins.
- Re-read the board every 5 significant actions.
- On any PATCH that flips a task to `needs_review`, set the `docs_state` field — one of `updated` / `created` / `not_required` / `n_a` — and a `docs_note` explaining the choice. *(R2.7 wave 1.8.1.)* This is the agent's **self-assessment** of whether code changes in this task required matching doc changes (and whether they happened). Forces the agent to think about doc-vs-code drift at the moment of handoff rather than discovering it during human review. The board surfaces `docs_state` on the card so the human can verify the assessment without re-reading the diff.

### 6f. Sync expectations (when to push to the board)

Rules about timing — when changes must be synced rather than batched.

- On task change → PATCH the API immediately, don't batch.
- On note added → POST immediately.
- On session end → PUT a resume summary capturing current state.
- Periodic → re-read tasks every 5 significant actions.

These six categories cover essentially all the rules in the contract. They're enforced at different layers (identity in `_resolve_actor`, scope in `_check_human_project_access`, lifecycle in the patch route, quality in validation, discipline by convention, sync by contract) but they all serve the same goal: **make the board honest, present, and trustworthy.**

---

## 7. What the contract says, in English

> *Walking through the contract content as a story rather than a list. Read this if you want to understand the agent experience without parsing JSON.*

When an agent first connects to VibeForge+ unauthenticated, it gets a small stub that says "I am VibeForge+, here's my version, here's how to authenticate, ask your human for a token." Nothing useful for actual work — just enough for the agent to know it's hit the right server.

When the agent connects authenticated — Bearer token in the header — it gets the full contract. The contract opens with a self-introduction: "Here's who you are. Your name is X, your slug is Y, your project is Z, your model type is what your human said it was, your model name is whatever you've reported about yourself."

Then the contract gives the agent its **discovery checklist** — six steps that say "you are authenticated, this is your full operating manual, read the bootstrap section, follow the workflows, write the CLAUDE.md from the template, all the API endpoints are below." This is the agent's "you are here" map.

Then it lays out the **enums** the agent will use everywhere — the seven valid task statuses (`backlog`, `ready`, `in_progress`, `needs_review`, `blocked`, `done`, `cancelled`), the four valid priorities (`low`, `medium`, `high`, `critical`), and the **priority matrix** that tells the agent what each priority *means* in terms of urgency. `critical` means drop everything; `high` means next session; `medium` means within three sessions; `low` means backlog. This isn't decoration — it's the agent's job description.

Then the **board capabilities** section explains what kind of system this is. It says: VibeForge+ is a self-hosted project tracker designed for human-AI collaboration. Agents are first-class participants alongside humans. There's a hierarchy: project contains milestones, milestones contain phases, phases contain tasks, tasks have notes. Humans create projects and make priority decisions. Agents pick up tasks, write code, post structured notes, and move work to needs_review for human validation. Notes are the shared memory between humans and agents across sessions.

Inside board capabilities is the **planning guidance** — what the agent should do when helping a human plan a new project. Read existing docs, propose milestones, propose phases under each milestone, break phases into tasks. Also the **onboarding partner** flow — what to do when there are NO projects yet and the agent has to help the human create the first one through conversation.

Then the **design principles** — 24 of them across three groups:

- **Engineering principles** (9): DRY, KISS, Secure by Default, Least Privilege, Defence in Depth, Fail Secure, No Secrets in Output, Least Knowledge, Separation of Concerns
- **Work principles** (8): Document Decisions, Verify Before Asserting, Complete the Handoff, Contract is Law, Atomic Deploys, No Assumptions, Audit Your Own Work, Flag Debt
- **Visibility principles** (7): Explain Trade-offs, Estimate Impact, Seek Approval on Architecture, Plain English First, Revert Path, No Silent Side Effects, Progress Visibility

Each principle is one rule + one rationale. The agent is expected to internalise these and let them shape *how* it thinks, not just *what* it does. The principles are aspirational where the rules are mechanical — the rules say what's blocked, the principles say what's preferred.

Then the **code commentary conventions** — the WHY/RULE/FLOW/GATE comment pattern. WHY explains business or security reason, RULE references the contract rule being enforced, FLOW describes the user/agent journey, GATE marks a permission or validation checkpoint. This pattern makes code self-documenting in a specific way that an AI can re-parse later for context recovery.

Then the **task discipline** rules — the operational rules an agent follows on every task touch. Read notes before starting. Move to in_progress when work begins, with a transition_note. Move to needs_review when work needs validation, set owner_label to `human:<Display Name>`. Update immediately, don't batch. Use structured HTML in notes. Verify every API write.

Then the **board reconciliation** rules — how the agent handles concurrent edits with humans. Always GET fresh before writing. Latest human action wins. Notes are additive, status is competitive. Don't fight the human; if they moved something back, respect it.

Then the **endpoints** — the full API catalogue, grouped by category (tasks, notes, milestones, phases, members, project, triggers, relationships, artefacts, onboard). Each endpoint has method, path, description, and body schema. The agent treats this as its API client documentation — anything not listed here is not part of the contract and shouldn't be called.

Then the **agent enforcement** rules — the explicit list of what the API will reject and why. Agent cannot move to done (422). Agent cannot move to cancelled (422). Agent cannot move to needs_review without a human owner (422). Agent identity forced on note posts. Agents can only supersede their own notes. Supersede blocked on done tasks. Completion notes required before close.

Then the **bootstrap section** (described in §4 above), the **workflows** (checktasks + sync), and the **CLAUDE.md template** rendered with this agent's specific values.

Finally the **available_projects** list — but filtered to only the agent's own project, so agents can't enumerate other projects via the contract endpoint.

The contract is **complete** in the sense that an agent reading it once has everything it needs to participate honestly. There's no implicit knowledge required, no out-of-band information, no "ask the human if you're confused about how the API works." It's all there.

That's the contract. ~35KB of text that turns a generic AI into a participant on a specific board.

---

## 8. The API in human terms

> *A categorical tour of the API surface, in plain English. Use this to understand what an agent CAN do, then look up the exact endpoint signatures in the auto-generated `AGENT-CONTRACT.html` when you need to make a call.*

The API spans 10 categories of agent-relevant endpoints (tasks, notes, milestones, phases, members, project, triggers, relationships, artefacts, onboard) plus admin/portal/auth surfaces excluded from the agent contract. Here's what each agent-facing category is for, in narrative form.

### Tasks — read, create, update, audit

The biggest category. This is where the agent spends most of its time. There's a `list` endpoint for getting all tasks in a project, a `get` endpoint for fetching a single task by ID, a `create` endpoint for adding a new task with title/status/priority/owner/etc, a `patch` endpoint for updating any field, and an `audit` endpoint that returns the full activity history for a task. The patch endpoint is the most-used — every status change, owner change, priority bump, title rename, due date update goes through PATCH. Status changes require a `transition_note` explaining why, and agents are blocked from moving tasks to `done` or `cancelled`.

### Notes — the shared memory layer

Every meaningful change generates a note. There's a `list` endpoint for fetching all notes on a task (including superseded ones, with full supersede history), a `create` endpoint for posting a new note, and `supersede`/`revert` endpoints for striking through notes that are wrong without deleting them. Agents can only supersede their own notes — they cannot supersede human notes. Notes are the substrate for the **rewind protocol** described in the future BOARD-PURPOSE-AND-PACT doc — they're how an agent reconstructs context when it doesn't remember why something is the way it is.

### Milestones — major project checkpoints

A small category. Agents `list` milestones, `create` new ones (when planning a project), and trigger `close`/`reopen` on existing ones. Milestones are filter chips on the board, not work items themselves.

### Phases — work groupings inside milestones

Even smaller. `list` phases, `create` new ones. Phases show as badges on task cards and as swimlane dividers on the board. Phases belong to a milestone.

### Members — who's on this project

Two endpoints: `list` returns all human and agent members with their names, roles, and types. `mentionables` returns a slimmed list optimised for @mention autocomplete. The members endpoint is **the source of truth for resolving the human reviewer** when an agent moves a task to needs_review — the agent calls this, filters for `type=human`, picks one, and prefixes the name with `human:` for the owner_label field. Without this lookup the agent would have to hardcode reviewer names or guess.

### Project — resume + dashboard

Two endpoints. The `resume` endpoint lets the agent (or human) PUT a free-text summary of the project's current state. This is what the **next** agent reads on session start to rehydrate context. The `dashboard` endpoint returns task counts by status, milestone progress, and recent activity for the project drawer view.

### Triggers — placeholder for notifications

One endpoint right now: `mention`. It accepts an @mention event and currently does nothing (placeholder for a future notification pipeline). Agents post here when they @mention someone in a note, even though the system doesn't yet act on it.

### Relationships — structured task-to-task links

Three endpoints. `list` (`GET /tasks/{id}/relationships`) returns all linked tasks for a given task: blocked_by edge + blocks edges + related (soft) edges. `related_create` (`POST /tasks/{id}/related`) creates an audit-trailed, idempotent, queryable soft relation between two tasks (with a required `reason` ≥10 chars). `blocks_create` (`POST /tasks/{id}/blocks`) is the reverse-blocked-by direction (sets the target's blocked_by_task_id = this task; rejects if target already has a blocker). Replaces the older inline-prose pattern (`related: VF-XXX` in description) which was unqueryable + unidirectional + dropped the audit signal — agent contract now points to the structured endpoints.

### Artefacts — read-only fetch over already-persisted onboard state

One endpoint: `GET /projects/{slug}/artefacts/{type}` where type is `plan` | `agent_md` | `contract` | `handover`. Read-only over what onboard_state already captures. `plan` returns the initial_plan content + hash from substep 5; `agent_md` returns the discipline manifest content + hash from substep 6; `contract` is a 308 redirect to `/agentnotes/{slug}` (canonical contract source); `handover` returns 404 with a filesystem pointer (handover docs live at `0-MD/progress/`, not server-stored in this contract version — see backlog proposal at `0-MD/proposed/2026-05-04-server-side-artefact-lifecycle.md` for the design options). Cross-vendor portability win for agents without local filesystem access.

### Onboard — first-session ceremony surface

A small group: `framing` (GET — returns the framing intro the agent pastes verbatim to the human), `scaffold` (GET — returns the bundled tool defaults), `state_get` / `state_reset` / `ack` / `complete` / `force-finish`. The 7-substep ceremony walks framing-acknowledgement, tooling-hash, doc-complexity, compaction-practice, plan-hash, agent-md-hash, first-close-complete. Most agent activity here is during the first-onboard sequence; subsequent sessions touch this surface mainly via `/agentnotes` (the contract refresh) rather than the onboard endpoints directly.

**Auth-failure envelope on `/onboard-state*` endpoints** *(CONTRACT 2.14.2, an internal release):* a `401` response carries `code: "ONBOARD_AUTH_REQUIRED"` plus a stable `auth_diagnosis` enum (`auth_missing` / `auth_empty` / `token_invalid_or_revoked` / `token_expired` / `unknown`), an `agent_remedy` recovery path, and a `client_observed` block (`{ip, user_agent, auth_header_present, token_hint}`) showing what the *server* saw about the request — letting the agent self-diagnose without a `/me` round-trip. The matching `[ONBOARD-401]` server-log line carries the same `auth_diagnosis` + `token_hint`, so a human watching logs and an agent inspecting the envelope see the same failure through different surfaces. *Design principle worth noting:* error responses should be legible to **both** the agent reading the body **and** the human reading the log, naming the same diagnosis on both sides at the same point of failure — apply this when shaping your own error envelopes.

### The flow in narrative form

Here's what a typical agent session looks like as a sequence of API calls:

```
Session start:
  1. GET /api/v2/me                              → "yes you are Claude on project X"
  2. GET /api/v2/agentnotes/{slug}               → fetch the full contract (re-read on session start)
  3. GET /api/v2/projects/{slug}/tasks           → list of all tasks
  4. (filter for ones assigned to me)
  5. GET /api/v2/projects/{slug}/members         → resolve who the human reviewer is
  6. GET /api/v2/tasks/{task_id}/notes           → read the notes on the task I'm picking up

Picking up a task:
  7. PATCH /api/v2/tasks/{task_id}               → status: in_progress, transition_note: "starting on X"
  8. (server auto-posts the transition_note as a visible note)

While working (multiple times):
  9. GET /api/v2/tasks/{task_id}                 → re-read fresh state
 10. POST /api/v2/tasks/{task_id}/notes          → post a structured note about progress
 11. PATCH /api/v2/tasks/{task_id}               → maybe update phase, blocked_by, etc

When finished:
 12. POST /api/v2/tasks/{task_id}/notes          → post the completion note (Problem/Fix/Scope/Test)
 13. PATCH /api/v2/tasks/{task_id}               → status: needs_review, owner_label: "human:the maintainer"
                                                   transition_note: "ready for review, see test steps"
 14. (server validates the human owner, accepts)

Session end:
 15. PUT /api/v2/projects/{slug}/resume          → update the resume with current state
 16. (git commit, git push, output session summary)
```

That's the entire interaction model. ~16 calls for a single-task session. Most are GETs (read-heavy by design — fetch fresh, write deliberate). The pattern is **read → think → decide → write → read again to verify**.

Every write call gets attributed to the agent in `activity_events` with its name, token id, and (post an internal release) operator. Every change is auditable. Every failure is honest (4xx with a reason; no silent success).

### What's NOT in the API

- **No bulk operations.** No `PATCH /tasks` for updating many at once. One task at a time. Forces deliberate work.
- **No "create project."** Agents cannot create projects. Humans create projects via UI; agents discover them via `/agentnotes`.
- **No DELETE on notes.** Notes are immutable; supersede instead. Audit trail stays clean.
- **No "act as another user" endpoint.** No way for an agent to impersonate.
- **No webhooks (yet).** The mention trigger is a placeholder. Future notification pipeline.
- **No GraphQL or query language.** The API is small and opinionated. If you can't do it with the listed endpoints, you can't do it.

These omissions are deliberate. They keep the contract small enough to fit in an agent's working memory.

---

## 9. How the contract is enforced

The contract is not a request. It is a precondition for participation, enforced at the route level.

```
   Agent makes API call
            │
            ▼
   ┌────────────────────────────────────┐
   │  FastAPI route handler             │
   │  (e.g. patch_task)                 │
   └────────────────┬───────────────────┘
                    │
                    ▼
          ┌─────────────────────┐
          │  _resolve_actor     │  ← extracts Bearer token, hashes,
          │  (identity gate)    │     looks up agent. 401 if invalid.
          └─────────┬───────────┘     403 if cross-project.
                    │
                    ▼
          ┌─────────────────────┐
          │  _require_write     │  ← agent already validated above,
          │  (authorisation     │     human users get role-checked.
          │   gate)             │
          └─────────┬───────────┘
                    │
                    ▼
          ┌─────────────────────┐
          │  Lifecycle gates    │  ← agent → done? 422.
          │  (in route logic)   │     agent → cancelled? 422.
          │                     │     needs_review without human? 422.
          └─────────┬───────────┘
                    │
                    ▼
          ┌─────────────────────┐
          │  Identity overrides │  ← author_type forced to 'agent'
          │  (in note creation) │     author_name forced to agent.name
          │                     │     is_completion_note forced to false
          └─────────┬───────────┘
                    │
                    ▼
          ┌─────────────────────┐
          │  Database write     │  ← committed transaction
          └─────────┬───────────┘
                    │
                    ▼
          ┌─────────────────────┐
          │  Audit event insert │  ← actor_type, actor_user_id,
          │                     │     actor_token_id, operator_user_id
          │                     │     stamped at insert time
          └─────────┬───────────┘
                    │
                    ▼
          Response to agent (200, or 422 with detail)
```

Every contract rule maps to a specific gate in this chain. The doc describes the rules; the code enforces them. There is no parallel "trust the agent to follow the rules" layer — the rules are the routes.

This is the property that makes the contract trustworthy: **a malicious or buggy agent cannot follow a different contract by accident**. It can only do what the routes allow. If the routes say "you can't mark done," the agent literally cannot mark done — its PATCH gets 422'd with a clear message saying why.

### 9.1 The 422-recoverable envelope (every gate fires the same shape)

Every gated rejection (4xx response from a contract-enforcing route) carries a structured envelope rather than a bare `detail` string. This is a load-bearing design principle — the agent must have **everything it needs to recover from a single response**, with no need to ask the human, guess, or retry-blindly. The shape:

```json
{
  "code":          "BOARD_GATE_TRIGGERED" | "ONBOARD_AUTH_REQUIRED" | …,
  "detail":        "Plain-English explanation of what was rejected.",
  "gate_reason":   "contract_drift" | "drift_eval_required" | "missing_transition_note" | …,
  "agent_remedy":  "What the agent should do next, written for a fresh session.",
  "human_visible": true | false,
  "refresh_endpoint": "/agentnotes/{slug}",        // optional — when re-fetching is the remedy
  "response_header":  "X-Drift-Response",          // optional — when a header carries the answer
  "field_violated":   "transition_note"            // optional — when one specific field was wrong
}
```

The optional fields appear only when relevant. `human_visible: true` means the same envelope is surfaced on the human-facing UI (board card warnings, drift admin surface) so the human and agent see the same diagnosis through different lenses. The matching server-log line carries the same `code` + `gate_reason`, so a human watching logs and an agent inspecting the body see one failure named identically on both sides.

**Why this matters:** without the envelope contract, every new gate would invent its own shape and the agent would have to learn each one. With the envelope contract, an agent that has never seen a particular gate can still recover correctly — read `agent_remedy`, follow the named recovery (re-fetch / set header / fix field), retry. New gates ship cheaply because the recovery interface is already understood. The principle is enforced at design-review time: every new error path gets audited against this bar before it ships. *(Pinned design rule, R2.6 wave; the ONBOARD-401 envelope on `/onboard-state*` (§8) is one specific application of this general shape.)*

The connection between this architecture and [user-agent-model.md](user-agent-model.md) is direct: user-agent-model defines *who* the agent is (identity, scope, creator, disposability). This doc explains what the agent *can do* given that identity, and the contract content lists *every specific rule*. They're three layers of the same model.

---

## 10. How the contract evolves

The contract has a version field: `CONTRACT_VERSION` in `contract.py`, currently `2.14.3`. The version is included in every contract response so agents can detect changes.

Today, the agent re-reads the full contract on every session start. This is fine because it's only ~35KB and the cost is minimal. The drift gate (§9) additionally forces a fresh re-read on the first mutation per session — gating any agent whose cached contract has aged out of the refresh window.

**Version-bump rules** (emerged across the 2.x series, R2.5 → R2.7 + wave 2.0.x):

- **Patch bump (2.x.y → 2.x.y+1)** — new history block in `contract.py`, no JSON shape change OR small additive change to existing fields. Most ships are patch bumps.
- **Minor bump (2.x.0 → 2.(x+1).0)** — new endpoint, new field on an existing endpoint, new rule in `agent_enforcement` or `task_discipline`, restructured FRAMING_TEXT. Cumulative additive evolution.
- **Major bump (2.x.0 → 3.0.0)** — reserved for the Forgejo + Vaultwarden integration era. Not yet triggered.
- **`1.0` is the planned RC** — the cut-line marking "shipped to a buyer outside dogfood." Pre-1.0 the contract is in active evolution; post-1.0 changes ship under a stricter back-compat policy.

Contract changes happen when:

- New endpoints get added (then the `endpoints` section grows)
- Rules get added or tightened (then `agent_enforcement` or `task_discipline` grows)
- New design principles emerge from real use (then `design_principles` grows)
- The lifecycle model changes (this doc gets updated; the JSON catches up)
- Security tightens (rules about credentials, tokens, secrets get refined)

Contract changes do NOT happen for:

- Trivial wording fixes (typos, clarifications) — version stays
- Pure additions to documentation (this doc) — version stays
- Bugfixes in `contract.py` rendering — version stays

When the version goes from `2.14.3` to `2.15.0`, agents that had `2.14.3` cached should re-read. When it eventually goes from 2.x to `3.0.0`, agents should re-read AND the human should review the changes — major version bumps signal breaking rule changes that may require workflow updates.

---

## 11. Relationship to other architecture docs

```
   ┌──────────────────────────────────────────────────┐
   │  user-agent-model.md (v3)                        │
   │  Identity, disposability, creator = operator     │
   │  "Who am I, what can I do, who gets credit?"     │
   └──────────────┬───────────────────────────────────┘
                  │
                  │ identifies the agent
                  │
                  ▼
   ┌──────────────────────────────────────────────────┐
   │  AGENT-CONTRACT-ARCHITECTURE  (this doc)         │
   │  Onboarding, lifecycle, contract model           │
   │  "How does an agent participate? What's the      │
   │   shape of the deal we're making with it?"       │
   └──────────────┬───────────────────────────────────┘
                  │
                  │ governed by
                  │
                  ▼
   ┌──────────────────────────────────────────────────┐
   │  BOARD-PURPOSE-AND-PACT  (proposed, future)      │
   │  Why this whole thing exists                     │
   │  "What problem is the board solving, and what's  │
   │   the reciprocal pact between the parties?"      │
   └──────────────────────────────────────────────────┘
```

Three sibling docs. None replaces the others; each answers a different question.

- **user-agent-model.md** (v3) is the *who*. Identity, scope, disposability, 1:1 creator binding.
- **activity-model.md** is the *how much* / *when*. Period tracking, attribution, active-via-agent rendering.
- **AGENT-CONTRACT** (this doc) is the *what and how*. The shape of the contract, the lifecycle, the rules categories, the enforcement mechanism.
- **BOARD-PURPOSE-AND-PACT** (when built) is the *why*. The persistence-layer thesis, the failure modes the board protects against, the reciprocal commitments.

A new architect or agent should read in this order: **PURPOSE-AND-PACT** (when it exists) → **AGENT-CONTRACT** → **user-agent-model** → **activity-model** → drill into specific schema and threat docs as needed.

The three docs are **siblings, not parent-child**. Each can be read standalone but they're richer together. They cross-reference each other at section level so the reader can jump.

Beyond these three architectural docs, there's also:

- **`/agentnotes` JSON** — the live operational contract. Source of truth for what the rules are *right now*.
- **`AGENT-CONTRACT.html`** — auto-generated reference to the JSON, for humans who need to look up the literal contract content.
- **`CLAUDE.md` / `AGENTS.md`** — the per-agent local rules file, generated from a template in the contract.

This doc explains the model. The JSON is the model. The HTML is a snapshot. The CLAUDE.md is a working copy. Different surfaces, one underlying contract.

---

## 12. Why we made these choices

> *The rationale section. Read this if you want to understand WHY the contract is shaped this way, not just WHAT it says.*

### Why dynamic generation instead of a static file?

The contract embeds project-specific values (slug, prefix, base URL, agent name, current task counts, available reviewers). A static file would either be wrong for some agents or have to be templated at deploy time. Dynamic generation means every agent gets its own correctly-instantiated contract, fresh on every fetch.

### Why JSON as the primary format?

Agents are AI. They parse JSON better than HTML or markdown. A structured JSON contract is unambiguous — there's no rendering or parsing layer that can introduce drift between what the doc says and what the code enforces. The code generates the JSON; the JSON IS the contract.

### Why a separate hand-written architecture doc (this one)?

Because the JSON is machine-first and the auto-generated HTML is just a rendering of the JSON — neither tells you the *model*. They tell you the *content*. You can read every endpoint and every rule in the auto-HTML and still not understand why an agent can't move tasks to `done`, or why the bootstrap has 7 steps, or why notes are immutable. This doc fills that gap.

### Why split create-agent from issue-token (post an internal release)? — *v3 update: collapsed*

> **v3 update (2026-04-19):** The create/issue split was removed in v3. Under the current model, creation and token issuance are atomic — the creator IS the operator, and there is no delegation scenario that requires pre-provisioning for someone else. The split was a v2 feature that didn't fit the single-the maintainer dogfood scope. See [user-agent-model.md](user-agent-model.md) §4.

~~Because owners design bots, members operate them, and conflating creation with ownership made the model rigid. Splitting them lets a project owner provision a bot once and have multiple team members take turns operating it without ever requiring them to be project admins.~~ *(Removed in v3: users create their own agents directly.)*

### Why agents can't move tasks to `done`?

Because closure is a decision, not an execution. Marking something done is the human saying "I accept this work as complete." Agents do work; humans accept it. The 422 isn't a security gate — it's a forcing function that keeps humans in the loop on what counts as "shipped."

### Why do we use HTML in note bodies?

Because plain text doesn't render structure (Problem/Fix/Scope/Test sections, numbered test steps, @mentions, code blocks). Markdown would also work but HTML is more universally rendered by every UI we'd ever build. The body is sanitised server-side to a small whitelist (p, br, strong, em, b, i, ul, ol, li, span) to prevent XSS while keeping the structural cues that make notes scannable.

### Why is the contract physically enforced via routes, not advisory?

Because trust is fragile and enforcement is reliable. A purely advisory contract works until an agent has a bug, hallucinates, or gets prompt-injected. A route-enforced contract works regardless of the agent's intent. The doc describes the rules; the routes are the rules. They cannot drift.

### Why include design_principles in the contract at all?

Because they shape *how* the agent thinks, not just *what* it does. Without them an agent might follow every rule and still produce poor work. The principles exist so the agent knows what good looks like — DRY code, secure-by-default routes, atomic deploys, plain English summaries, document decisions. These aren't enforceable but they raise the floor.

### Why is the bootstrap 7 steps and not just "fetch and go"?

Because each step prevents a specific failure mode that real agents have hit. The token-via-file step came from an early session where a token was leaked in chat history. The verify-identity step came from sessions where agents worked for an hour with a bad token. The fetch-project-contract step came from sessions where agents used the public stub by mistake. Each step is a scar.

### Why is the available_projects list filtered for agents?

Because letting an agent enumerate every project on the board is an information leak. The agent only needs to see its own scoped project. The filter is a small but real privacy gate.

---

## 13. Where to look for current contract content

This doc explains the model. For the actual rules, endpoints, and rule wording **right now**, look at one of these:

| Surface | URL / path | Audience | When to use |
|---|---|---|---|
| **Live JSON** | `GET /agentnotes` (unauthenticated stub) | Agents | Routine bootstrap |
| **Live JSON (full)** | `GET /agentnotes` (authenticated) | Agents | Routine bootstrap with token |
| **Live JSON (project-scoped)** | `GET /agentnotes/{slug}` (authenticated) | Agents | After choosing a project |
| **Auto-HTML** | `0-MD/0-Documentation/public/AGENT-CONTRACT.html` | Humans | Look up exact rule wording |
| **Source** | `app/api/v2/contract.py` | Engineers | Edit the rules |

The auto-HTML is regenerated whenever `contract.py` changes (via the sync workflow). Don't edit it directly — edit the source. The HTML is a derived artefact, like a compiled binary. Treat it as read-only.

This doc is hand-written and updates separately, when the *model* changes. Updating a rule wording in `contract.py` does not require updating this doc. Adding a new lifecycle state, a new enforcement category, or a new artefact in the chain does require updating this doc.

---

## 14. Glossary

| Term | Meaning |
|---|---|
| **Agent** | An AI identity authenticated by a Bearer token, scoped to one project |
| **Contract** | The structured operating manual served at `/agentnotes`, defines what an agent can/must/cannot do |
| **Bootstrap** | The 7-step ritual an agent runs on first connection to a project |
| **Token** | Bcrypt-hashed credential. Whoever holds it can act as the agent. Owner of the token = effort attribution target |
| **Operator** | The human currently holding (and therefore owning) the agent's active token. Set on issue/cycle. |
| **CLAUDE.md / AGENTS.md** | The local rules file generated from a template in the contract, lives in the agent's working dir |
| **`/agentnotes`** | The live contract endpoint. Unauthenticated stub OR authenticated full contract |
| **`agents_md_template`** | Field in the contract response containing the per-agent CLAUDE.md content as a string |
| **Identity enforcement** | Category of rules preventing agents from impersonating humans or other agents |
| **Scope enforcement** | Category of rules preventing agents from reading/writing other projects |
| **Lifecycle enforcement** | Category of rules preventing agents from making decisions reserved for humans |
| **Quality enforcement** | Category of rules making the board self-documenting via structured notes and transition reasons |
| **Discipline rules** | Read-before-write rules that govern how an agent fetches state before mutating |
| **Sync expectations** | Rules about timing — when changes must be synced rather than batched |
| **Transition note** | The mandatory `transition_note` field on status changes that explains *why* the change happened |
| **Structured note** | A note formatted with Problem/Fix/Scope/Test sections in HTML |
| **Completion note** | A note flagged `is_completion_note=true`. Required before a human can close a task. Agents cannot create these. |
| **Supersede** | Mark a note as struck-through with a reason, instead of deleting. Audit trail preserved. |
| **`needs_review`** | Status that requires human reassignment. The handoff from agent → human. |
| **`agent_enforcement`** | The section of the contract listing what the API will reject and why |
| **CONTRACT_VERSION** | The version string in `contract.py`, currently `2.14.3`. Bumped when rules change (patch / minor / major rules in §10). |

---

## 15. Open questions

These are unsettled and need answers when we revisit the contract model.

1. ~~**Version field granularity.** We have `CONTRACT_VERSION = "2.3.0"` but no documented rule for when to bump major vs minor vs patch. Pending an internal release (pact versioning) will define this; may apply the same rule to the contract version.~~ **Resolved 2026-05-06 (an internal release):** Bump rules emerged across R2.5 → R2.7 + wave 2.0.x and are now documented in §10. Patch = additive history block / no shape change; minor = new endpoint or rule; major = reserved for Forgejo+VW integration era. `1.0` is the planned RC cut-line.

2. **MCP integration.** When MCP-mediated agents connect, do they use the same contract endpoint? Do they get the same content? Are they bound by the same rules? Currently undefined; depends on the MCP decision (an internal release/118/119).

3. **Agent-to-agent interaction.** Two agents on the same project — are they peers? Can one supersede another's notes (currently no — only own notes)? Can they hand off work to each other? Today the model assumes single-agent-per-project; multi-agent is undefined.

4. **External references in notes.** Should the contract enforce something about links to external docs, code review platforms, design tools? Currently no — notes can contain anything HTML-sanitiser allows. Worth thinking about.

5. **Contract surface for non-bot integrations.** What if someone writes a script (not an AI) that hits `/agentnotes` and wants to integrate? The contract assumes the consumer is an LLM agent. A script-friendly subset might be useful.

6. **Internationalisation.** The contract is English-only. The rules apply universally but the rule text is English. If we ever need a non-English deployment, the rendering layer needs i18n.

7. **The relationship between `agents_md_template` and this doc.** The template embeds a tiny version of the rules; this doc explains the model. There's no enforcement that they stay consistent. A future task could be to generate both from a single rule registry.

---

## 15.5 Documentation tagging — audience, IP, style (added 2026-04-08)

> This section was added during the documentation classification round (see `proposed/DOC-CLASSIFICATION-PROPOSAL.md`). It defines the three classification axes every doc carries in frontmatter, and the rules an agent follows when authoring or modifying documentation.

### Three axes, one frontmatter

When you (the agent) author or substantively modify a doc, you MUST set three frontmatter fields:

```yaml
---
audience: public          # public | internal | confidential | rescue | archive
ip: none                  # none | commercial | novel | derived
style: technical          # technical | practical | friendly | mixed
---
```

**Over-tagging is recoverable. Under-tagging is permanent loss.** When in doubt:
- Audience: pick `internal` (the safe middle)
- IP: pick `novel` and ask the human to confirm in your next note
- Style: match the doc's surrounding tier — technical for architecture, practical for operations, friendly for guides

### Audience definitions

| Tag | Meaning |
|---|---|
| **public** | Anyone can read. Safe for the website, README, marketing artefacts. |
| **internal** | All logged-in users on this install (humans + agents with valid tokens). Not for external eyes. |
| **confidential** | SA-only. Schema, threat models, recovery procedures, security analysis. |
| **rescue** | Operational documentation an SA needs *when things break*. Bootstrap and recovery scripts, break-glass steps. Read in emergencies, not for design reading. |
| **archive** | Historical, deliberately preserved, not maintained. Don't read as current truth. |

### IP definitions

| Tag | Meaning |
|---|---|
| **none** | Routine operational content, no IP value. Default for most docs. |
| **commercial** | Business-sensitive (competitive positioning, market analysis). Disclosure has business cost. |
| **novel** | Describes paradigms, patterns, or techniques believed original to this project. **Must include `ip_first_dated`, `ip_authors`, `ip_disclosure_path`, and `ip_summary`.** |
| **derived** | Builds on someone else's work in a way that requires attribution. Tracks the upstream source. |

When you tag `ip: novel`, you MUST also write `ip_summary` — one paragraph in plain English describing what the invention IS, in a form a non-engineer (lawyer, investor) could read. **Tagging without writing the summary defeats the purpose.**

### Style definitions

| Tag | Meaning | Default for |
|---|---|---|
| **technical** | Developer / architect voice. Assumes API / token / protocol vocabulary. | Architecture docs |
| **practical** | Technically-comfortable non-engineer voice. Walks through actions, less assumed knowledge. | Operations docs |
| **friendly** | Non-technical voice. Plain language, no jargon without immediate definition, recipe structure. | **Guides** |
| **mixed** | Multi-audience. Sections marked for different reader levels. | Cross-cutting docs |

**You match style to the doc you're writing, not to your own preferences.** A guide written in technical voice has failed even if every fact is correct.

### Guides — the proposal protocol

Guides are user-facing how-to content that lives in `0-Documentation/guides/`. They have a different shape and voice than surface docs.

**You do NOT invent guides on your own.** When authoring or updating a project's first doc contract, after the surface list is drafted and approved, you MUST propose a starter set of guides based on what you see in the project:

- **Trivial projects** (single-purpose tools, demos, copy-paste apps): propose 0–2 guides. Sometimes the right number is zero — say so honestly.
- **Non-trivial projects** (anything with users, data, workflows, external integrations): propose 2–4 guides drawn from the project's actual surfaces.
- **Frame the proposal as suggestions, not requirements.** The human decides which (if any) to write.
- **Record the human's accepted list under `guides_wanted`** in the doc contract. The list can be empty.

Future sessions: when authoring or updating any doc, check whether a related guide exists in `guides_wanted`. If yes and the underlying surface doc has changed, *suggest* a refresh in your next note. **Never refresh a guide without the human's nod.**

The guide rule, in one sentence: **derive from public docs OR from explicit human instruction during the authoring conversation, never invent unprompted.**

### Why this exists

Without machine-readable classification:
- The TOC has no way to group docs by audience tier
- The library bundle has no way to filter by sensitivity
- The IP register has no way to find inventions
- The agent has no canonical vocabulary when authoring new docs
- Future-us cannot answer "what's the IP here?" without an archaeological dig
- The first vibe coder to read our docs will bounce because everything is written in architect voice

The classification system is the cheapest possible answer to all six.

See `proposed/DOC-CLASSIFICATION-PROPOSAL.md` for the full rationale.

---

## 16. When this doc changes

This doc is updated when the **agent contract model** changes. Specifically:

- A new lifecycle state is added (e.g. "active, has token, sandboxed for first hour")
- A new enforcement category emerges (e.g. "behavioural enforcement" beyond the four current categories)
- A new artefact is added to the contract chain (e.g. an MCP-served variant)
- The bootstrap flow changes structurally
- The relationship between this doc and user-agent-model or PURPOSE-AND-PACT changes
- A category of rules gets added, removed, or significantly restructured

This doc is **NOT** updated when:

- A rule wording is tweaked in `contract.py`
- A new endpoint is added to an existing category
- A design principle is added to an existing group
- The CONTRACT_VERSION bumps for normal reasons
- A typo gets fixed

The discipline is the same as the architecture-doc-before-code rule from CLAUDE.md: **architectural changes update this doc first, mechanical changes update the source first.**

---

*Last updated: 2026-04-19 (cross-ref updates for v3 landing). Sibling to [user-agent-model.md](user-agent-model.md) (v3, supersedes former auth-agent.md) and [activity-model.md](activity-model.md). Future sibling to BOARD-PURPOSE-AND-PACT.md (currently a proposal in `proposed/`). The live contract content is at `/agentnotes` and rendered to `0-MD/AGENT-CONTRACT.html`. The source is `app/api/v2/contract.py`.*
