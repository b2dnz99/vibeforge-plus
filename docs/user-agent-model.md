---
title: VibeForge+ User/Agent Model (v3) — Disposable Agents, Per-User Lineage
audience: public
status: 0.7.1-PRE-RC
version: 1.1.0
supersedes: 0-MD/archive/auth-agent-v2.md
last_updated: 2026-05-09
authors: Parvez Khan + Claude (AI co-author)
ip: novel
style: technical
ip_first_dated: 2026-04-17
ip_authors: [pkhan, claude]
ip_disclosure_path: published
ip_summary: Per-user disposable-agent model. Each agent is 1:1 to (user, project) and never transfers. Users can have many agents concurrently (multi-model, multi-role) and across time (short-lived, killed by context rot). Drops v2's delegation/handover mechanism as overfit for a single-the maintainer dogfood scope. Captures the VS Code sandbox reality that cross-session token reuse cannot be enforced, so the model nudges toward clean per-agent tokens via disposability plus cheap creation rather than policing. UI implications include a Shape-B admin tree (user → project → agent cards) and a 7-day hide window for retired agents in the user-facing view.
---

# User/Agent Model — v3

> **Status:** Active as of 2026-04-19. Supersedes v2 (archived at [`0-MD/archive/auth-agent-v2.md`](../../archive/auth-agent-v2.md)). Internal companion: [user-agent-model-internal.md](../internal/user-agent-model-internal.md). Cross-cutting sections previously co-housed in v2 have been spun off to [activity-model.md](activity-model.md) (period tracking and attribution) and [horizon-principle.md](horizon-principle.md) (board as system of record).
>
> **See also:** [identity-roles.md](identity-roles.md) — the human identity tier (SA / SU / User / Viewer), separate from the agent model covered here.

---

## TL;DR — three shifts from v2

1. **Agents are disposable.** Many per user per project concurrently (e.g. Claude-backend + Grok-frontend + Codex-auth on the same project). Context rot kills them; users mint fresh ones. No attempt to keep an agent alive forever.
2. **1:1 agent → (user, project), never transfers.** v2's token-handover / cycle-to-become-operator mechanic is dropped. `created_by = token_owner` always. Token cycling issues a fresh token for the *same* agent; no operator change possible.
3. **Any project member can create their own agents.** Creation is not an admin action — it's a developer's dev-environment tool, each user's to provision as needed on projects they have access to.

Plus two UX primitives:

- **7-day hide window** for soft-deleted agents in the user-facing view. Admin view keeps every agent ever, forever.
- **Shape-B tree** in admin: user → project → agent-cards. Project is a visible branch node so the 1:1 relationship reads naturally, and cross-project drill-down is one click.

---

## Why this doc exists

v2 (`auth-agent.md`, 2026-04-07) introduced a **delegation model** — `token_owner_id` separate from `created_by`, any project member can cycle to become operator, a SA can pre-provision agents on behalf of team members. That model fits a *team-of-humans-sharing-a-bot* scenario.

**It does not fit how VibeForge+ is actually used.** VibeForge+ is a persistence layer for vibe coding — the differentiator is that the *board* is the shared persistent memory, and the agents that operate above it are ephemeral. A single the maintainer (or a small team) runs multiple models (Claude, Grok, Codex) for different task types, spins agents up per task, lets them die from context rot, and starts new ones. Handing a bot across humans is not something that happens in this workflow.

**v2 overshoots. v3 is v2 minus the overreach, plus an explicit disposability principle and a user-centric creation model.** v1's original primitive — *"one agent = one project = one creator = one identity"* — was the right shape; v2 added complexity we don't benefit from.

This doc captures the intent **BEFORE** any code or UI change, per CLAUDE.md's Documentation Discipline (*"architecture changes update the doc BEFORE the code"*).

---

## 1. Model primitives

### 1.1 Agent identity

An agent has **one user** and **one project**, both fixed at creation, both immutable for the agent's entire lifetime.

| Field | Meaning |
|---|---|
| `created_by` | The user who provisioned the agent. In v3, this is also the token owner and the operator — all three collapse to one field. |
| `project_id` | The project the agent is scoped to. Fixed at creation. Cannot change. |

The `token_owner_id` field introduced in v2 becomes redundant in v3 — it's always equal to `created_by`. A future migration can collapse the two; meanwhile the column stays and is kept in sync.

### 1.2 Agents are disposable

An agent is expected to have a **bounded useful lifetime**, typically one task, one work session, or until context rot renders it less useful. When an agent's useful life ends, it dies. The common death modes:

- **Context rot** — the most frequent cause. The model's context fills up, the user starts a new chat session, may or may not choose to reuse the token. This event is invisible to the board; we only observe the downstream "no more API calls from this token."
- **Explicit revoke** — user clicks revoke in the UI when they're done with an agent.
- **Creator-user removed from project** — cascade revoke (see §2.1).
- **Creator-user soft-deleted** — cascade revoke (see §2.2).

Agents are **not** designed to be long-lived, shared, or handed off. **Creating a new one is cheap and encouraged.** This is a design principle, not an accident.

### 1.3 The 1:1 is agent-rooted, not user-rooted

| Direction | Constraint |
|---|---|
| Agent → user | **1:1 forever.** Each agent has exactly one user, fixed at creation. |
| Agent → project | **1:1 forever.** Each agent has exactly one project, fixed at creation. |
| User → agents | **1:many (concurrent + historical).** A user can have many agents active at once (different models for different task types) and a long historical trail of retired ones. |
| Project → agents | **1:many (concurrent + historical).** Same, aggregated across all the project's users. |

The 1:1 is **agent-side.** It's what keeps effort attribution coherent. It is **not** a "one bot per user" constraint — a single user routinely has several agents active simultaneously.

**Scenario (worked):** the maintainer is building a small SaaS. He has three concurrent tasks on one project:
- Backend API — Claude (excels at architecture + Python)
- Frontend UI — Grok (faster iteration for this codebase)
- Auth layer — Codex (per-model prompt library that works well for this)

Three agents, three tokens, three VS Code windows, one project, one user. Each agent is 1:1 with the maintainer and with the project. the maintainer is many:many with nothing — he's just the user who owns all three.

### 1.4 Token scope is enforced at agent-scope

Every agent token is tied to a single agent, which is tied to a single project. **The user can, in practice, paste the same token into multiple VS Code windows — we cannot stop that**, because VS Code's chat environment is sandboxed from the board.

What we *can* do is make **clean-per-agent-token use cheap**: trivial to provision new agents per task, no friction, no admin approval, disposable. The model nudges toward "one agent per VS Code session / per task type" by making that the path of least resistance.

**Cross-project isolation is the load-bearing reason for per-project tokens**, not authentication security. If a user has Project A and Project B open in separate VS Code windows, per-project tokens prevent Project A's Claude from accidentally posting to Project B's board. The scope constraint is about blast-radius containment across a user's own stack, not about authenticating who the token belongs to.

### 1.5 We don't enforce what we can't enforce

Corollary of the above. VibeForge+ **does not try to:**

- Detect token reuse across VS Code sessions
- Prevent a user from sharing a token with their own other processes
- Force one-agent-per-user-per-project

It **does:**

- Make the hygienic pattern (per-task agent) trivial and default
- Track activity per agent, so if a user reuses tokens, the data is still captured against the agent identity they chose to reuse

This is the meta-layer design principle *"no security theatre"* applied to agent lifecycle: real mitigations for real threats (per-project scope isolation), no ceremony around threats we can't actually close (cross-session token reuse in a sandboxed IDE).

---

## 2. User lifecycle cascades

### 2.1 User removed from a project

**Effect:** All agents created by this user on this project → revoked (immediate, clean break).

**Rationale:** An agent's project access derives from the user's project access. If the user loses standing, the agent's continuation is meaningless — its work can't attribute to a user who no longer has a role in the project.

**Preserved:** All `activity_events` rows for those agents stay intact (immutable audit). The dual FK + snapshot pattern in `activity.py:8` keeps attribution legible.

**Restoration path:** Re-adding the user to the project does **not** auto-restore their revoked agents. If the user wants to work again, they create fresh agents. This is cheap under the disposability principle.

### 2.2 User soft-deleted

**Effect:** All tokens owned by this user (across all projects they were ever on) → zeroed out. Their agent rows → `status='revoked'`.

**Rationale:** The user no longer exists as an active identity. Their delegates go with them.

**Preserved:** Agent rows, activity trail, all identity UUIDs remain. Attribution survives via the dual preservation pattern already in place (`actor_user_id` no-FK reference + `details.actor` snapshot).

**Restoration path:** User-restore does **not** auto-restore tokens or un-revoke agents. Per v2's current code in `admin.py:748-805` — this part stays correct. User creates fresh agents post-restore.

### 2.3 Project archived

**Effect:** All active agents on the project → revoked.

**Rationale (decision locked 2026-04-19):** Matches the disposability primitive. In practice, project archive is rarely "just pausing" — usually the work has concluded or moved on, and the old agents' context is stale anyway. If an archive-then-reopen does happen, creating fresh agents fits the workflow better than resurrecting stale ones.

**Reopen:** Does not auto-restore agents. Users create fresh if work resumes.

---

## 3. Effort / time tracking posture

**Capture at the finest primitive available today. Defer aggregation.**

The existing `agent_activity_log` table (per v2's `auth-agent-internal.md` §3) captures period-start / period-end / duration / operator per agent. That primitive is correct and stays.

The **multi-agent-per-user concurrent case** (three agents working simultaneously on one project, per §1.3's worked scenario) does not yet have a settled aggregation answer:

- Sum durations across concurrent agents → inflates, double-counts overlapping time
- Union periods across agents → honest "wall clock" for the user, less intuitive per-agent
- Per-agent display with optional user rollup using "pick primary" or similar → yet another UX design decision

**v3 defers this entirely.** Raw primitives are captured today; the display/aggregation question waits until real dogfood data exists to reason against. Cheap to discard unused granularity; expensive to not have it when the aggregation is finally specified.

---

## 4. Permission model — changes from v2

| Action | v2 | v3 (v1.1) |
|---|---|---|
| Create agent on project | Project owner, admin, SU, SA | **Any project member with `write` role** (includes the above, plus any regular member) |
| Issue token at creation | Same — plus a separate "issue-token" action for post-hoc pre-provisioning | **Collapsed** — creation IS issuing (no split, since handover is gone) |
| Cycle token | Any `write+` member, *becomes operator* | Creator (self only) cycles own — operator does **not** change |
| Revoke own token | Self-only | Same |
| Revoke agent | Owner, admin, SU, SA | **Creator (self) + SU/SA.** PO and project-admin members are **not** authorised (v1.1 tighten). |
| Restore revoked agent | Owner, admin, SU, SA | **SU/SA only.** Restoration is a sysadmin recovery path. |
| Cascade-revoke on member removal | — | **New (v1.1).** Removing a `ProjectMember` row auto-revokes that user's active agents scoped to that project. Audit: `agent_revoked` with `reason=member_removed_cascade`. |
| Cascade-revoke on user soft-delete | SA only (unchanged from v2) | SA only — cascades across **all** projects the user owned agents on. |
| Take over someone else's agent | Cycle = become operator (v2 feature) | **Not possible** (removed by design) |

**The "create vs issue" split collapses.** In v2 the split existed so a SU could pre-provision an agent for a team member (SU creates, member issues-and-becomes-operator). In v3 each user creates their own — there is nothing to pre-provision.

### 4.1 Sysadmin vs project roles — the v1.1 distinction

v1.0 (signed off 2026-04-19) inherited v2's "admin member can manage any agent on the project" path via `ProjectMember.role == 'admin'`, and also allowed the project owner (`Project.owner_id`) the same authority. v1.1 **removes both**. The reasoning (surfaced in an internal release during an internal release design review on 2026-04-21):

- **PO is a project role, not a sysadmin role.** The project owner decides *who has access to the project*. They do that by adding or removing human members — not by reaching into other users' tooling.
- **Admin-member is a collab role.** `ProjectMember.role='admin'` means "can edit everything on the board" — it does not mean "can cycle another user's API token."
- **Only SU/SA are sysadmin tiers.** When cross-user agent manipulation is legitimately needed (ex-member cleanup, stolen-token revoke, break-glass), the SU or SA does it — that is exactly the identity tier's job, and it is audited.

**Enforcement flow on member removal (cascade).** When PO or SU removes a user from a project, every active agent that user created on that project is auto-revoked in the same transaction, with a dedicated audit event per agent (`reason=member_removed_cascade`, `actor=the-remover`). No orphan tokens, no PO-reaching-into-PU's-toolbox. The PO controls *who has access*; the system handles *what their tools look like* as a consequence.

*Trust-the-user = trust-their-agent.* If a project trusts the user enough to let them in (as a `write+` member), it trusts them to mint and manage their own agents. If that trust ends (member removed), the tools end as a systemic consequence — not as a per-agent human action by PO.

---

## 5. UI design

### 5.1 Admin view — Shape B tree

User → Project → Agents as lite cards. Project is an intermediate branch node.

```
the maintainer (SU)
├─ vibeforge-plus
│   ├─ [ Claude · backend · active · last seen 2m ago  ]
│   ├─ [ Grok   · frontend · active · last seen 8m ago ]
│   └─ ▸ Archive (3)   ← collapsed retired agents
└─ pc-parts-demo
    ├─ [ Codex  · auth · active · last seen 4h ago     ]
    └─ ▸ Archive (1)
```

**Primary card row (always visible):**

```
[ name · role/label · status · last seen ]
```

**Expanded card (on click):**

- Model type + self-reported version (e.g. `claude · claude-opus-4-7`)
- Token prefix (e.g. `vf_a3b2…`)
- Created at + created by (redundant in this tree — tree root is always the creator — but surfaced for audit consistency across views)
- Revoked at + revoked by (if applicable)
- Total active-seconds from `agent_activity_log` (denormalised counter on `agents.total_active_seconds`)
- Last task touched (if recently available from ActivityEvent lookup)

**Rationale for Shape B:** Users can accumulate many agents (multi-model × multi-project × across time). Project-as-branch lets admin collapse/expand to answer questions like *"what's the maintainer running on vibeforge-plus right now?"* without scrolling past unrelated projects. The Archive sub-branch per project keeps the active tree clean without losing history.

### 5.2 User config view — Shape A

The user sees **only their own agents**, flat list with project-on-card, scoped to active + recently-retired (≤ 7 days).

```
My agents (3 active, 1 recently retired)

[ Claude · backend · vibeforge-plus · active · 2m ago  ]
[ Grok   · frontend · vibeforge-plus · active · 8m ago ]
[ Codex  · auth · pc-parts-demo · active · 4h ago      ]
─────────────────────────────────────────────────────────
Recently retired (past 7 days):
[ Claude · baseline · pc-parts-demo · revoked · 3d ago ]
```

**The 7-day hide window:** soft-deleted / revoked agents disappear from the user's view after 7 days. They remain visible forever in the admin tree (for audit) and their activity-event rows remain intact.

**Rationale:** the user view is *"my working toolset,"* not *"my archaeological record."* The audit is preserved (admin tree + immutable activity log) but the user doesn't need to scroll past 47 dead agents to find the three they care about.

**No restore-from-user-view.** If a user wants to bring back a >7-day-retired agent, they ask admin — and admin will usually say *"create a new one, that's the designed path."*

### 5.3 Card anatomy — common field sources

| Field | Source | Refresh behaviour |
|---|---|---|
| `name` | `agents.name` | Immutable after creation |
| `role/label` | `agents.description` (short form) or agent-type classification | Immutable after creation |
| `project` | `agents.project_id` → `projects.slug` / `.name` | Immutable |
| `status` | `agents.status` (`active` / `revoked`) | Bumped on lifecycle actions |
| `last seen` | `agents.last_seen_at` (API pulse, an internal release) | Bumped by `_resolve_actor` on every authenticated Bearer call |
| `active-seconds-total` | `agents.total_active_seconds` (denormalised counter, an internal release) | Bumped on period close in `agent_activity_log` |

**Nuance on `last seen`:** it's a **token-pulse**, not a work-indicator. An agent sitting idle in VS Code with context rot but not calling the API will show "not seen recently" even if the user considers it "still my agent." That's by design — stale token = stale card. For richer "active today" display, use `agent_activity_log` aggregation (future, per §3).

---

## 6. Schema implications

**v3 does not require an immediate migration.** The existing schema supports the model. What v3 enables is **simplification over time:**

1. **`agents.token_owner_id` → deprecated.** Always equal to `created_by` in v3. Can be dropped in a future migration; until then it stays in sync.
2. **`project_members.agent_id` role-override layer → redundant under v3 semantics.** Agent access derives from the user's project membership, not from the agent's own ProjectMember row. The an internal release auto-synthesis logic in `members.py:208-229` stays correct (it's what drives Shape B's tree view — agents surface via `agents.project_id`). The "role override" usage of ProjectMember.agent_id is a v2 feature for delegated agents that v3 does not use. Flag for UI removal now; column deprecation later.
3. **Activity schemas unchanged.** `activity_events` and `agent_activity_log` work as-is.

These are cleanup candidates, not blockers. **The v3 UI can ship against the current schema** without any migration; schema simplification is a follow-up.

---

## 7. What v3 does NOT change from v2

- **Scope fixed at creation.** Agent's project never changes after creation.
- **Token format + storage.** `vf_` + 20 bytes hex entropy, sha256 hash stored at rest, token_prefix for display. Plaintext shown exactly once at creation.
- **Dual preservation in `activity_events`.** FK (`actor_user_id`, no constraint → survives user soft-delete) + snapshot (`details.actor` display name at insert time). This is the attribution-survives-deletion pattern and it is correct.
- **Soft-delete semantics for users.** `status='deleted'` + `deleted_at` + `deleted_by`, no hard delete ever. Restoration remains allowed.
- **Bearer auth at API layer.** No change.
- **an internal release heartbeat primitive (`last_seen_at`).** Kept as-is.
- **an internal release activity log primitives (`agent_activity_log`, `user_activity_log`, denormalised counters).** Kept — they are the raw data that future aggregation will read from.
- **Immutable audit trail discipline.** `activity_events` is append-only.

---

## 8. Decisions (locked 2026-04-19)

All five open questions from the proposal round resolved as proposed defaults:

1. **Project archive → revoke agents.** Matches disposability. Reopen creates fresh agents. See §2.3.
2. **Token cycling kept as "same agent, new token."** Operator does not change (only the user who owns the agent can cycle, or admins on their behalf). Preserves activity-rollup coherence under one agent identity.
3. **`project_members.agent_id` role-override usage removed from UI.** Column remains in schema (future migration may drop it); the role-override layer is not used by v3 semantics.
4. **Creator-self revoke of own agent allowed.** Users control their own tools. Restoration of a self-revoked agent remains admin-only (for consistency with "restore is admin recovery," not a user action).
5. **Multi-agent effort aggregation display — deferred.** Raw per-agent primitives captured today (no change needed). Display decision waits until real dogfood data exists. Not a blocker for v3 UI work.

---

## 9. Relationship to sibling docs

**Public surface:**
- **[activity-model.md](activity-model.md)** — period tracking, attribution, active-via-agent rendering. Spun off from v2's co-housed activity sections.
- **[horizon-principle.md](horizon-principle.md)** — forward-looking thesis about board-as-system-of-record. Spun off from v2.
- **[agent-contract.md](agent-contract.md)** — what the agent agrees to (contract, bootstrap, rule categories, enforcement).

**Confidential (SA-only) surface:**
- **[user-agent-model-internal.md](../internal/user-agent-model-internal.md)** — schema, lifecycle code paths, cascade implementations, migration notes. The v3 companion to this doc.
- **[activity-capture.md](../../archive/activity-capture.md)** — the 2-minute rule in code, period state machine, denormalised counters, anti-idle-tab heartbeat.
- **[threat-model.md](../internal/threat-model.md)** — threat inventory (human attacker, agent attacker, internal, network).
- **[recovery-procedures.md](../internal/recovery-procedures.md)** — SA password reset, lockout, compromised token, activity log corruption, DB loss.

**Archived:**
- **[`auth-agent-v2.md`](../../archive/auth-agent-v2.md)** (archived 2026-04-19) — the v2 doc this supersedes.
- **[`auth-agent-internal-v2.md`](../../archive/auth-agent-internal-v2.md)** (archived 2026-04-19) — v2's internal companion.
- **[`AUTH-ARCHITECTURE-v1.md`](../../archive/AUTH-ARCHITECTURE-v1.md)** (archived 2026-04-05) — v1 historical context. v3 reclaims v1's core primitive *"one agent = one project = one creator = one identity"* while retaining v2's innovations (dual-preservation pattern, activity log primitives, denormalised counters).

---

## 10. Implementation phases (preview, not spec)

**This doc specifies intent, not implementation.** The UI refactor plan sits on top of this doc and will be drafted separately.

- **Phase 0 — Doc sign-off.** ✅ Complete (2026-04-19). v3 approved, v2 archived, spin-offs created, cross-refs updated, TOC + library rebuilt.
- **Phase 1 — Permission relaxation.** Any project member can create agents (API gate + UI allow). Smallest commit-shape change. **First to DEV, per SDLC-Lite discipline.**
- **Phase 2 — UI refactor.** Shape B admin tree + Shape A user config + the member-management UI (user↔project assignment, project member lists, role edits).
- **Phase 3 — Disposability UX polish.** 7-day hide window on user view, revoke/restore flows in admin tree, "create new" as the default action path in user config.
- **Phase 4 — Schema cleanup (non-urgent).** Deprecate `token_owner_id` column; remove `project_members.agent_id` role-override usage. Additive; no rollback risk.

Each phase is independently shippable. No big-bang migration required, no blocking sequence beyond Phase 0. All code deployments follow SDLC-Lite: **DEV first**, then UAT, then PROD.

---

## Sign-off

- [x] **the maintainer (the maintainer)** — approved 2026-04-19, defaults ticked
- [x] **Architect (Claude)** — code/schema implications verified against current repo state (v2 schema compatible; no migration required to ship v3 UI)
- [x] **Post-approval transition executed 2026-04-19:**
 - [x] This doc promoted to `0-Documentation/public/user-agent-model.md`
 - [x] v2 (`auth-agent.md`) archived at `0-MD/archive/auth-agent-v2.md` with SUPERSEDED banner
 - [x] v2 internal companion archived at `0-MD/archive/auth-agent-internal-v2.md` with SUPERSEDED banner
 - [x] Spin-off docs created: `activity-model.md`, `horizon-principle.md`, `activity-capture.md`, `recovery-procedures.md`, `threat-model.md`
 - [x] v3 internal companion created: `user-agent-model-internal.md`
 - [x] Cross-refs updated in: `agent-contract.md`, `documentation-architecture.md`, `tech-index.md`, `product-brief.md` (Stage 1)
 - [ ] Stage 2 cross-refs (README, proposals, toolkit) — opportunistic as each doc is next touched

---

*Captured 2026-04-17 from a planning conversation between the maintainer and Claude about UI-gap triage that surfaced deeper intent about the user/agent model. Approved and landed 2026-04-19. Per CLAUDE.md discipline: **doc before code.** Code implementation phases sit on top of this doc and deploy DEV-first per SDLC-Lite.*
