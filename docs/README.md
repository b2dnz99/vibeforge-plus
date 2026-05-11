# VibeForge+ docs (0.7.0-PRE-RC bundle)

Customer-facing documentation that ships with this release. Read in two passes:

## Pass 1 — overview (read in order)

| File | What it covers |
|---|---|
| [`what-vibeforge-plus-is.md`](what-vibeforge-plus-is.md) | What the system is, what it tries to address, what isn't in this release |
| [`board-model.md`](board-model.md) | Entities (project / milestone / phase / task / note), relationships, status state machine |
| [`identity-and-membership.md`](identity-and-membership.md) | User / Agent / SU / SA tiers + project-membership model |
| [`admin-portal-tour.md`](admin-portal-tour.md) | What each section of the admin portal does + when to use it |
| [`operator-verbs.md`](operator-verbs.md) | Recommended verb vocabulary for talking to your AI agent |
| [`drift-gate.md`](drift-gate.md) | What the contract-drift mechanism does, why it exists, how to disable it |

## Pass 2 — deeper reference (read on demand)

| File | What it covers |
|---|---|
| [`agent-contract.md`](agent-contract.md) | Canonical agent contract — endpoints, response shapes, recovery patterns |
| [`board-hierarchy.md`](board-hierarchy.md) | Phase + milestone display rules + naming conventions |
| [`contract-drift.md`](contract-drift.md) | Deeper-dive on the drift mechanism's design + history |
| [`documentation-architecture.md`](documentation-architecture.md) | The doc system itself — frontmatter classification, build pipeline, TOC generation |
| [`identity-roles.md`](identity-roles.md) | The four-role identity model in depth |
| [`su-elevation-tier.md`](su-elevation-tier.md) | The SU → SA elevation flow specifically |
| [`user-agent-model.md`](user-agent-model.md) | Agent lifecycle + relationship to users + identity invariants |

For install instructions, see [`INSTALL.md`](../INSTALL.md) at the repo root.

---

Built by **Parvez Khan** with **Claude (Anthropic)** as AI co-author. GPL-3.0.
