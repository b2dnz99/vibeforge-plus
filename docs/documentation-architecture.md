---
title: VibeForge+ Documentation Architecture
audience: public
ip: novel
style: technical
ip_first_dated: 2026-04-08
ip_authors: [pkhan, claude]
ip_disclosure_path: published
ip_summary: |
 Three-axis classification system (audience + IP + language style) combined
 with a generated TOC as the canonical project index, a guides tier whose
 content is agent-proposed and human-approved, and a build pipeline that
 emits filtered library bundles, an IP register, and tier-aware exports —
 all from frontmatter on the source MDs. The novelty is the COMBINATION:
 most projects either classify by folder structure (loses audience/IP
 independence) or by frontmatter without a generated index (loses
 discoverability), or have classification without a derivation-based
 guides tier (loses user-facing onboarding). Doing all three together
 produces a documentation system that is self-aware about its own audience,
 IP value, and writing voice.
status: 0.7.1-PRE-RC
version: 1.1.0
last_updated: 2026-05-09
authors: Parvez Khan + Claude (AI co-author)
---

# Overview

This is the **documentation system that VibeForge+ ships with**. It is the canonical reference for how docs are organised, classified, built, and discovered — both for *this* project (which is the first user of its own system) and for *every downstream project* that adopts the doc contract feature when an internal release ships.

It captures the architecture of the system, not the *content* the system holds. For content, see `0-MD/0-Documentation/TOC.md` (the canonical project index, generated from this system).

This doc is itself dogfood: it carries `audience: public`, `ip: novel`, `style: technical`, and lives in `0-Documentation/` alongside everything else it describes.

---

# 1. The folder layout

```
0-MD/
├── TOC.md                           ← canonical generated index (do not hand-edit)
├── TOC.html                         ← rendered, double-clickable
├── TOC.json                         ← machine-readable, for agents
├── TOC.template.md                  ← manual sections (Quick Links, Stack, etc.)
├── README.md                        ← entry point, points at TOC
│
├── 0-Documentation/                 ← canonical, active project documentation
│   ├── public/                      ← audience: public — safe to share, exportable
│   │   ├── activity-model.md
│   │   ├── agent-contract.md
│   │   ├── board-hierarchy.md
│   │   ├── documentation-architecture.md  ← THIS doc
│   │   ├── horizon-principle.md
│   │   ├── product-vision.md
│   │   └── user-agent-model.md                ← identity model (supersedes auth-agent.md v2)
│   ├── internal/                    ← audience: internal — team-only, not for export
│   │   ├── cto-rationale.md
│   │   ├── product-brief.md
│   │   ├── tech-index.md
│   │   ├── deploy.md                ← (formerly in confidential/, see §1b)
│   │   ├── recovery-procedures.md   ← (formerly in confidential/)
│   │   ├── threat-model.md          ← (formerly in confidential/)
│   │   └── user-agent-model-internal.md  ← (formerly in confidential/)
│   ├── rescue/                      ← audience: rescue — emergency operator content
│   │   └── bootstrap.md
│   └── guides/                      ← user-facing how-to content (mixed audience)
│       ├── README.md
│       └── (agent-proposed, human-approved guides)
│
├── proposed/                        ← captured thinking, not yet active
│   ├── DOC-CLASSIFICATION-PROPOSAL.md
│   ├── PROJECT-SCAFFOLD-PROPOSAL.md
│   ├── SYNC-ARCHITECTURE-PROPOSAL.md
│   ├── BOARD-PURPOSE-AND-PACT-PROPOSAL.md
│   ├── MCP-WIZARD-ARCHITECTURE.md
│   └── drafts/                      ← review-only YAML/conf snippets
│
├── progress/                        ← session state, handover history
│   ├── SESSION-HANDOFF.md
│   └── AUTH-PROGRESS.md
│
├── toolkit/                         ← operational tool documentation (planned)
│   ├── README.md
│   └── tools/
│       └── (one MD per tool)
│
├── library/                         ← built artefacts, regenerable
│   ├── VIBEFORGE-DOC-LIBRARY.html   ← latest pointer
│   ├── VIBEFORGE-DOC-LIBRARY-v3.0.html ← versioned snapshot
│   ├── MANIFEST.md                  ← versioning rules + index
│   └── snapshots/                   ← versioned zips at architectural inflection points
│       └── SNAPSHOT-2026-04-08-pre-classification.zip
│
└── archive/                         ← historical, deliberately preserved, not maintained
    └── (anything we explicitly want to keep but never read as current truth)
```

**Five top-level concepts** under `0-MD/`:

1. **TOC + README** — the entry points
2. **`0-Documentation/`** — active, canonical, in-use documentation
3. **`proposed/`** — captured thinking awaiting build / decision
4. **`progress/`** — session state + handover history
5. **Built artefacts** (`toolkit/`, `library/`) — regenerable, can be wiped without loss

Plus `archive/` for things we keep but don't maintain.

---

# 1b. Physical audience guards (added 2026-04-11)

The original spec was flat: every doc lived directly under `0-Documentation/` and frontmatter was the only classifier. The build pipeline respected that, but **humans don't read frontmatter** — they read folder names. The failure mode: a project user does `cp -r 0-Documentation/ ~/share-with-vendor/` thinking everything in there is shareable, when in fact some docs are `audience: confidential` or `audience: internal`. Frontmatter is invisible to `cp`, `tar`, `scp`, `zip`, drag-and-drop, and tab completion.

**The fix:** physical separation by audience. Docs live in subfolders that match their declared audience:

- `public/` — safe to share, can be bundled into exports
- `internal/` — team-only, do not include in customer-facing bundles (covers everything formerly under `confidential/` — see note below)
- `rescue/` — emergency operator content (bootstrap, recovery), bundled into the rescue card

> **`confidential/` collapsed into `internal/` (2026-05-05, an internal release/an internal release).** The tier was a vestige of a "to-be-sold product" framing that didn't materialise. The 4 docs that lived there — `deploy.md`, `recovery-procedures.md`, `threat-model.md`, `user-agent-model-internal.md` — are all *internal* in the operative sense (don't ship to customers). `public + internal` carries the load. The `audience: confidential` frontmatter value is deprecated; the bundle scripts treat anything not `public` as not-for-export.

**Frontmatter is still source of truth.** The folder is a physical guard, not a replacement. The build pipeline enforces that they agree:

- `scripts/build_toc.py` runs an audit pass before walking the docs tree
- For each `*.md` in an audience subfolder, it parses the frontmatter and verifies `audience` matches the folder name
- Any mismatch fails the build with a specific error: `AUDIENCE MISMATCH: <path> is in 'public/' but frontmatter says audience='internal'`
- Build aborts on mismatch — doesn't generate a stale TOC

**What this gives us:**

- `cp -r 0-Documentation/public/` is safe by definition
- Bundle scripts can `--exclude internal/` for export
- Tab-completion guides humans away from sensitive files
- Onboarding rule reduces to one line: *"you can read everything in `public/`"*
- Frontmatter and folder cannot drift silently — the build catches it

**Cost paid:**

- One extra directory level under `0-Documentation/`
- Build scripts walk one level deeper (already done in `walk_md` and the renderer)
- Architecture doc updated (this section)
- The audit is sub-millisecond and fails fast

**What this does NOT do:**

- It does not replace frontmatter classification — frontmatter still drives TOC grouping, library bundles, and IP register
- It does not apply to `proposed/` or `progress/` (those are not audience-classified, they are stage-classified)
- It does not affect `guides/` which is mixed-audience by design

**For downstream projects:** at the customer-facing scaffold stage (1.0 RC), this audience-folders pattern will be the default for the project template. Until then it lives in VibeForge+'s own dogfood instance and the architecture spec.

---

# 2. The three classification axes

Every doc carries three frontmatter fields. They are independent: a doc can be `public + novel + friendly`, or `internal + none + technical`, or any combination.

```
                ┌────────────────────────┐
                │  doc frontmatter       │
                │                        │
                │  audience: ...         │
                │  ip:       ...         │
                │  style:    ...         │
                └─────────┬──────────────┘
                          │
       ┌──────────────────┼──────────────────┐
       ▼                  ▼                  ▼
  ┌─────────┐       ┌─────────┐        ┌─────────┐
  │ AUDIENCE│       │   IP    │        │  STYLE  │
  │         │       │         │        │         │
  │ public  │       │ none    │        │technical│
  │ internal│       │commercial        │practical│
  │ rescue  │       │ novel   │        │ friendly│
  │ archive │       │ derived │        │  mixed  │
  │         │       │         │        │         │
  └────┬────┘       └────┬────┘        └────┬────┘
       │                 │                  │
       ▼                 ▼                  ▼
  bundle/export      IP register        agent voice
  filtering          attribution        when authoring
  TOC grouping       prior-art          reader assumption
```

**Audience** = who can read it. Drives bundle inclusion, export filtering, TOC grouping. Five values.

**IP** = what is it worth. Drives the IP register, attribution, future legal options. Four values. `ip: novel` requires four extra frontmatter fields: `ip_first_dated`, `ip_authors`, `ip_disclosure_path`, `ip_summary`.

**Style** = how is it written. Drives the agent's authoring voice and the reader's assumed level. Four values.

Full definitions live in `proposed/DOC-CLASSIFICATION-PROPOSAL.md` (the originating doc) and `agent-contract.md` §15.5 (the agent-side rules).

---

# 3. The build pipeline

```
                ┌─────────────────────────────────┐
                │  source markdown files           │
                │  (0-Documentation/, proposed/,   │
                │   guides/, toolkit/)             │
                └──────────────┬──────────────────┘
                               │
                               ▼
              ┌───────────────────────────────┐
              │  scripts/build_toc.py         │
              │  - walks filesystem            │
              │  - parses frontmatter          │
              │  - hashes content              │
              │  - groups by audience tier     │
              │  - generates IP register       │
              │  - merges with TOC.template.md │
              └──────┬────────────────────────┘
                     │
        ┌────────────┼────────────┐
        ▼            ▼            ▼
   0-MD/0-Documentation/TOC.{md,html,json}
   (git-track)  (browse)       (machine-read)
                                     │
                                     │
                ┌────────────────────┴───────────────────┐
                ▼                                         ▼
   ┌──────────────────────┐                ┌──────────────────────────┐
   │ build_doc_library.py │                │ build_toolkit_library.py │
   │  (reads TOC.json)    │                │  (reads TOC.json)         │
   │  filters by tier     │                │  rescue tier only         │
   └──────┬───────────────┘                └────────────┬─────────────┘
          │                                              │
          ▼                                              ▼
   library/VIBEFORGE-DOC-LIBRARY-vN.html       library/VIBEFORGE-RESCUE-CARD.html
   (single tabbed bundle, all-tier or filtered) (small, single-tier, emergency reading)
```

**Single source of truth: the markdown files.** Everything else is derived.

**TOC.json is the canonical machine-readable index.** Both library builders read from it. No hardcoded `DOCS = [...]` lists anywhere.

**Build is deterministic and sub-second.** Re-running with no source changes produces identical bytes (modulo `generated_date` stamps). No build server, no CI, no remote dependencies.

---

# 4. The TOC template merge

`TOC.md` is part hand-written, part generated. The template at `0-MD/0-Documentation/TOC.template.md` contains:

- **Manual sections** — Quick Links, Stack, Key Concepts, Open Source Prep, "How this TOC is built." These are stable and rarely change.
- **Placeholder tokens** — `{{generated_documentation_index}}`, `{{generated_guides_index}}`, `{{generated_ip_register}}`, `{{generated_proposed_index}}`, `{{generated_library_index}}`, `{{generated_progress_index}}`. The build script substitutes these with rendered tables.

The generated tables group docs by audience tier and show: `(title link) | ip | style | status | version | updated`. The IP register is rendered as a flat list with full per-doc detail.

This separation means:

- **Adding a doc** → no template edit needed; rebuild picks it up automatically
- **Changing a section header or quick link** → edit template, rebuild
- **Refactoring the table shape** → edit `render_doc_table` in `build_toc.py`, no template touch

---

# 4b. Document versioning convention

All proposals and architecture documents carry a `version` field in frontmatter. The version follows a two-tier scheme:

| Change type | Version bump | Examples |
|---|---|---|
| **Minor version** (0.x) | Structural change: new section added, section removed, significant reframing, new mechanism designed, scope change | 0.3 → 0.4 |
| **Point release** (0.x.y) | Typo fix, subtle reframe, wording clarification, cross-reference update, formatting consistency, frontmatter correction | 0.4 → 0.4.1 |

**The distinction:** if a reader who last read version 0.3 would need to re-read to understand a design change, that's a minor version. If they wouldn't notice the difference, that's a point release.

This convention applies to all documents in `proposed/`, `0-Documentation/`, and `toolkit/`. The library bundle version (`VIBEFORGE-DOC-LIBRARY-v{N}.html`) follows its own rules in `library/MANIFEST.md`.

---

# 5. The doc contract relationship

The doc contract is a **per-project** artefact (not built into VibeForge+ itself yet — it's the proposed feature in `PROJECT-SCAFFOLD-PROPOSAL.md` that ships when an internal release lands). It declares two things:

```
┌─────────────────────────────────────────┐
│  per-project doc contract (proposed)    │
│                                          │
│  ## Hard layer (gate-enforced)          │
│  - declared surfaces                     │
│    (auth, api, db, billing, ...)         │
│  - frontmatter validity                  │
│  - content hash drift                    │
│                                          │
│  ## Soft layer (agent-honoured only)    │
│  - default style                         │
│  - guides_wanted: [...]                  │
│  - voice notes                           │
│  - custom directives                     │
└─────────────────────────────────────────┘
```

The **hard layer** is mechanically enforceable by GUESS (the sync gate from `SYNC-ARCHITECTURE-PROPOSAL.md`). When a developer pushes to main, GUESS reads the doc contract, walks the project's `0-Documentation/` tree, and refuses the push if a declared surface is missing or has invalid frontmatter.

The **soft layer** is read by the agent at session start and used as authoring guidance. The gate does NOT enforce that guides exist or that style preferences are honoured — those are agent-side disciplines, not server-side constraints.

This split is intentional: machine enforcement where it works, prompt engineering where it doesn't, no mixing.

---

# 6. The guides tier — derivation-as-spark

Guides are a special category of documentation: **agent-proposed, human-approved, derived from surface docs.**

```
   project's surface docs
   (user-agent-model.md, etc.)
            │
            ▼
   ┌────────────────────────┐
   │  agent looks at the    │
   │  surfaces and proposes │
   │  starter guides        │
   │                        │
   │  trivial: 0–2          │
   │  non-trivial: 2–4      │
   └────────────┬───────────┘
                │
                ▼
   ┌────────────────────────┐
   │  human reacts          │
   │  yes / no / extend     │
   │  reword / replace      │
   └────────────┬───────────┘
                │
                ▼
   guides_wanted: [list] in doc contract
                │
                ▼
   agent writes guides on demand
   in friendly voice, derived from
   surface docs, never invented
```

**The agent never invents guides.** It proposes based on what it can see, the human picks, the picks become canonical. For trivial projects (a copy-paste app, a single-page demo) the right number of guides may be **zero** — and the agent should say so honestly.

For non-trivial projects (CRM, eCommerce, anything with users + data + workflows) there are usually 2–4 obvious starter guides: getting started, the first meaningful workflow, the most common error, the deployment story.

`derived_from` frontmatter captures the source surface docs for context, but **drift on guides is not enforced**. The agent may *suggest* a refresh when source surfaces change, but never refreshes without the human's nod.

---

# 7. Where this falls in the broader system

```
┌─────────────────────────────────────────────────────────────┐
│  PRODUCT (the persistence layer for vibe coders)             │
│                                                               │
│  ┌────────────┐  ┌────────────┐  ┌──────────────────────┐  │
│  │   GUESS    │  │  Project   │  │  Documentation       │  │
│  │  sync gate │←─┤  Scaffold  │←─┤  Architecture        │  │
│  │            │  │            │  │  (this doc)          │  │
│  │            │  │            │  │                      │  │
│  └─────┬──────┘  └──────┬─────┘  └──────────┬───────────┘  │
│        │                │                    │              │
│        │                │                    ▼              │
│        │                │           Three-axis classification│
│        │                │           + Guides tier           │
│        │                │           + TOC.json as spine     │
│        │                ▼                    │              │
│        │        Per-project scaffold ────────┘              │
│        │        with primitives + plan                       │
│        │                                                      │
│        ▼                                                      │
│   Server-side gate enforces hard layer of doc contract       │
│                                                               │
└─────────────────────────────────────────────────────────────┘
```

**This doc is the ground floor.** GUESS and the scaffold both depend on the classification system existing. The scaffold proposal references this doc for the doc contract format. GUESS references the doc contract for what to enforce. **Without classification, neither has a vocabulary to operate on.**

---

# 8. What every downstream project gets

When an internal release ships and a downstream project adopts the doc contract feature, it gets a copy of:

1. **The same folder layout** (`0-MD/0-Documentation/`, `proposed/`, `guides/`, `library/`, etc.)
2. **The same three-axis classification** (audience / ip / style enums in agent contract)
3. **The same TOC build script** (`build_toc.py` from the artefact catalogue)
4. **The same TOC template structure** (Quick Links + generated sections + Stack + Key Concepts)
5. **A starter doc contract** authored from the catalogue primitives during the planning conversation
6. **An IP register** that stays empty until the project itself produces something novel
7. **A guides folder** that stays empty until the agent proposes guides at first scaffold

**The downstream project's documentation system is structurally identical to VibeForge+'s own.** That's the dogfood: if we cannot maintain our own, we cannot ship the feature.

---

# 9. What this doc is NOT

- **Not a tutorial** for using VibeForge+. That's `bootstrap.md` and the (eventual) guides.
- **Not the doc contract spec.** That's `proposed/PROJECT-SCAFFOLD-PROPOSAL.md` §4 and §17.
- **Not the classification rules.** Those live in `agent-contract.md` §15.5 (canonical) and `proposed/DOC-CLASSIFICATION-PROPOSAL.md` (rationale).
- **Not the TOC content.** That's `TOC.md`, generated.
- **Not a build script reference.** Those are `scripts/build_toc.py`, `scripts/build_doc_library.py`, etc., and they're self-documenting.

**This doc is the architecture map.** It describes how the parts fit together. Read this first if you're trying to understand the system; read the parts if you're trying to use them.

---

# 10. Update triggers

Revise this doc when:

- A new top-level concept appears under `0-MD/` (a new sibling to `0-Documentation/`, `proposed/`, etc.)
- A new build script joins the pipeline
- The classification axes change (a new value, a new axis, an axis removed)
- The doc contract format changes
- The TOC template structure changes
- The downstream-project layout diverges from VibeForge+'s own

**Do NOT revise when:**

- A doc is added or removed from `0-Documentation/` (the TOC handles that)
- A frontmatter tag changes on an existing doc (TOC handles)
- A new guide is written (TOC handles)
- The library bundle is rebuilt (regenerable, no architectural change)

---

# 11. Sibling documents

- **`agent-contract.md`** — §15.5 has the canonical agent-side rules for the three axes and guides protocol
- **`proposed/DOC-CLASSIFICATION-PROPOSAL.md`** — full rationale for the classification system, captured before back-propagation
- **`proposed/PROJECT-SCAFFOLD-PROPOSAL.md`** — the per-project scaffold this system will eventually ship to downstream projects
- **`proposed/SYNC-ARCHITECTURE-PROPOSAL.md` (GUESS)** — the gate that will enforce the hard layer of the doc contract
- **`TOC.md`** — the canonical project index, generated by `build_toc.py`, the live consumer of this architecture
