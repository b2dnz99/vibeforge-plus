"""
Project documentation TOC builder — board-shipped default.

Scans the doc classes under `0-MD/0-Documentation/` and generates the
canonical `0-MD/0-Documentation/TOC.md` (plus `.html` and `.json` siblings)
from filesystem state:

  - `0-MD/0-Documentation/internal/**/*.md`  — contributor-facing docs (default)
  - `0-MD/0-Documentation/proposed/**/*.md`  — captured-thinking, pre-canonical (default)
  - `0-MD/0-Documentation/public/**/*.md`    — customer / outside-reader docs (CREATED ON DEMAND)

`internal/` and `proposed/` are the two default classes. `public/` is
deliberately NOT pre-created — most projects don't have a public technical
readership, and pre-creating the directory invites unnecessary content +
maintenance debt. SCAN_DIRS still LISTS `public/` so that if a customer
creates the directory on demand (graduating a doc into it for an outside
reader who specifically asked), the TOC auto-includes it without requiring
a code change. Empty classes (including `public/` for projects that never
need it) are omitted from the generated TOC entirely.

Each present class is listed as its own section in the generated TOC so the
reader can tell at a glance who a doc speaks to and whether the thinking is
settled or still in proposal form. See `README.md` (Doc classes section)
for the full lifecycle (proposed → graduate to internal; public on demand;
archived/ subdir preserves shelved docs without polluting the TOC).

EXCLUSIONS (these never appear in the TOC):

  - `**/archived/**` under any class — kept on disk for history; the live
    TOC only shows docs that are actively maintained.
  - `.scratch/` at the project root — gitignored ephemera; never docs.

PIPELINE ORDERING (load-bearing convention — see also "render-last + verify"
discipline). This builder is the LAST step of the doc pipeline:

  1. Renders every `.md` it indexes via `vf_render.py` (sibling import) so
     the `.html` outputs the TOC links to are guaranteed fresh on disk.
  2. Builds `TOC.md` linking to `.md` source files (verified to exist).
  3. Builds `TOC.html` linking to `.html` rendered siblings (verified to
     exist after the render pass). Each output's links are RELATIVE TO THAT
     OUTPUT'S OWN LOCATION, so they work whether you open the file from the
     project root, in a markdown viewer, or in a browser.

Per-output dependency contract:

  - `TOC.md` indexes `.md` source files. Link targets: `.md`, relative to
    `0-MD/0-Documentation/` (TOC.md's location).
  - `TOC.html` indexes `.html` rendered siblings. Link targets: `.html`,
    relative to `0-MD/0-Documentation/` (TOC.html's location).
  - Either output verifies its link targets at emit time. Missing target:
    stderr warning + the row is emitted with `[missing render]` /
    `[missing source]` indicator. The output is never refused; the index
    is never silent about gaps; you decide what to fix.

USAGE (from the project root, by default):
  python 0-MD/.tools/vf_toc.py
  python 0-MD/.tools/vf_toc.py --root /path/to/project
  python 0-MD/.tools/vf_toc.py --no-render    # skip render orchestration

ADAPT VOICE: this layout is a sensible default, not a contract. If your
project already organises docs differently, edit `SCAN_DIRS` below to match
your reality, or replace this script entirely with a TOC builder that
matches your toolchain. ASK THE HUMAN before re-organising an existing
doc tree to fit this default — re-homing en-masse is a move that wants
explicit human sign-off, not autopilot.

EDIT/REPLACE freely — this is a default. Your contract may name `vf_toc.py`
as MANDATORY (per the OUR-block Render & TOC Discipline), but the
*implementation* is yours. Swap the directory layout, swap the output
formats, swap the entire TOC builder if you have a preferred toolchain.
Just keep the entry-point command working AND preserve the load-bearing
conventions: render before index, verify each link target, fail gracefully
with a clear message rather than producing a TOC that points at 404s.

Outputs are deterministic — same input filesystem state always produces the
same TOC files. Safe to run on every commit; safe to run when nothing
changed.

The HTML output uses the same `template.html` as `vf_render.py` so the TOC
page matches the doc page styling (full MC theme).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

# Inline imports from sibling vf_render.py — works regardless of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from vf_render import build_toc, md_to_html, parse_frontmatter, render as render_doc  # noqa: E402


TOC_VERSION = "1.2"

# Doc classes scanned by the TOC, in display order. Each entry is
# (subdir name under 0-MD/0-Documentation/, section heading text).
# Edit / extend / reorder to match your project. ASK THE HUMAN if unsure.
SCAN_DIRS = [
    ("internal", "Internal (contributor-facing)"),
    ("public",   "Public (customer / outside-reader-facing)"),
    ("proposed", "Proposed (captured thinking, pre-canonical)"),
]

# Path components that exclude a doc from the TOC even if it lives under
# a scanned class. `archived` is the canonical "shelved without graduation"
# bucket — kept on disk for history but out of the live index.
EXCLUDED_PATH_PARTS = {"archived"}


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def doc_entry(path: Path, doc_root: Path) -> dict[str, object]:
    """Returns a doc record. Path is RELATIVE TO THE TOC LOCATION (doc_root),
    so links work directly from TOC.md / TOC.html without any prefixing.
    """
    text = path.read_text(encoding="utf-8")
    frontmatter, body = parse_frontmatter(text)
    title = frontmatter.get("title")
    if not title:
        for line in body.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
    title = title or path.stem.replace("-", " ").title()
    rel_md = path.relative_to(doc_root).as_posix()
    return {
        "title": title,
        "path_md": rel_md,                      # relative to doc_root (= TOC location)
        "path_html": rel_md[:-3] + ".html" if rel_md.endswith(".md") else rel_md,
        "audience": frontmatter.get("audience", ""),
        "status": frontmatter.get("status", ""),
        "version": frontmatter.get("version", ""),
        "byte_count": path.stat().st_size,
        "content_hash": file_hash(path),
    }


def collect_docs(class_root: Path, doc_root: Path) -> list[dict[str, object]]:
    if not class_root.exists():
        return []
    out: list[dict[str, object]] = []
    for p in sorted(class_root.rglob("*.md")):
        if any(part in EXCLUDED_PATH_PARTS for part in p.relative_to(class_root).parts):
            continue
        out.append(doc_entry(p, doc_root))
    return out


def collect_progress(progress_root: Path, doc_root: Path) -> list[dict[str, object]]:
    """Session-handover artefacts at 0-MD/progress/ (peer to 0-Documentation/).
    Categorically separate from documentation — listed in their own TOC section
    per the bundled README. Path stored relative to the TOC's location, which
    means the link starts with `../` (escapes 0-Documentation/ to reach progress/).
    Frontmatter is optional on handovers; missing fields render as '-'.
    """
    if not progress_root.exists():
        return []
    out: list[dict[str, object]] = []
    for p in sorted(progress_root.glob("*.md")):
        text = p.read_text(encoding="utf-8")
        frontmatter, body = parse_frontmatter(text)
        title = frontmatter.get("title")
        if not title:
            for line in body.splitlines():
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
        title = title or p.stem.replace("-", " ").title()
        # Path relative to doc_root (= TOC location). Goes up one level to escape
        # 0-Documentation/, then into progress/. Forward-slash for URL safety.
        rel_md = ("../" + p.relative_to(progress_root.parent).as_posix())
        out.append({
            "title": title,
            "path_md": rel_md,
            "path_html": rel_md[:-3] + ".html" if rel_md.endswith(".md") else rel_md,
            "audience": frontmatter.get("audience", "session"),
            "status": frontmatter.get("status", "handover"),
            "version": frontmatter.get("version", ""),
            "byte_count": p.stat().st_size,
            "content_hash": file_hash(p),
        })
    return out


def render_pass(docs: list[dict[str, object]], doc_root: Path, project_root: Path) -> None:
    """Wave 2.0.5: render-orchestration step. Every .md the TOC will index
    gets rendered to its sibling .html FIRST so the TOC.html links land on
    real files. Render failures are surfaced via stderr + the doc record's
    `render_ok` flag (consumed by build_html for the [missing render]
    indicator). The TOC build is never refused — the operator decides what
    to fix.
    """
    for doc in docs:
        src_md = doc_root / str(doc["path_md"])
        if not src_md.exists():
            doc["render_ok"] = False
            doc["render_err"] = "source .md missing at render time"
            sys.stderr.write(f"[vf_toc render] SKIP {doc['path_md']} — source missing\n")
            continue
        try:
            render_doc(src_md, project_root)
            doc["render_ok"] = True
        except Exception as e:
            doc["render_ok"] = False
            doc["render_err"] = repr(e)
            sys.stderr.write(f"[vf_toc render] FAIL {doc['path_md']} — {e!r}\n")


def _missing_md(doc: dict[str, object], doc_root: Path) -> bool:
    return not (doc_root / str(doc["path_md"])).exists()


def _missing_html(doc: dict[str, object], doc_root: Path) -> bool:
    return not (doc_root / str(doc["path_html"])).exists()


def build_markdown(sections: list[tuple[str, list[dict[str, object]]]],
                   doc_root: Path) -> str:
    """TOC.md — links to .md source files (relative to TOC.md's own location).
    Verifies each .md target exists; warns + adds [missing source] indicator
    when one doesn't. Empty classes are omitted entirely so minimal projects
    don't get ceremony for empty buckets.
    """
    lines = [
        "# Documentation TOC",
        "",
        f"Generated: {date.today().isoformat()}  ",
        f"TOC version: {TOC_VERSION}",
        "",
        "_Links target the **source `.md`** files, relative to this TOC's location. "
        "See `TOC.html` for the rendered-HTML browse view (links target `.html` siblings)._",
        "",
    ]
    if not any(docs for _, docs in sections):
        lines.extend([
            "_No docs yet. Add a markdown file under "
            "`0-MD/0-Documentation/internal/`, `public/`, or `proposed/` "
            "and re-run `python 0-MD/.tools/vf_toc.py`._",
            "",
        ])
        return "\n".join(lines)

    for heading, docs in sections:
        if not docs:
            continue
        lines.extend([
            f"## {heading}",
            "",
            "| Doc | Audience | Status | Version |",
            "|-----|----------|--------|---------|",
        ])
        for doc in docs:
            indicator = ""
            if _missing_md(doc, doc_root):
                indicator = " `[missing source]`"
                sys.stderr.write(f"[vf_toc TOC.md] WARN missing source: {doc['path_md']}\n")
            lines.append(
                f"| [{doc['title']}]({doc['path_md']}){indicator} | "
                f"{doc['audience'] or '-'} | "
                f"{doc['status'] or '-'} | "
                f"{doc['version'] or '-'} |"
            )
        lines.append("")
    return "\n".join(lines)


def build_html(sections: list[tuple[str, list[dict[str, object]]]],
               template_path: Path, doc_root: Path) -> str:
    """TOC.html — links to .html rendered siblings (relative to TOC.html's
    own location). Verifies each .html target exists; warns + adds
    [NEEDS RENDER] indicator when one doesn't. Browser users clicking
    through never hit a 404 silently — the missing state is visible.
    """
    if not template_path.exists():
        raise SystemExit(
            f"Template not found at {template_path} — restore from board "
            "scaffold (GET /api/v2/onboard/scaffold) or your own version."
        )

    # Build an HTML body inline so we can mark [NEEDS RENDER] as styled spans
    # rather than markdown-table cell text (which would be visually identical
    # to the row content).
    body_parts: list[str] = [
        '<h1 id="documentation-toc">Documentation TOC</h1>',
        f'<p class="toc-meta">Generated: {date.today().isoformat()} '
        f'· TOC version: {TOC_VERSION}</p>',
        '<p class="toc-meta-note">Links target the rendered <code>.html</code> '
        'siblings, relative to this page\'s location. See <a href="TOC.md">'
        'TOC.md</a> for the markdown-source view.</p>',
    ]

    if not any(docs for _, docs in sections):
        body_parts.append(
            '<p class="toc-empty">No docs yet. Add a markdown file under '
            '<code>0-MD/0-Documentation/internal/</code>, <code>public/</code>, '
            'or <code>proposed/</code> and re-run '
            '<code>python 0-MD/.tools/vf_toc.py</code>.</p>'
        )
    else:
        for heading, docs in sections:
            if not docs:
                continue
            slug = heading.lower().replace(" ", "-").replace("(", "").replace(")", "").replace("/", "-")
            body_parts.append(f'<h2 id="{slug}">{heading}</h2>')
            body_parts.append('<div class="table-wrap"><table>')
            body_parts.append(
                '<thead><tr>'
                '<th style="text-align:left">Doc</th>'
                '<th style="text-align:left">Audience</th>'
                '<th style="text-align:left">Status</th>'
                '<th style="text-align:left">Version</th>'
                '</tr></thead><tbody>'
            )
            for doc in docs:
                missing = _missing_html(doc, doc_root)
                if missing:
                    sys.stderr.write(
                        f"[vf_toc TOC.html] WARN missing render: {doc['path_html']}\n"
                    )
                    indicator = ' <span class="toc-missing">[NEEDS RENDER]</span>'
                    href = doc["path_html"]   # link still emitted; user can see the gap
                else:
                    indicator = ""
                    href = doc["path_html"]
                body_parts.append(
                    f'<tr>'
                    f'<td style="text-align:left"><a href="{href}">{doc["title"]}</a>{indicator}</td>'
                    f'<td style="text-align:left">{doc["audience"] or "-"}</td>'
                    f'<td style="text-align:left">{doc["status"] or "-"}</td>'
                    f'<td style="text-align:left">{doc["version"] or "-"}</td>'
                    f'</tr>'
                )
            body_parts.append('</tbody></table></div>')

    body = "\n".join(body_parts)
    toc = build_toc(body)
    return (
        template_path.read_text(encoding="utf-8")
        .replace("{title}", "Documentation TOC")
        .replace("{brand}", "Project Docs")
        .replace("{path}", "0-MD/0-Documentation/TOC.html")
        .replace("{audience_class}", "index")
        .replace("{audience_label}", "Index")
        .replace("{audience_short}", "INDEX")
        .replace("{toc}", toc)
        .replace("{meta_dl}", '<div><dt>type</dt><dd>Generated index</dd></div>')
        .replace("{body}", body)
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build project documentation TOC. Renders every "
                    "indexed .md to its .html sibling first (unless --no-render), "
                    "then emits TOC.md + TOC.html + TOC.json with verified links."
    )
    parser.add_argument("--root", help="Project root override (default: Path.cwd())", default=None)
    parser.add_argument("--no-render", action="store_true",
                        help="Skip the render-orchestration pass; only build the TOC. "
                             "Use only if you've just rendered separately and want "
                             "to re-emit the index.")
    args = parser.parse_args()
    project_root = Path(args.root).resolve() if args.root else Path.cwd().resolve()
    if not project_root.exists():
        raise SystemExit(f"Project root does not exist: {project_root}")
    doc_root = project_root / "0-MD" / "0-Documentation"
    out_md = doc_root / "TOC.md"
    out_html = doc_root / "TOC.html"
    out_json = doc_root / "TOC.json"
    template_path = project_root / "0-MD" / ".tools" / "template.html"

    doc_root.mkdir(parents=True, exist_ok=True)

    sections: list[tuple[str, list[dict[str, object]]]] = []
    json_payload: dict[str, object] = {
        "toc_version": TOC_VERSION,
        "generated_date": date.today().isoformat(),
        "classes": {},
    }
    summary_parts: list[str] = []
    all_docs: list[dict[str, object]] = []
    for subdir, heading in SCAN_DIRS:
        docs = collect_docs(doc_root / subdir, doc_root)
        sections.append((heading, docs))
        json_payload["classes"][subdir] = docs  # type: ignore[index]
        all_docs.extend(docs)
        if docs:
            summary_parts.append(f"{len(docs)} {subdir}")

    # Session-handover artefacts at 0-MD/progress/ (peer to 0-Documentation/).
    # Listed in dedicated TOC section per bundled README — visible but
    # categorically separate from durable documentation. Added to all_docs so
    # render_pass picks them up (otherwise TOC.html would show [NEEDS RENDER]
    # for every handover, which is loud false-alarm).
    progress_root = project_root / "0-MD" / "progress"
    progress = collect_progress(progress_root, doc_root)
    sections.append(("Session continuity (handover artefacts)", progress))
    json_payload["classes"]["progress"] = progress  # type: ignore[index]
    all_docs.extend(progress)
    if progress:
        summary_parts.append(f"{len(progress)} progress")

    # Wave 2.0.5: render-orchestration pass — every indexed .md is rendered
    # to its sibling .html BEFORE the TOC builds, so TOC.html links land on
    # real files. Skip with --no-render only if you have a separate render
    # workflow.
    if not args.no_render:
        render_pass(all_docs, doc_root, project_root)
    else:
        for d in all_docs:
            d["render_ok"] = None  # unknown; verification below catches missing

    markdown = build_markdown(sections, doc_root)
    out_md.write_text(markdown, encoding="utf-8")
    out_html.write_text(build_html(sections, template_path, doc_root), encoding="utf-8")
    out_json.write_text(json.dumps(json_payload, indent=2), encoding="utf-8")

    summary = " + ".join(summary_parts) if summary_parts else "0 docs"
    rendered_count = sum(1 for d in all_docs if d.get("render_ok") is True)
    failed_count = sum(1 for d in all_docs if d.get("render_ok") is False)
    render_status = (
        f" · rendered {rendered_count}/{len(all_docs)}"
        + (f" ({failed_count} FAILED — see stderr above)" if failed_count else "")
        if not args.no_render and all_docs else ""
    )
    print(f"TOC {TOC_VERSION}: {summary}{render_status} -> "
          f"{out_md.relative_to(project_root)} + .html + .json")


if __name__ == "__main__":
    main()
