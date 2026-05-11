---
title: "Board-shipped scaffold defaults"
audience: internal
status: active
version: "2.4.0"
last_updated: 2026-05-03
---

# `0-MD/.tools/` — board-shipped defaults

These four files were materialised here by your VibeForge+ board's onboard
workflow as **defaults**. They satisfy the OUR-block's Render & TOC Discipline
out of the box. **You can edit or replace any of them at any time** — they are
plain files in your repo, not externally-managed code.

## What's here

| File | Purpose | Default behaviour |
|------|---------|-------------------|
| `vf_render.py` | Render one Markdown file to a sibling HTML file | Project-root-aware (`Path.cwd()` / `--root`); refuses writes outside project; rich Markdown subset (frontmatter + headings + paragraphs + lists + **tables** + blockquotes + fenced code + horizontal rules + inline code/bold/italic/links); reads `template.html` for the styled shell |
| `vf_toc.py` | Build the documentation TOC | Scans `0-MD/0-Documentation/{internal,proposed}/**/*.md` by default (also `public/` if you create it on demand; excluding `archived/` everywhere); generates `0-MD/0-Documentation/TOC.md` + `.html` + `.json`; one section per non-empty class; deterministic; uses `template.html` for the HTML index |
| `template.html` | Styled HTML shell with slot substitution | **VibeForge+ MC theme** — dark violet, `Plus Jakarta Sans` + `JetBrains Mono`, sticky topbar with neon glow, sidebar TOC, doc-meta card with violet stripe, audience pill, table styling, code-block with violet accent stripe |
| `README.md` | This file | Bundle docs + supported-MD reference + house style + ASCII diagram patterns |

## How to use

From your project root:

```
# render one doc to its sibling .html
python 0-MD/.tools/vf_render.py 0-MD/0-Documentation/some-doc.md

# rebuild the TOC (renders every .md it indexes first, then emits TOC.md + .html + .json)
python 0-MD/.tools/vf_toc.py
```

`vf_toc.py` is the LAST step of the doc pipeline. It does two things in
order: (1) renders every `.md` it's about to index so the `.html` siblings
are guaranteed fresh on disk, then (2) emits `TOC.md` linking to `.md`
sources and `TOC.html` linking to `.html` rendered siblings. Each output's
links are RELATIVE TO THAT OUTPUT'S OWN LOCATION (so links work in any
markdown viewer for `TOC.md` and in any browser for `TOC.html`).

If a link target is missing at emit time (a render failed, a source got
deleted between scan and emit), the row is included with a visible
`[missing source]` (in TOC.md) or `[NEEDS RENDER]` (in TOC.html)
indicator + a stderr warning. The TOC is never refused — you decide what
to fix.

Both commands also support `--root <path>` if you need to invoke them from
outside the project root (e.g. CI or cross-project scripts):

```
python /elsewhere/vf_render.py --root /path/to/project 0-MD/some.md
python /elsewhere/vf_toc.py --root /path/to/project
```

`vf_toc.py --no-render` skips the render-orchestration pass — use only
when you've just rendered separately and want to re-emit the index.

### Replacing this tool

If you outgrow the default (your project has a different doc layout, you
want a richer TOC with tags / search / cross-references, you want a
different theme), replace `vf_toc.py` with your own. The contracts to
preserve when you do:

- **Render before index.** A TOC that links to artefacts which don't
  exist is worse than no TOC; it implies navigation works when it doesn't.
- **Per-output dependency contract.** If you emit both `.md` and `.html`
  views of the index, each links to the matching artefact format
  (markdown→markdown, html→html), with paths relative to that output's
  own location.
- **Verify and warn; never fail silent.** When a link target is missing
  at emit time, surface it in the output (visible indicator) and on
  stderr (one-line warning). Don't refuse to produce output.

The Python implementation is yours to swap; the conventions above are
what makes any TOC builder trustworthy.

---

## Handover → compact → absorb (the session-continuity cycle)

Long sessions hit **compaction** — the agent's context window fills up and
gets compressed lossy. Auto-compact strips information without negotiation;
your agent loses precision; you lose alignment. The framework's answer is
a deliberate cycle the human + agent run TOGETHER:

**1. Handover** (BEFORE compact). At a natural breakpoint — long session,
nearing context limits, end of a working day, switching agents, hitting a
compaction warning — the agent writes a session-handover doc capturing
what was just done, where state is, what's next. The doc is the durable
bridge across the lossy compact.

**2. Compact** — either auto-triggered by the chat platform OR explicitly
chosen by you (e.g. clearing context, switching sessions). Information IS
lost; the handover doc is what survives.

**3. Absorb** (AFTER compact). The next-session agent reads the handover
doc as part of cold-start re-grounding. The human re-reads it too if they
need to remember where they left off. Both halves are back in alignment;
work continues from the bridge.

### Why this beats auto-compact

- **Less lossy.** YOU choose what's load-bearing in the handover; auto-
  compact applies a generic compression.
- **Captured intent.** Your "what's next + open questions" lives in the
  handover doc instead of evaporating with the context window.
- **Cold-start cheap.** The next agent reads one doc and picks up clean,
  rather than re-deriving state from scratch.

### When to handover

- Long session approaching context limits (the chat surface usually warns)
- End of a working day if continuing tomorrow
- Switching agents mid-project (e.g. handing from Claude to Codex)
- Major milestone reached — natural punctuation point
- Whenever you sense "things are getting fuzzy on the agent's end"

### What goes in the handover doc

A session-handover doc lives at `0-MD/progress/SESSION-HANDOFF-{date}-{summary}.md`
and typically includes (adapt as fits the project).

> **Why `0-MD/progress/` and not under `0-MD/0-Documentation/`?** Handovers are
> *session-continuity escape-hatch artefacts*, not durable project documentation.
> They're written for one specific moment (the cross-compaction bridge) and lose
> usefulness once absorbed. Mixing them into the docs tree would bloat the TOC
> with one-shot artefacts and confuse the "what is durable knowledge?" signal.
> Keeping them at `0-MD/progress/` (a peer to `0-MD/0-Documentation/`) makes the
> distinction visible from the path alone. The bundled TOC builder lists them
> in a dedicated "Session continuity (handover artefacts)" section — visible but
> categorically separate from documentation.

- **State shipped** (what changed in this session — commits, deploys, board
  mutations)
- **Live runtime state** (versions, environment status, active gates)
- **Tickets touched** (status, URLs, what's pending)
- **Files of interest** (where work landed, what's modified locally)
- **Outstanding queue** (what's not yet done — the next-session priorities)
- **Cold-start checklist** (steps the next-session agent runs to re-ground:
  read this doc, fetch /agentnotes, check git log, etc)
- **Open questions for the human** (anything blocked on PK input)

### How the agent uses this on cold-start

The next-session agent's first move on absorbing a handover doc:

1. Read the most recent `SESSION-HANDOFF-*.md` in `0-MD/progress/`
2. Run the cold-start checklist from the doc
3. Refresh the contract (`GET /agentnotes/{slug}`) to grab any version
   changes that happened during the gap
4. Confirm to the human you've absorbed: "I've read the handover from
   {date} — picking up at {next-priority}. Confirm or redirect?"

### Operator agency: opt-in, not enforced

The framework can't make you do this — it provides the surface and the
discipline if you put it there. But the cycle is what makes session
continuity *actually work* across compaction events. Skip it and you'll
re-onboard the next agent from scratch. Do it and the next session picks
up where this one left off.

---

## Doc classes — internal + proposed by default; public on demand

The bundled TOC builder assumes a small set of doc classes living under `0-MD/0-Documentation/`. Each class answers a different question for the reader; the layout makes the answer visible from the path alone before they open the file.

```
  0-MD/
    0-Documentation/
      TOC.md             (built by vf_toc.py; canonical index)
      internal/          contributor-facing docs (default)
        archived/        out-of-TOC; kept for history
      proposed/          captured-thinking, pre-canonical (default)
        archived/        out-of-TOC; shelved without graduation
      public/            customer / outside-reader docs (CREATED ON DEMAND, not by default)
        archived/        out-of-TOC; kept for history
    progress/            session-continuity / handover artefacts
                         (peer to 0-Documentation, NOT under it; listed in a
                         dedicated TOC section, NOT counted as documentation)
  .scratch/              project root, gitignored, never in TOC
```

**Class meaning:**

- **`internal/`** — docs for people who work on the project: architecture, decision logs, runbooks, contributor onboarding. Internal vocabulary is fine here (your ticket IDs, your shorthand, your memory keys).
- **`proposed/`** — captured thinking BEFORE you've committed to it: design tradeoffs you're weighing, deferred decisions, thought-bubbles the human flagged worth capturing. Proposals graduate to `internal/` (or to `public/` if and when you create it for a specific outside reader) once the thinking settles. Proposals that don't graduate — abandoned ideas, superseded directions — move to `proposed/archived/` so the history survives without bloating the TOC.
- **`public/`** — docs for outside readers: customer-facing guides, public API references, README-style overviews. **Created on demand, not by default.** Most projects don't have a public technical readership — pre-creating `public/` invites unnecessary content + maintenance debt. Create the directory only when an outside reader specifically asks for documentation, and graduate the relevant `internal/` or `proposed/` doc into it then. The moment `public/` exists, the TOC auto-includes it AND `vf_render.py` activates an `audience: public` NOTE + a heuristic leak scan on every render — see *"What changes when public/ exists"* below.

**Lifecycle:**

```
  proposal authored          decision settles          graduates by audience
  ─────────────────────────► ─────────────────────────► ────────────────────►
  proposed/foo.md            (still useful?)            internal/foo.md   (default)
                             (yes → graduate)           OR public/foo.md  (only if explicitly needed)

                             (no → archive)             proposed/archived/foo.md
```

**ADAPT VOICE:** this layout is a sensible default, not a contract. For minimal projects, `internal/` alone is usually enough — light docs are almost always internal and small enough that a `proposed/` step adds ceremony without value. **If your project already organises docs differently and it works, keep yours** — adapt `vf_toc.py`'s `SCAN_DIRS` to match your reality, or replace the TOC builder entirely. **ASK THE HUMAN before re-organising an existing doc tree to match this default** — re-homing en-masse is the kind of move that wants explicit human sign-off, not autopilot.

### Why `public/` is created on demand, not by default

Every directory in a default scaffold tree carries an implicit *"please populate me"* directive. Customers and agents respect that directive faithfully and create maintenance debt. For most projects:

- The agent surface (contract endpoint + teachable 422 envelopes + structured specs) IS the public-facing programmable surface; humans hitting the system are usually stuck-or-researching, not browsing per-project public docs.
- Outside-reader audiences (curious tinkerers, end-users of your product) are typically served by marketing / landing / blog surfaces, not by the project's per-project doc tree.
- A pre-created `public/` directory invites content into it because tiers are read as *"things to fill"*, not *"things to fill on demand"*.

So: `public/` is omitted from the default tree on purpose. When you DO have an outside reader who specifically asked for documentation, create the directory and graduate the relevant doc — that act IS the human signal that the tier is wanted.

### What changes when `public/` exists

The moment a `public/` directory exists with at least one `audience: public` doc, two things activate at render time:

1. **Always-fires NOTE on every `audience: public` render.** Before the leak scan runs, `vf_render.py` writes a stderr line:
   > `[VF-RENDER NOTE] {file} (audience=public): the bundled scan only catches a narrow class of generic internal-jargon markers (ticket-shape codes, IC-XXX, memory-key shapes). It is NOT an IP / PII / trade-secret / client-name / commercial-confidence check. You know your customer, your contracts, your codenames - protect your own IP. ASK THE HUMAN to eyeball any public doc before it ships externally; the agent's review is necessary but not sufficient.`

   Fires unconditionally — even when the leak scan finds nothing. A clean leak-pattern run is the most dangerous moment for false confidence; the NOTE is the standing caveat that the automated scope is narrow and the rest is human judgement.

2. **Heuristic `[VF-RENDER WARN]` per leak-pattern hit.** `PUBLIC_LEAK_PATTERNS` (a short regex list at the top of `vf_render.py`) checks for ticket-shape codes (`ABC-123`), `IC-NNN` markers, and memory-key shapes (`feedback_X`). On hit, stderr names the file + line + match + suggested actions. False positives are expected — silence by editing the regex list, rephrasing the doc, re-classifying the file, or asking the human if unsure.

Together: the NOTE is the always-on responsibility transfer; the WARN is the specific hits. Neither blocks rendering — both are cooperative signals.

### When does something warrant a proposal?

Proposals are not free: each one adds a row to the TOC, accumulates context for future readers, and creates a graduation-or-archive obligation. The brake against over-using them is honest self-assessment, not a hard rule.

> **If you can't name what made this worth a proposal, it's not one.**

A doc earns its place in `proposed/` when at least one of these is true:

- **Multiple design tradeoffs** that need to be visible together (A vs B vs C, with rationale per option) before a decision lands.
- **Deferred decision** — you want to capture the thinking now while context is fresh, but the call gets made later (next session, after a metric, with the human present).
- **Human-flagged thought-bubble** — the human said "capture this" or "MD it" or similar; treat that as the trigger.
- **Cross-cutting impact** that warrants discussion before code touches multiple surfaces.

A doc does **not** warrant a proposal when:

- The decision is already made and the doc just records it (write it directly to `internal/` or `public/`).
- It's a restatement of context already in the ticket / chat / commit message.
- It's a single small refactor or bug fix's design (use a commit message or a ticket note).
- You're "just being thorough" — thoroughness without a named tradeoff is documentation theatre.

**Optional `triggered_by:` frontmatter field** — encouraged on proposals so future readers can tell what surfaced the thought:

```
---
title: Cache invalidation strategy
audience: internal
status: proposed
triggered_by: post-incident review on 2026-04-12 cache-stampede
---
```

Not required by any tool; just a useful breadcrumb. **AGENT NEEDS TO ADAPT** the contents of `triggered_by:` to your project's reality (your ticket IDs, your incident IDs, your conversation references). **ASK THE HUMAN** if unsure whether something warrants a proposal vs. going straight to `internal/`/`public/` — the cost of asking is one round-trip; the cost of an over-proposed TOC is reader-fatigue that erodes signal.

---

## Supported Markdown subset

The bundled renderer (`vf_render.py`) handles this subset cleanly. **Stick
within it for predictable HTML output**; reach beyond it only when you've
extended the renderer too. Anything not on this list will appear as raw text
or fall through to paragraph rendering.

### Frontmatter (YAML between `---` fences)

Stripped from output. Surfaced in the doc-meta card at the top of the rendered
page.

```
---
title: My Doc
audience: internal
status: Active
version: 0.1.0
last_updated: 2026-05-01
authors: Project Team
---
```

Recognised keys: `title`, `audience`, `status`, `version`, `last_updated`,
`authors`, `supersedes`. Other keys are ignored by the renderer (but are
fine to include for other tooling).

`audience` values change the audience-pill colour: `public` (violet), `internal`
(amber), `confidential`/`sa` (red), `rescue` (red).

### Headings (`#` through `######`)

Anchored automatically (slug from heading text) for TOC linkage.

```
# H1 (only one per doc, please)
## H2 (sidebar shows these as primary entries)
### H3 (sidebar shows these as nested)
#### H4 (uppercase styled, no TOC entry)
```

### Paragraphs

Multiple lines in the same paragraph are joined with a space. Blank line ends
the paragraph.

### Lists (ordered + unordered)

```
- bullet item
- another bullet
* same as dash

1. numbered item
2. another numbered
```

Mix of `-` and `*` is fine within the same list. Nested lists not currently
parsed (use sub-headings or paragraphs instead).

### Tables (Markdown pipe tables)

Header row + separator row (with optional `:` for alignment) + body rows.

```
| Column A | Column B | Column C |
|----------|:--------:|---------:|
| left     | center   | right    |
| more     | data     | here     |
```

Alignment per column: default left, `:---:` center, `---:` right.

### Blockquotes

```
> Single-line quote.
> Multi-line quotes are joined into a single quote block.
> Use bold/italic inside as normal.
```

### Fenced code blocks

Triple-backtick fences with optional language hint. Whitespace and newlines
preserved exactly.

````
```python
def hello():
    print("world")
```
````

### Horizontal rule

```
---
```

(Three or more dashes on their own line.)

### Inline formatting

| Markdown | Renders as |
|----------|------------|
| `` `code` `` | `inline code` (violet-tinted background) |
| `**bold**` | **bold** |
| `*italic*` | *italic* |
| `[text](url)` | [text](url) |

---

## House style — advisory for writers (humans + agents)

Small set of conventions that make docs more readable and render predictably:

- **One H1 per doc.** Use `## H2` for top-level sections, `### H3` for
  sub-sections. The sidebar TOC shows H1-H3.
- **Short headings.** 2-6 words. Long headings break the sidebar layout.
- **One idea per paragraph.** Multiple ideas → multiple paragraphs (or
  bullets). Walls of text don't render well in the narrow doc column.
- **Prefer bullets to long prose** for any list-of-things. Three-line
  paragraph that names three things → three bullets.
- **Code blocks for any structured non-prose content.** Filenames, paths,
  CLI commands, config snippets, API payloads, JSON, ASCII diagrams — all
  belong in fenced code blocks (preserved whitespace + monospace font).
- **Inline `code` for short identifiers** in flowing prose: filenames,
  function names, env vars, status values.
- **Tables for tabular data only.** Matrix of options, comparison grids,
  state transitions — yes. Lists of bullets dressed up as tables — no.
- **Blockquotes for emphasis on a key statement** — usually one per doc,
  near the top, summarising the main point. Don't overuse.
- **Frontmatter on every doc.** At minimum `title` + `audience`. Add
  `status`/`version`/`last_updated` once the doc has any history.

---

## Recommended pattern: ASCII diagrams

For architecture, workflows, sequence flows, decision trees, state machines,
and project-shape visualisations, **prefer ASCII over external image
tooling**. The bundled renderer preserves whitespace + monospace font inside
fenced code blocks — diagrams render predictably without any image pipeline,
embed step, or external dependency.

### Why ASCII beats images in this codebase

- **text-searchable + grep-able** — find any node/edge/state by name
- **survive plain-text contexts** — chat, terminal, plain-text agent
  message windows, log lines
- **no rendering toolchain** — no Mermaid, no PlantUML, no SVG export, no
  image-host upload
- **agents can produce them in 5 seconds** without image-generation tools
- **diff well in git** — line-level diff of structure changes
- **blame well in PR review** — change attribution per character if needed

Reach for image diagrams only when the visualisation genuinely cannot be
expressed in text (rare). Otherwise: ASCII.

### Character set

```
Box-drawing:  ┌ ┐ └ ┘ ├ ┤ ┬ ┴ ┼ ─ │
Heavy:        ┏ ┓ ┗ ┛ ┣ ┫ ┳ ┻ ╋ ━ ┃
Double:       ╔ ╗ ╚ ╝ ╠ ╣ ╦ ╩ ╬ ═ ║
Arrows:       → ← ↑ ↓ ↔ ↕ ⇒ ⇐ ⇑ ⇓ ⇔ ⤴ ⤵
Trees:        ├── └── │
Bullets:      • ◦ ▪ ▫ ●
Misc:         ✓ ✗ ★ ☆ ▲ ▼ ◆ ◇
```

### Pattern: architecture diagram (component + data flow)

```
   ┌─────────────┐    GET /agentnotes/{slug}     ┌──────────┐
   │  Customer   │ ─────────────────────────────►│  Board   │
   │   Agent     │                               │  (DEV)   │
   └─────────────┘ ◄─────────────────────────────└──────────┘
          │           200 + refresh_nonce + rules     ▲
          │                                           │
          │ POST /onboard-state/ack                   │
          │   {step, value, surfaced_summary}         │
          └───────────────────────────────────────────┘
```

### Pattern: workflow / sequence (steps in time order)

```
  agent                      board                     human
    │                          │                         │
    │ 1. GET /onboard/framing  │                         │
    │ ────────────────────────►│                         │
    │ ◄─── framing + OUR-block │                         │
    │                          │                         │
    │ 2. paste 3 sentences     │                         │
    │ ─────────────────────────────────────────────────► │
    │                          │                         │
    │ 3. POST /ack             │                         │
    │   {surfaced_summary}     │                         │
    │ ────────────────────────►│                         │
    │ ◄─── 200 (gate clears)   │                         │
```

### Pattern: state transitions (status machine)

```
                   ┌───────────┐
        ┌─────────►│ backlog   │
        │          └─────┬─────┘
        │                │ (assign + start)
        │                ▼
        │          ┌───────────┐
        │          │   ready   │
        │          └─────┬─────┘
        │                │ (agent starts)
        │                ▼
        │          ┌───────────┐
        │ (revisit)│ in_progress│
        │ ◄────────└─────┬─────┘
        │                │ (agent completes)
        │                ▼
        │          ┌────────────┐    HUMAN ONLY    ┌──────┐
        │          │needs_review│ ────────────────►│ done │
        │          └─────┬──────┘                  └──────┘
        │                │ (human cancels)              ▲
        │                ▼                              │
        │          ┌────────────┐                       │
        └──────────│ cancelled  │                       │
                   └────────────┘                       │
                                                        │
                  Note: agent → done is server-rejected (422)
                  per Human-Closure Discipline. The closure
                  ceremony belongs to the human.
```

### Pattern: decision tree

```
  Need to render a doc?
      │
      ├── markdown only?  ──── yes ──► python 0-MD/.tools/vf_render.py <file>
      │
      ├── needs custom CSS?  ── yes ──► edit 0-MD/.tools/template.html
      │
      └── needs new MD feature?  ── yes ──► extend vf_render.py md_to_html()
```

### Pattern: project shape / tree

```
  Project root/
  ├── 0-MD/                          docs only
  │   ├── 0-Documentation/           doc tree (default = internal + proposed)
  │   │   ├── TOC.md                 built by vf_toc.py
  │   │   ├── internal/              contributor-facing docs (default)
  │   │   │   └── archived/          (out of TOC; kept for history)
  │   │   ├── proposed/              captured-thinking, pre-canonical (default)
  │   │   │   └── archived/          (out of TOC; shelved without graduation)
  │   │   └── public/                customer-facing docs (CREATE ON DEMAND ONLY)
  │   │       └── archived/          (out of TOC; kept for history)
  │   └── .tools/                    this scaffold (you're reading this README)
  ├── src/                           (or wherever your project source lives)
  └── .scratch/                      ephemeral / API snapshots (gitignored)
```

---

## What to change if you want

- **Visual style** → edit `template.html`. Change colours, fonts, layout — the
  `{title}/{brand}/{path}/{audience_class}/{audience_short}/{audience_label}/{toc}/{meta_dl}/{body}`
  slots get substituted at render time. Any HTML/CSS/JS is fair game.
- **Brand label in topbar** → currently hardcoded to `Project Docs` in
  `vf_render.py`. Change `brand = "Project Docs"` to your project's name.
- **Markdown features** → edit `vf_render.py`'s `md_to_html()`. Add tables-with-
  rowspan, footnotes, MathJax, mermaid diagrams, etc. as needed.
- **Doc tree layout** → edit `vf_toc.py`'s `SCAN_DIRS` (lists
  `internal`, `public`, `proposed` under `0-MD/0-Documentation/`; only
  non-empty classes appear in the generated TOC). Add/remove/rename
  classes to match your project's shape; the heading text per class is
  fully editable. Edit `EXCLUDED_PATH_PARTS` to change which subdirs
  (default: `archived`) get filtered out of the live TOC.
- **Output formats** → edit `vf_toc.py`'s `main()`. Drop `TOC.json`, add a
  sitemap, change the markdown table format — your call.
- **Replace entirely** → if you have a preferred toolchain (mkdocs, hugo,
  sphinx, mdBook, etc.), swap these scripts out. Just keep the entry-point
  command names working (`vf_render.py <file>` / `vf_toc.py`) so your
  contract's MANDATORY discipline still has a working command to call.

---

## Why these are defaults (not just samples)

The OUR-block in your project's `AGENTS.md` (or `CLAUDE.md`) declares Render
& TOC Discipline as MANDATORY. The board ships these working defaults so the
discipline doesn't fail on day one. Without them, your contract would name
commands that don't exist — and the agent would either fail or improvise.

If your project genuinely doesn't need this discipline, consider editing the
served OUR-block on the board side rather than deleting these locally — that
way *future* projects don't inherit a contradiction either.

---

## Re-fetch from board

If you ever want to reset to the latest board defaults (e.g., when board
rules change or you want to start over):

```
curl -H "Authorization: Bearer $VIBEFORGE_TOKEN" \
     "$VIBEFORGE_API/onboard/scaffold" \
     | python -c "
import json, sys, pathlib
data = json.load(sys.stdin)
for art in data['artefacts']:
    p = pathlib.Path(art['path'])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(art['content'], encoding='utf-8')
    print(f'wrote {p}')
"
```

This overwrites your local versions with the current board defaults.
