---
title: "What VibeForge+ is"
audience: public
status: 0.7.0-PRE-RC
version: 1.1.0
last_updated: 2026-05-09
authors: Parvez Khan + Claude (AI co-author)
---

# What VibeForge+ is

A self-managed task board for AI-paired programming. One operator (the human at the keyboard), one or more AI agents (Claude, Codex, Cursor, GPT — agent-agnostic), and a board that holds the intent both parties keep losing track of when context drifts.

Runs on your own host. Has no telemetry. Stores no credentials for third-party services. GPLv3.

## Honest framing

This is a **personal project**. The author built it for their own AI-paired-programming workflow and chose to release it under GPLv3 in case it's useful to anyone else. It is **fully vibecoded** — written with AI agents from start to finish — and is at **0.7.0-PRE-RC**, meaning the mechanics work end-to-end on a fresh Ubuntu 24 install but a formal threat-model review has not been done.

If you're looking for production-grade enterprise software with an SLA, this isn't that. If you're looking for a small, opinionated task board built around the assumption that you and your AI agent are working as a pair, it might suit you.

The orange `0.7.0-PRE-RC` label under the top-left logo on every page is the visual reminder.

## What it does

- A **task board** with projects, phases, tasks, notes, and audit-trailed status changes
- An **HTTP API** any AI agent can talk to with a Bearer token — no editor plugins, no SDK, no vendor lock-in
- An **agent contract** the system serves at `/agentnotes` — the rules, the endpoints, the recovery patterns. The agent reads it on first contact; the system enforces it
- A **drift gate** that periodically forces the agent to re-read its contract — closes the "agent's rules went stale four hours ago" gap on long sessions
- A **two-tier admin model** that keeps daily-use operators away from system-config controls (Super User vs Super Admin)

## What's in the bundle

```
        ┌───────────────────────────────────────────────────┐
        │  app    Postgres   Caddy    health-check          │
        │  ↑                                                │
        │  │  the actual board API + UI                     │
        │  │                                                │
        │  forgejo                  vaultwarden             │
        │  (bundled, not wired)     (bundled, not wired)    │
        └───────────────────────────────────────────────────┘
```

Six containers come up on `docker compose up`. Four are wired and used by the install:

- **app** — the FastAPI service serving the board UI + API
- **db** — Postgres
- **caddy** — TLS termination, self-signed by default
- **health** — separate container that polls the others and serves the System Health dashboard

Two are bundled but **not currently wired into the board flow** in this release:

- **forgejo** — a git server. Present in the compose file because future board-hosted-git work will use it; nothing in the current board talks to it. Safe to leave running, safe to disable in compose if you don't want it.
- **vaultwarden** — a credential vault (community Bitwarden fork). Same story — present for future use, not consumed by anything in this release.

If you want a leaner stack, comment out the `forgejo` and `vaultwarden` services in `docker-compose.yml` before `docker compose up`. The board works without them.

## What it tries to address

When you spend hours AI-paired-programming, two things drift apart:

1. **The code** — moving forward in git, accumulating decisions in the diff, with the human and agent both heads-down in the editor
2. **The board** — the intent record. Why this task. What was decided. What's blocked. What's next. Sitting silent because nobody stopped to update it

That gap is invisible while you're inside it and very expensive when you come back to it next session, or when a new agent picks up the work cold. The agent can't see what's in the board if you don't put it there. The agent can't refresh its understanding if the system doesn't remind it.

VibeForge+ is one attempt at the persistence-layer half of that problem — keep the board in sync via the agent itself, and have the system enforce periodic re-grounding so the agent's rules don't decay silently. Whether the approach generalises, or works for anyone other than the author, is genuinely unknown.

## What it is NOT

- **Not a project-management SaaS.** No multi-tenant, no analytics dashboards, no Kanban marketplace.
- **Not a Jira clone.** No story points, no estimates, no sprint planning rituals. The unit of work is "task" — title + description + status + notes + relationships. That's the whole model.
- **Not a CI/CD platform.** Tracks intent and progress; doesn't run builds, deploy code, or gate merges.
- **Not a customer support system.** Audience is small teams and solo operators, not external users.
- **Not a compliance product.** Audit log is local-only; no off-host shipping; no regulatory framework attestations.

## Pre-RC status — what works, what doesn't

**Works:**

- The board mechanics, agent contract, drift gate
- Install runs end-to-end on a fresh Ubuntu 24 VM in 2-5 minutes
- Caddy self-signed mode (TLS just works without a public DNS name)
- Two-tier admin model (User / SU / SA), session lifecycle, agent token CRUD
- Audit log, activity feed, project/task/note/relationship CRUD via API + UI

**Known limitations in this release:**

- **No formal threat-model review.** Not for public-internet exposure without further hardening.
- **No first-boot UI wizard.** Install is via a bash script that prompts for credentials inline.
- **Forgejo + Vaultwarden bundled but not wired** (see above).
- **No multi-host / cluster mode.** Single host only.
- **No off-host backup automation.** Use `pg_dump` from the host shell + your own backup pipeline.
- **No SSO across the bundled services.** Each service has its own login flow if you choose to wire it.

## Who it might suit

- Solo developers doing significant AI-paired programming who want session memory to survive a `/clear`
- Small teams (think 2-5 people) who want a shared board their agents can talk to without giving GitHub Issues another integration
- Operators self-hosting their tools who want one box, one cert, one update path, zero third-party dependencies

## Who it probably doesn't suit

- Teams of fifty looking for a Jira replacement
- Anyone needing single-sign-on across a corporate identity provider
- Anyone needing the board reachable from the public internet without further security work

## What you do with it

After install, the operator hits `https://<your-hostname>/` and creates a project. The system gives the agent a one-paragraph onboard prompt the operator pastes into the agent's first message. From then on the agent reads the board, writes to the board, and is gently reminded to re-read its rules whenever it goes too long without doing so. The operator stays in the editor; the board catches up via the agent.

That's the loop.
