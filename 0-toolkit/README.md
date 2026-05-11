---
title: VibeForge+ Operational Toolkit
audience: rescue
ip: none
style: practical
status: Active
last_updated: 2026-04-08
---

# Operational toolkit

VM-level scripts that bootstrap, recover, and verify the VibeForge+ stack. **Read this folder when something is broken or you need to spin up a new install.**

## What lives here

Each tool gets one MD file in `tools/` describing:
- What it does
- When to reach for it
- All subcommands with usage and example output
- Where credentials live (and how to find them)
- Gotchas, recovery context, what NOT to do

The actual scripts live in `scripts/` at the repo root, deployed to `/opt/vibeforge/scripts/` on the VM. The MD files in this folder are the **operator's manual**, not the source code.

## When to use the toolkit

- **Fresh install** of VibeForge+ on a new VM → run the bootstrap subcommands in dependency order
- **Lost a credential** (admin password, service token, etc.) → run the matching reset subcommand
- **Service is in a weird state** → run the matching verify subcommand to identify what's broken
- **Just want to confirm everything is healthy** → run `verify` on each tool

## The bootstrap pattern

Every tool follows the same shape:

```
<tool>.sh bootstrap              ← fresh init (refuses if already done)
<tool>.sh reset-{thing}          ← regenerate a specific credential
<tool>.sh verify                 ← read-only health + auth round-trip
```

Some tools have additional subcommands. All use the **same park location**: `/opt/vibeforge/.bootstrap/<tool>.json` (chmod 600, root-owned).

The pattern is documented in `0-MD/0-Documentation/proposed/pending-RC-1.0/INSTALL-RECOVERY-TOOLKIT-PROPOSAL.md`.

## Currently in this folder

| Tool | Subject | Tier |
|---|---|---|
| `tools/reset_sa.md` | VibeForge+ Super Admin password reset | rescue |
| `tools/bootstrap_forgejo.md` | Forgejo (board git) bootstrap + recovery | rescue |

More tools land here as the install/recovery toolkit grows. Vaultwarden, Caddy (if/when), Postgres role provisioning, and an umbrella `verify_stack.sh` are all on the roadmap.

## Discipline rule

**If a script exists in `scripts/` for operational bootstrap or recovery, it MUST have a matching `tools/<name>.md` here.** Without the doc, the operator at 3am has nothing to read. Without the script, the doc has nothing to invoke.

The **toolkit library** (`scripts/build_toolkit_library.py`, planned) bundles every tool MD into a single portable HTML for offline emergency reading — even when the board UI is broken, the rescue card still opens in any browser.

## See also

- `0-MD/0-Documentation/proposed/pending-RC-1.0/INSTALL-RECOVERY-TOOLKIT-PROPOSAL.md` — full pattern rationale
- `0-MD/0-Documentation/public/agent-contract.md` §15.5 — audience tagging (`rescue` tier)
- `0-MD/0-Documentation/public/documentation-architecture.md` — where the toolkit fits in the broader doc system
