"""
Build the single tabbed VibeForge+ Doc Library — the portable, versioned bundle
of every architecture and product doc that matters at this point in time.

This is dogfood of the documentation contract: one location, one build, one
artefact, versioned, with rationale embedded. Run from repo root:

  python scripts/build_toc.py        # rebuild the canonical TOC.json first
  python scripts/build_doc_library.py # then build the library FROM the TOC

Output:
  0-MD/library/VIBEFORGE-DOC-LIBRARY-v{N}.html   ← versioned, immutable per build
  0-MD/library/VIBEFORGE-DOC-LIBRARY.html        ← latest pointer (overwrites)
  0-MD/library/MANIFEST.md                       ← what's in the bundle, why, version table

Source of truth: 0-MD/0-Documentation/TOC.json (regenerate with scripts/build_toc.py).
This script reads TOC.json, applies the LABELS / CATEGORY mapping below for
friendly tab names + tab grouping, and bundles the docs.
"""
import json
import re
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
from render_architecture_docs import md_to_html, build_toc, parse_frontmatter  # noqa

# ─────────────────────────────────────────────────────────────────────
# VERSION
# Bump VERSION whenever the SET of docs in the bundle materially changes
# (a doc added, removed, or category-shifted). Routine content edits to
# existing docs do not bump VERSION — they go out as the same version
# with a fresh CONTENT_DATE.
# ─────────────────────────────────────────────────────────────────────

VERSION = "3.0"
CONTENT_DATE = date.today().isoformat()

TOC_JSON = REPO / "0-MD" / "0-Documentation" / "TOC.json"

# Friendly labels + categories per slug. Slugs come from TOC.json (filename
# stems). Anything not listed here is skipped — explicit opt-in keeps the
# library curated rather than dumping every MD into the bundle.
SLUG_META = {
    # slug              : (label,             category)
    "auth-agent"        : ("Auth & Agent",    "Architecture"),
    "auth-agent-internal": ("Auth Internal",  "Architecture"),
    "agent-contract"    : ("Agent Contract",  "Architecture"),
    "board-hierarchy"   : ("Board Hierarchy", "Architecture"),
    "product-vision"    : ("Product Vision",  "Architecture"),
    "bootstrap"         : ("Bootstrap",       "Operations"),
    "deploy"            : ("Deploy",          "Operations"),
    "SYNC-ARCHITECTURE-PROPOSAL": ("GUESS (Sync)",       "Proposed"),
    "PROJECT-SCAFFOLD-PROPOSAL" : ("Project Scaffold",   "Proposed"),
    "BOARD-PURPOSE-AND-PACT-PROPOSAL": ("Board Pact",    "Proposed"),
    "CONTEXT-DRIFT-REFRESH-ENGINEER": ("Drift Refresh", "Proposed"),
    "product-brief"     : ("Product Brief",   "Product"),
    "tech-index"        : ("Tech Index",      "Product"),
    "cto-rationale"     : ("CTO Rationale",   "Product"),
}

# Tab order within each category — falls back to insertion order from TOC.
CATEGORY_ORDER = ["Architecture", "Operations", "Proposed", "Product"]


def load_docs_from_toc():
    """Read TOC.json and emit (slug, label, path, category) tuples in render order."""
    if not TOC_JSON.exists():
        print(f"ERROR: {TOC_JSON} not found. Run scripts/build_toc.py first.", file=sys.stderr)
        sys.exit(1)
    toc = json.loads(TOC_JSON.read_text(encoding="utf-8"))
    entries = []
    for entry in toc.get("documentation", []) + toc.get("proposed", []):
        # slug is filename stem, but the proposed/ files use UPPER-CASE filenames.
        slug = Path(entry["filename"]).stem
        meta = SLUG_META.get(slug)
        if not meta:
            continue  # not curated for the library
        label, category = meta
        entries.append((slug.lower().replace("_", "-"), label, entry["path"], category))
    # Sort by category order, then preserve discovery order within
    cat_index = {c: i for i, c in enumerate(CATEGORY_ORDER)}
    entries.sort(key=lambda e: (cat_index.get(e[3], 99),))
    return entries


DOCS = load_docs_from_toc()

# ─────────────────────────────────────────────────────────────────────
# Build
# ─────────────────────────────────────────────────────────────────────

LIB_DIR = REPO / "0-MD" / "library"
LIB_DIR.mkdir(parents=True, exist_ok=True)

CSS = """
:root{--bg:#0f1520;--surface:#141e32;--sub:#0f172a;--border:#334155;--text:#c8d0dc;--mute:#64748b;--strong:#e2e8f0;--accent:#a78bfa;--mono:Consolas,monospace}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:var(--bg);color:var(--text);font-size:14px;line-height:1.6}
header{padding:1.2rem 2rem;border-bottom:1px solid var(--border);background:var(--sub);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:1rem}
header h1{font-size:1.3rem;color:var(--strong)}
header .version{font-family:var(--mono);font-size:.65rem;color:var(--accent);background:rgba(167,139,250,.1);padding:.3rem .7rem;border-radius:4px;border:1px solid rgba(167,139,250,.3)}
header p{color:var(--mute);font-size:.75rem;margin-top:.3rem;width:100%}
.tabs-wrap{background:var(--sub);border-bottom:1px solid var(--border);padding:0 2rem}
.tab-cat{font-family:var(--mono);font-size:.55rem;text-transform:uppercase;letter-spacing:.08em;color:var(--mute);padding:.5rem 0 .2rem;display:block}
.tabs{display:flex;gap:0;flex-wrap:wrap}
.tab{background:none;border:none;color:var(--mute);font-family:var(--mono);font-size:.7rem;text-transform:uppercase;letter-spacing:.06em;padding:.7rem 1.2rem;cursor:pointer;border-bottom:2px solid transparent;transition:all .15s}
.tab:hover{color:var(--text)}
.tab.active{color:var(--accent);border-bottom-color:var(--accent)}
.pane{display:none;grid-template-columns:260px 1fr;gap:1.5rem;padding:2rem;max-width:1400px;margin:0 auto}
.pane.active{display:grid}
.toc{position:sticky;top:1rem;align-self:start;max-height:calc(100vh - 4rem);overflow-y:auto;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:1rem}
.toc ul{list-style:none}
.toc a{color:var(--mute);text-decoration:none;font-size:.72rem;display:block;padding:.25rem .5rem;border-left:2px solid transparent;border-radius:0 4px 4px 0;transition:all .15s}
.toc a:hover{color:var(--text);border-left-color:var(--accent);background:rgba(167,139,250,.08)}
.toc-l1>a{color:var(--strong);font-weight:600}
.toc-l2>a{padding-left:1rem}
.toc-l3>a{padding-left:1.6rem;font-size:.66rem}
main{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:2rem 2.5rem;min-width:0}
.meta{background:var(--sub);border:1px solid var(--border);border-radius:6px;padding:.6rem .9rem;margin-bottom:1.5rem;font-family:var(--mono);font-size:.65rem;color:var(--mute)}
.meta b{color:var(--text);text-transform:uppercase;letter-spacing:.05em;font-size:.55rem;margin-right:.3rem}
main h1{font-size:1.5rem;color:var(--strong);margin:1.5rem 0 .5rem;border-bottom:1px solid var(--border);padding-bottom:.4rem;scroll-margin-top:1rem}
main h1:first-of-type{margin-top:0}
main h2{font-size:1.1rem;color:var(--accent);margin:1.5rem 0 .5rem;scroll-margin-top:1rem}
main h3{font-size:.95rem;color:var(--strong);margin:1.2rem 0 .4rem;scroll-margin-top:1rem}
main h4{font-size:.85rem;color:var(--text);margin:1rem 0 .3rem}
main p{margin:.6rem 0;font-size:.85rem}
main ul,main ol{margin:.6rem 0 .6rem 1.5rem;font-size:.85rem}
main li{margin:.2rem 0}
main blockquote{border-left:3px solid var(--accent);padding:.6rem 1rem;margin:1rem 0;background:rgba(167,139,250,.08);border-radius:0 6px 6px 0;font-style:italic;font-size:.85rem}
main blockquote strong{color:var(--accent);font-style:normal}
main code{font-family:var(--mono);background:var(--sub);padding:1px 6px;border-radius:3px;font-size:.85em;color:var(--accent)}
main .code-block{background:var(--sub);border:1px solid var(--border);border-radius:8px;padding:.85rem 1rem;margin:1rem 0;overflow-x:auto;font-family:var(--mono);font-size:.72rem}
main .code-block code{background:none;color:var(--text);padding:0}
main hr{border:none;border-top:1px dashed var(--border);margin:2rem 0}
main a{color:var(--accent);text-decoration:none}
main a:hover{text-decoration:underline}
main strong{color:var(--strong)}
.table-wrap{overflow-x:auto;margin:1rem 0;border-radius:8px;border:1px solid var(--border)}
table{width:100%;border-collapse:collapse;font-size:.72rem}
th{background:var(--sub);color:var(--accent);padding:.5rem .7rem;font-family:var(--mono);font-size:.55rem;text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid var(--border);text-align:left}
td{padding:.4rem .7rem;border-bottom:1px solid var(--border)}
tr:last-child td{border-bottom:none}
tr:hover td{background:var(--sub)}
@media (max-width:1000px){.pane{grid-template-columns:1fr}.toc{position:relative;max-height:none}}
"""

def build_pane(slug, label, path, category, active):
    text = (REPO / path).read_text(encoding="utf-8")
    fm, body_md = parse_frontmatter(text)
    body = md_to_html(body_md)
    body = re.sub(r'id="([^"]+)"', lambda m: f'id="{slug}-{m.group(1)}"', body)
    body = re.sub(r'href="#([^"]+)"', lambda m: f'href="#{slug}-{m.group(1)}"', body)
    toc = build_toc(body)
    meta_items = [f"<b>{k}</b> {v}" for k, v in fm.items() if k in ("status", "version", "last_updated", "audience")]
    meta = f'<div class="meta">{" · ".join(meta_items)}</div>' if meta_items else ""
    cls = "pane active" if active else "pane"
    return (
        f'<button class="tab{" active" if active else ""}" data-cat="{category}" '
        f'onclick="show(\'{slug}\',this)">{label}</button>',
        f'<div class="{cls}" id="pane-{slug}"><aside class="toc">{toc}</aside><main>{meta}{body}</main></div>',
    )


def main():
    tab_groups = {}
    panes = []
    for i, (slug, label, path, category) in enumerate(DOCS):
        tab, pane = build_pane(slug, label, path, category, active=(i == 0))
        tab_groups.setdefault(category, []).append(tab)
        panes.append(pane)

    tabs_html = ""
    for cat in ("Architecture", "Operations", "Proposed", "Product"):
        if cat not in tab_groups:
            continue
        tabs_html += f'<span class="tab-cat">{cat}</span><div class="tabs">{"".join(tab_groups[cat])}</div>'

    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VibeForge+ Doc Library v{VERSION}</title>
<style>{CSS}</style></head><body>
<header>
  <div>
    <h1>VibeForge+ Doc Library</h1>
    <p>Portable single-file bundle · Architecture · Proposed · Product · {len(DOCS)} documents</p>
  </div>
  <div class="version">v{VERSION} · {CONTENT_DATE}</div>
</header>
<div class="tabs-wrap">{tabs_html}</div>
{"".join(panes)}
<script>
function show(slug, btn) {{
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.pane').forEach(p => p.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('pane-' + slug).classList.add('active');
  window.scrollTo(0, 0);
}}
</script></body></html>"""

    versioned = LIB_DIR / f"VIBEFORGE-DOC-LIBRARY-v{VERSION}.html"
    latest = LIB_DIR / "VIBEFORGE-DOC-LIBRARY.html"
    versioned.write_text(html, encoding="utf-8")
    latest.write_text(html, encoding="utf-8")

    # Manifest
    manifest = [
        "# VibeForge+ Doc Library — Manifest",
        "",
        f"**Latest version:** v{VERSION}  ",
        f"**Built:** {CONTENT_DATE}  ",
        f"**Doc count:** {len(DOCS)}",
        "",
        "## What this is",
        "",
        "A single, portable, versioned HTML bundle of every VibeForge+ architecture",
        "and product document that matters right now. Open `VIBEFORGE-DOC-LIBRARY.html`",
        "in any browser — no server, no internet, no dependencies.",
        "",
        "This library is dogfood of the documentation contract: **one location, one",
        "build, one artefact, versioned, with rationale embedded**. The build is",
        "deterministic — re-running `python scripts/build_doc_library.py` produces the",
        "same bytes from the same source MDs.",
        "",
        "## Versioning rules",
        "",
        "- **VERSION** in `scripts/build_doc_library.py` is bumped manually when the",
        "  *set* of docs in the bundle materially changes (a doc added, removed, or",
        "  pivoted in scope). Routine content edits to existing docs do **not** bump",
        "  the version — they go out as the same version with a fresh `CONTENT_DATE`.",
        "- **CONTENT_DATE** is auto-stamped on every build.",
        "- The versioned file (`VIBEFORGE-DOC-LIBRARY-v{N}.html`) is **immutable per",
        "  major change**. The `VIBEFORGE-DOC-LIBRARY.html` pointer always reflects",
        "  the latest build.",
        "- Old versioned files are kept in `0-MD/library/` as historical record.",
        "",
        "## What's in the bundle",
        "",
        "| Tab | Category | Source path |",
        "|---|---|---|",
    ]
    for slug, label, path, category in DOCS:
        manifest.append(f"| {label} | {category} | `{path}` |")

    manifest += [
        "",
        "## How to use",
        "",
        "- **Read locally:** double-click `VIBEFORGE-DOC-LIBRARY.html`.",
        "- **Share:** copy the single HTML file anywhere — email, USB, cloud drive, paste into chat.",
        "- **Pin a version:** copy `VIBEFORGE-DOC-LIBRARY-v{N}.html` for that exact moment in time.",
        "- **Rebuild after MD edits:** `python scripts/build_doc_library.py`.",
        "",
        "## Update triggers",
        "",
        "- Any doc in the DOCS list above is materially edited → rebuild (no version bump)",
        "- A doc is added or removed from the DOCS list → rebuild + version bump",
        "- A doc moves from `proposed/` to `public/` → rebuild + version bump + update tab category",
        "- A new architecture round produces a new doc → add to DOCS list, rebuild + version bump",
        "",
        "## Why this exists",
        "",
        "The documentation contract proposal (VF-230, PROJECT-SCAFFOLD-PROPOSAL.md)",
        "argues that every project should have a discoverable, versioned documentation",
        "library at a known location. This bundle is VibeForge+ practising what it",
        "preaches on its own architecture docs. If we cannot keep our own library alive,",
        "we have no business shipping the doc-contract feature.",
        "",
        f"## Build log (latest)",
        f"",
        f"- v{VERSION} ({CONTENT_DATE}): {len(DOCS)} documents bundled.",
    ]

    (LIB_DIR / "MANIFEST.md").write_text("\n".join(manifest), encoding="utf-8")

    print(f"Built v{VERSION} ({CONTENT_DATE})")
    print(f"  -> {versioned.relative_to(REPO)} ({len(html):,} bytes)")
    print(f"  -> {latest.relative_to(REPO)}")
    print(f"  -> {(LIB_DIR / 'MANIFEST.md').relative_to(REPO)}")
    print(f"  Tabs: {len(DOCS)}  ·  Categories: {len(tab_groups)}")


if __name__ == "__main__":
    main()
