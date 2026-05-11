# VibeForge+

**Version: 0.7.0-PRE-RC** · **License: GPL-3.0**

A self-managed task board for AI-paired programming. The board holds the intent (tasks, decisions, progress, audit) that the editor doesn't track, and any AI agent — Claude, Codex, Cursor, GPT — can talk to it via a simple HTTP API.

> ⚠ **Internal use only — use at your own risk.**
>
> Personal project, fully vibecoded, released as a curiosity in case it's useful to someone. A formal threat-model review has not been completed. Suitable for self-hosted small-team or single-operator use behind your own network controls. **Not for public-internet exposure without further hardening.**

## What it is

- A task board with projects, phases, tasks, notes, and audit-trailed status changes
- An HTTP API any AI agent can talk to with a Bearer token
- An agent contract the system serves at `/agentnotes` — rules, endpoints, recovery patterns
- A drift gate that periodically forces the agent to re-read its contract
- A two-tier admin model (Super User / Super Admin) keeping daily-operator UX away from system-config

## What it is NOT

- Not a project-management SaaS, not a Jira clone
- Not a CI/CD platform
- Not a customer support tool
- Not a compliance product

## What's in the bundle

Single host. `docker compose up` brings up six containers:

- `app` (board API + UI), `db` (Postgres), `caddy` (TLS), `health` — wired and used
- `forgejo`, `vaultwarden` — bundled but **not currently wired into the board flow**; safe to leave running, safe to comment out of `docker-compose.yml` for a leaner stack

## Install

Ubuntu 24.04 LTS, root, internet, ~5 GB free disk. See [`INSTALL.md`](INSTALL.md) for the full guide.

```bash
sudo bash scripts/vibeforge-install.sh
```

The script handles Docker install, source extraction, prompts for hostname + SA + first-user credentials, generates `.env` with random secrets, brings the stack up, runs migrations, creates accounts. Total: 2-5 minutes on a fresh VM.

## Documentation

The [`docs/`](docs/) folder ships with the bundle:

| Doc | Covers |
|---|---|
| [`docs/what-vibeforge-plus-is.md`](docs/what-vibeforge-plus-is.md) | What the system is, what it tries to address, what isn't in this release |
| [`docs/board-model.md`](docs/board-model.md) | Entities, relationships, status state machine |
| [`docs/identity-and-membership.md`](docs/identity-and-membership.md) | User / Agent / SU / SA tiers + project membership |
| [`docs/admin-portal-tour.md`](docs/admin-portal-tour.md) | What each admin portal section is for |
| [`docs/operator-verbs.md`](docs/operator-verbs.md) | Recommended verbs for talking to your AI agent |
| [`docs/drift-gate.md`](docs/drift-gate.md) | The contract-drift mechanism + how to disable it |

Read in order, top to bottom.

## Status

**0.7.0-PRE-RC** — pre-release. The orange-pulse label under the top-left logo on every page is the visual reminder.

What works: board mechanics, agent contract, drift gate, install on Ubuntu 24, Caddy self-signed mode, two-tier admin model, audit log, project/task CRUD via API + UI.

What's not in this release: formal threat-model review, first-boot UI wizard (install is via bash script), Forgejo + Vaultwarden wired into the board, multi-host / cluster mode, off-host backup automation, SSO across services. If any of these matter for your environment, this isn't the right tool today.

## License

GPL-3.0 — see [`LICENSE`](LICENSE). You can use it, modify it, redistribute it under the same license. The source is yours.

## Security

See [`SECURITY.md`](SECURITY.md) for how to report security issues. Pre-RC means there's no SLA and no security team — issues are triaged manually.

## Contributing

Not currently accepting external contributions. The project is in a personal-use phase; any PRs filed will likely sit unreviewed. Filing issues is welcome — bug reports especially — though triage cadence is irregular.

## Credits

Built by **Parvez Khan** with **Claude (Anthropic)** as AI co-author. The project is fully vibecoded — the codebase, the docs, and the install path were all produced through AI-paired programming sessions. Released under GPL-3.0 in case it's useful to anyone else doing similar work.
