# VibeForge+

**Version: 0.7.1-PRE-RC** · **License: GPL-3.0**

A self-managed task board for AI-paired programming. **Persistence layer + discipline framework**, single host, GPLv3.

> ⚠ Personal project, fully vibecoded, no formal threat-model review. Suitable for self-hosted small-team or single-operator use behind your own network controls. **Not for public-internet exposure without further hardening.**

## The gap

In AI-paired programming, the code drifts forward in git while the board — intent, decisions, what's blocked, what's next — sits silent because nobody stops to update it. Invisible inside the session; expensive when a new agent picks up the work cold next time.

## What it does

**Persistence layer** — projects, milestones, phases, tasks, notes, audit-trailed status changes. Talked to via a plain HTTP API any AI agent can use with a Bearer token (Claude, Codex, Cursor, GPT — agent-agnostic).

**Discipline framework** — gates that substitute for the consequence-loop humans carry by default and agents don't: required `transition_note` on every status change, required `docs_state` declaration on `needs_review`, a drift gate that periodically forces the agent to re-read its contract. The path of least resistance is "update the board" — the agent does it because the next state transition is gated on it.

## Who it suits — honest framing

**Not for prompt-and-forget.** If your style is "say it once and ship," the board's first `needs_review` is friction and you'll abandon it; the framing gate at the install wizard is the deliberate audience filter.

**Best fit: new or early-stage projects.** Bootstrap, not retrofit. Weak existing discipline cannot be unbroken — this tool doesn't claim to unstick it.

Not a Jira clone, not CI/CD, not customer support, not a compliance product.

## Install

Ubuntu 24.04 LTS on x86_64 (macOS users see [`INSTALL-MAC.md`](INSTALL-MAC.md)):

```bash
sudo VIBEFORGE_BRANCH=main bash scripts/vibeforge-install.sh
```

2-5 minutes on a fresh VM. Full guide: [`INSTALL.md`](INSTALL.md). After install, read [`docs/`](docs/) top to bottom — six docs, ~20 minutes.

## What ships

`docker compose up` brings up six containers: `app`, `db`, `caddy`, `health` (all wired), and `forgejo` + `vaultwarden` (bundled but **not wired into the board flow** in this release — safe to comment out for a leaner stack).

## Status

**0.7.1-PRE-RC** — orange-pulse label under the top-left logo is the visual reminder.

> **Release note (0.7.1):** This release incorporates fixes and documentation improvements surfaced by external install testing on macOS / Apple Silicon — install-script branch override, arm64 health-container build, install-script email-collision guard, and dedicated macOS install notes.

**Works:** board mechanics, agent contract, drift gate, install on fresh Ubuntu 24, Caddy self-signed, two-tier admin, audit log, full project / task / note CRUD via API + UI.

**Not in this release:** formal threat-model review, first-boot UI wizard, Forgejo + Vaultwarden wired in, multi-host, off-host backups, SSO.

## License, security, contributing

GPL-3.0 — see [`LICENSE`](LICENSE). Use, modify, redistribute under the same.

Security: see [`SECURITY.md`](SECURITY.md). Pre-RC, no SLA, single-maintainer triage.

Not currently accepting external contributions. Bug-report issues welcome; PRs filed will likely sit unreviewed.

## Credits

Built by **Parvez Khan** with **Claude (Anthropic)** as AI co-author. Fully vibecoded — codebase, docs, install path all produced through AI-paired-programming sessions. Released under GPL-3.0 in case it's useful to someone else doing similar work.
