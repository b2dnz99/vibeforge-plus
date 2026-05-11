---
title: "Admin portal — tour and section guide"
audience: public
status: 0.7.0-PRE-RC
version: 1.1.0
last_updated: 2026-05-09
authors: Parvez Khan + Claude (AI co-author)
---

# Admin portal tour

The admin portal lives at `https://<your-hostname>/admin/login`. It's a separate surface from the board UI, with its own login flow and session cookie. Only **Super-Admin (SA)** can reach it.

This doc walks the four main sections, says what each one is for, and flags when you'd actually use it.

## Why a separate portal exists

See [`identity-and-membership.md`](identity-and-membership.md) for the full reasoning. Short version: dangerous controls (cert rotation, system config, bootstrap endpoints, user management) are kept off the board UI so daily operators don't accidentally hit them. SA logs in at `/admin/login`, gets a 30-minute timed session, does the admin work, signs out.

If you're an SU operator who occasionally needs to do an admin action, you can request **SA elevation** from your User/SU profile — gives you a 24-hour stacked SA cookie that retains your underlying SU identity.

## Portal sitemap

```
   /admin/portal/
   │
   ├── overview                          ◀── landing page, system snapshot
   │
   ├── administration/
   │   ├── users                         ◀── user CRUD, role management
   │   ├── agents                        ◀── agent token issue / cycle / revoke
   │   ├── agent-telemetry-and-drift     ◀── per-agent metrics + drift state
   │   └── audit                         ◀── system-wide activity log
   │
   ├── health/
   │   ├── overview                      ◀── containers + TLS + DB + uptime
   │   └── audit                         ◀── (alias of administration/audit)
   │
   ├── configuration/
   │   ├── certificates                  ◀── TLS cert wizard
   │   └── session-policy                ◀── session timeouts + token TTLs
   │
   └── lifecycle/
       └── environment                   ◀── what host am I on? tier banner
```

## The four section headings

### 1. Administration

Day-to-day SA work. The four workspaces:

**Users** — create / suspend / soft-delete / change role / reset password / force password change. Each user row links to a detail panel showing their session history, recent activity, and project memberships. The audit panel inside each user shows every action taken by them.

**Agents** — same shape as users but for AI agent tokens. Issue a new token (one-time download as `.agent-config`), cycle (rotate the token without losing the agent identity), revoke (immediate kill). Each agent row shows last-seen timestamp, API call count, and drift-gate state.

**Agent telemetry & drift** — the observability surface. Per-agent: API call counter, last-contract-read time, drift-gate eval pass/fail history. Plus the system-wide drift-gate toggle (`enabled` = full enforcement; `disabled` = "observation mode" — logs everything, suppresses freeze).

**Audit** — every mutation in the system, filterable by actor, action, project, time window. The forensic record.

When you'd use Administration:
- Onboarding a new team member → Users
- Issuing an AI agent its first token → Agents
- "Why is the agent stuck?" → Agent telemetry + drift
- "Who renamed this project?" → Audit

### 2. Health

System-state observability. Two views:

**Overview** — the dashboard. Container statuses (6 of 6 healthy), TLS cert state, DB stats, request counts, recent errors. Refreshes live. Shows red badges when something's wrong.

**Audit** — same view as `administration/audit`, just linked from a different sidebar location for the operators who think of audit as "a health concern" rather than "an admin concern."

When you'd use Health:
- Routine "everything OK?" check
- Cert about to expire?
- Container restarted, what happened?
- "Which container is sick?"

### 3. Configuration

System-config surfaces. Two workspaces:

**Certificates** — the TLS cert wizard. Four modes:
- `caddy_internal` (the default — Caddy mints a self-signed cert from its bundled CA)
- `file` (operator uploads a PEM bundle from their own CA)
- `acme` (Let's Encrypt — requires public DNS + port 80 reachable)
- `self_signed` (legacy — same effect as `caddy_internal`)

The wizard shows the current cert state (subject, SAN, expiry, days remaining), lets you swap mode + paste / upload new cert material in place, and triggers a Caddy reload to pick it up. The Caddy CA root is downloadable from this page for operators who want to install it as a trusted root on their devices.

**Session policy** — knobs for session timeouts + token TTLs:
- Cookie session lifetime (default: 24 hours, max: 7 days)
- Agent token default TTL (default: 30 days, max: 365 days)
- SA elevation lifetime (fixed: 30 minutes)
- SU-elevated-to-SA cookie lifetime (fixed: 24 hours)
- Project wizard token expiry (15m / 1h / 6h / 12h / 18h / 24h — for the agent-onboarding flow)

When you'd use Configuration:
- Initial install: switch from self-signed to a real cert (cert wizard)
- New device coming online: download CA bundle (cert wizard)
- "Sessions are timing out too fast" (session policy)
- "Tokens last too long for our security posture" (session policy)

### 4. Lifecycle

**Environment** — the "what am I looking at right now" page. Shows hostname, tier banner (PROD / staging / local), Postgres version, Alembic head, install time (earliest user creation), Python version, container OS timezone, NTP template.

The tier banner is the load-bearing thing: a coloured dot + label telling you whether the install you're staring at is a production environment or a staging one. Used as the last sanity-check before any destructive action.

When you'd use Lifecycle:
- Before doing anything destructive: confirm tier
- "What version of the database am I on?"
- "What time does the host think it is?"

## Visual cues across the portal

- **Red text** = mutation buttons (delete, revoke, rotate cert, archive). Anything dangerous.
- **Padlocked items** = roadmap placeholders for sections not yet shipped.
- **Pulsing green pill (SU · Elevated)** = your underlying SU session
- **Pulsing red+amber pill (SA · Elevated)** = SA privilege available on top of your SU
- **Solid red pill (SA · Break-Glass)** = pure SA cookie, no underlying user identity

The pill in the top-right tells you what tier you're operating as. If it pulses, look at it before clicking anything.

## What's NOT in the portal

- **Backup management** — no in-portal "back up the DB" button. Use `pg_dump` from the host shell, or set up your own cron.
- **Project deletion** — projects can be archived from the board UI, but full deletion (with data removal) is not implemented. Archiving is the available alternative.
- **Multi-host / cluster management** — single-host install only.
- **Real-time alerting** — health overview shows current state but doesn't push alerts to email / chat / pagers.

If any of these are required for your environment, this isn't the right tool today.
