---
title: "Identity, tiers, and project membership"
audience: public
status: 0.7.0-PRE-RC
version: 1.1.0
last_updated: 2026-05-09
authors: Parvez Khan + Claude (AI co-author)
---

# Identity, tiers, and project membership

Three actor types interact with the board: **users**, **agents**, and **the system itself** (audit rows, scheduled jobs). Within users there are three tiers: regular **User**, **Super User (SU)**, **Super Admin (SA)**. This doc explains who can do what and where the boundaries are.

## Actor types at a glance

```
                 ┌─────────────────────────────┐
                 │           SYSTEM            │
                 │  audit rows, scheduled jobs │
                 │  (no login surface)         │
                 └─────────────────────────────┘
                              ▲
              ┌───────────────┴────────────────┐
              │                                │
        ┌─────┴────────┐                ┌─────┴────────┐
        │    USERS     │                │    AGENTS    │
        │  (humans)    │                │  (AI tokens) │
        │              │                │              │
        │ login via    │                │ Bearer token │
        │ web UI       │                │ on every API │
        │              │                │ call         │
        │ session      │                │              │
        │ cookie       │                │ scoped to    │
        │              │                │ ONE project  │
        └──┬───────┬───┘                └──────────────┘
           │       │
     ┌─────┴───┐   │
     ▼         ▼   ▼
   board     admin
   surface   portal
```

**Users** log in via the web UI and get a session cookie. **Agents** authenticate every HTTP call with a Bearer token issued from the admin portal. **System** writes audit rows but isn't a login subject.

## The user tier ladder

Three tiers, layered. Higher tiers retain everything below.

```
                 ┌──────────────────────────────────┐
                 │       SUPER ADMIN (SA)           │
                 │                                  │
                 │  • System config                 │
                 │  • TLS cert wizard               │
                 │  • All bootstrap endpoints       │
                 │  • User management               │
                 │  • Cross-project oversight       │
                 │                                  │
                 │  CANNOT log into the board UI    │
                 │  (separate /admin/login surface) │
                 └────────────────┬─────────────────┘
                                  │ via /admin/login
                                  │
                 ┌────────────────┴─────────────────┐
                 │       SUPER USER (SU)            │
                 │                                  │
                 │  • Everything a User can do,     │
                 │    PLUS:                         │
                 │  • Cross-project visibility      │
                 │  • Elevated audit access         │
                 │  • Can request SA elevation      │
                 │    (24h cookie)                  │
                 └────────────────┬─────────────────┘
                                  │ via /ui/login
                                  │
                 ┌────────────────┴─────────────────┐
                 │            USER                  │
                 │                                  │
                 │  • Login to the board UI         │
                 │  • Read + write tasks in         │
                 │    projects they're a member of  │
                 │  • Post notes, change status     │
                 │  • Manage their own profile      │
                 │                                  │
                 │  Default tier — what an operator │
                 │  added to a team gets            │
                 └──────────────────────────────────┘
```

### Why SA is on a separate surface

The Super Admin tier holds the dangerous controls: cert rotation, system-wide settings, user creation, the install-bootstrap endpoints. Mixing those with daily-board-operator UX produces accidents — clicking "rotate cert" on the wrong project, or wiping a user when meant to suspend.

**SA logs in at a separate URL (`/admin/login`).** SA cannot log into the board UI at all. The session cookie is named differently (`vf_sa_session`) so the two contexts can't bleed into each other. After 30 minutes of inactivity the SA session times out and you're back to your User/SU identity if any.

This is **break-glass discipline**: dangerous controls require an explicit context switch, not a hidden permissions toggle.

### Why SU exists between User and SA

A User account is project-scoped — they only see projects they're a member of. SU is the cross-project tier — useful for an operator who runs many projects but doesn't need to do system-config work. SU sees everything; SU can request 24-hour SA elevation when they need to do a one-off admin action without changing identity.

For solo-operator installs, you may end up with one SU and one SA (yourself, twice). For small teams, you'd typically have:

- **Several Users** (one per team member)
- **One or two SUs** (lead + backup)
- **One SA** (the install owner)

## Agents

An agent is **not a user**. It's a project-scoped Bearer token issued from the admin portal.

| Property | Detail |
|---|---|
| Identity | Token (random 32+ char string), shown ONCE at issue + downloadable as `.agent-config` env file |
| Scope | One project — the agent cannot see or write to any other project |
| Authentication | Every API call carries `Authorization: Bearer <token>` |
| Display name | Operator-set at creation (e.g. "Claude", "Codex", "Cursor") |
| Lifetime | Tunable per-project (default 30 days; eternal tokens supported but not recommended) |
| Revocation | Single click in the admin portal; cascades immediately on next request |

The agent gets the **agent contract** at `GET /agentnotes` — a self-contained document describing the rules, the endpoints, and the recovery patterns. The agent reads this on first contact and re-reads when the drift gate fires.

### The drift gate (briefly)

Every API mutation by an agent goes through a freshness check: when did this agent last refresh its contract? If too long ago (default 1 hour), the next mutation returns 422 with instructions to re-read `/agentnotes` and retry. After re-reading, the agent has to answer a short session-state question (proves it actually re-grounded its working memory). After 4 escalations without passing, the agent's actions are frozen until a human clears the gate.

This is the mechanism that keeps mature agent sessions from acting on stale rules. The operator never has to remember to refresh; the system enforces it.

## Project membership

A project has **members**. Each member is a User with one of three roles:

| Role | Read | Write | Admin actions |
|---|---|---|---|
| `read` | ✓ | ✗ | ✗ |
| `write` | ✓ | ✓ | ✗ |
| `admin` | ✓ | ✓ | ✓ (rename project, manage members, archive) |

The User who creates the project becomes the first admin. Members are added by an existing admin (or by an SA from the admin portal).

```
   Project: example-website
   ├── alice         (admin)        ◀── created the project
   ├── bob           (write)        ◀── developer
   ├── carol         (read)         ◀── stakeholder, view-only
   └── claude-bot    (agent)        ◀── AI agent, not a user
```

Agents do **not** appear in the membership table. They have project scope baked into their token.

## Authentication mechanics

For users (board UI + admin portal):
- Login via web form or JSON POST to `/ui/login` (User/SU) or `/admin/login` (SA)
- Server issues a session cookie (`vf_session` for User/SU, `vf_sa_session` for SA)
- Cookie is HttpOnly + Secure + SameSite=Lax
- Subsequent requests authenticate via the cookie

For agents (HTTP API):
- Token issued in admin portal at `/admin/portal/administration/agents`
- Stored in the agent's `.agent-config` file (env-var format)
- Every API call carries `Authorization: Bearer <token>`
- No session — every call is independently authenticated

## Common questions

**Can a User be in multiple projects?** Yes. A User can be a member of any number of projects, possibly with different roles in each.

**Can an agent be in multiple projects?** No, by design. One token = one project. Issue separate tokens if the same AI client needs to work across projects.

**What happens when I revoke an agent?** The token is immediately invalidated server-side. The agent's next request returns 401. Active drift-gate state is cleared. Audit row written.

**Can I change a User's tier?** SAs can promote a User to SU or back via the admin portal. SA tier is set at install (the install script's first prompt) and at user-creation time; rotating an SA out of SA tier is a deliberate two-step action.

**What if I lose my SA password?** The bundled `scripts/reset_sa_password.py` lets a host operator with shell access reset it. There's no email-based recovery flow.

## What's not in this release

- Single sign-on (SSO) across services. The bundled Forgejo and Vaultwarden containers are not currently wired into the board flow (see `what-vibeforge-plus-is.md`); each service has its own login flow if you choose to enable it.
- LDAP / Active Directory integration.
- Multi-factor authentication (MFA).
- Per-task ACLs (access is per-project, not per-task).

If any of these are required for your environment, this isn't the right tool today.
