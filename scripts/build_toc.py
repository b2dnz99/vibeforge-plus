"""
VibeForge+ TOC generator.

Walks the documentation tree, parses frontmatter, merges with the manual template
at 0-MD/0-Documentation/TOC.template.md, and emits three artefacts:

  0-MD/0-Documentation/TOC.md   ← human-readable, git-tracked. Links target .md sources, RELATIVE TO TOC.md location.
  0-MD/0-Documentation/TOC.html ← rendered, double-clickable, dark theme. Links target .html siblings, RELATIVE TO TOC.html location.
  0-MD/0-Documentation/TOC.json ← machine-readable for agents (GET /api/v2/projects/{slug}/toc)

Run from repo root:
  python scripts/build_toc.py             (renders every .md before building the TOC)
  python scripts/build_toc.py --no-render (skip render orchestration; only build TOC)

PIPELINE (load-bearing — see VF-364 + the index-renders-last principle):
  1. audit_audience_folders fail-fast (preserved public guard — frontmatter audience
     must match physical folder)
  2. walk all doc trees + collect entries
  3. render-orchestration pass: render every .md to its sibling .html via
     scripts/render_architecture_docs.py:render_doc (skipped under --no-render)
  4. emit TOC.json + TOC.md (links to .md sources, relative to TOC.md location;
     verify .md exists at emit time, [missing source] indicator + stderr WARN if not)
  5. emit TOC.html (links to .html siblings, relative to TOC.html location;
     verify .html exists at emit time, [NEEDS RENDER] indicator + stderr WARN if not)

OUTPUT IS NEVER REFUSED for missing link targets — visible indicator + stderr WARN
let the operator decide what to fix. (The audit_audience_folders fail-fast at the
top is a SEPARATE concern: data-integrity, predating the never-refuse principle,
load-bearing public guard.)

Deterministic: re-running with no changes produces identical bytes (modulo the
generated_date stamp). Sub-second (excluding the render pass — that scales with
doc count).

This is the canonical project index. Every other index/library/manifest reads
from TOC.json. Do not maintain a parallel list anywhere.
"""
import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
from render_architecture_docs import md_to_html, parse_frontmatter, render_doc  # noqa: E402

MD_DIR = REPO / "0-MD"
DOCS_DIR = MD_DIR / "0-Documentation"
GUIDES_DIR = DOCS_DIR / "guides"
PROPOSED_DIR = DOCS_DIR / "proposed"
TOOLKIT_DIR = REPO / "0-toolkit"
LIBRARY_DIR = MD_DIR / "library"
PROGRESS_DIR = MD_DIR / "progress"

AUDIENCE_ORDER = ["public", "internal", "confidential", "rescue", "archive", ""]

TEMPLATE = DOCS_DIR / "TOC.template.md"
OUT_MD = DOCS_DIR / "TOC.md"
OUT_HTML = DOCS_DIR / "TOC.html"
OUT_JSON = DOCS_DIR / "TOC.json"
TOC_DIR = DOCS_DIR  # TOC.md/.html/.json all live here; used for link relative-path math

TOC_VERSION = "4.0"


def file_hash(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:12]


def doc_entry(p: Path) -> dict:
    text = p.read_text(encoding="utf-8")
    fm, _body = parse_frontmatter(text)
    return {
        "slug": p.stem.lower().replace("_", "-"),
        "filename": p.name,
        "path": str(p.relative_to(REPO)).replace("\\", "/"),
        "title": fm.get("title", p.stem),
        "audience": fm.get("audience", "").lower(),
        "ip": fm.get("ip", "").lower(),
        "style": fm.get("style", "").lower(),
        "ip_first_dated": fm.get("ip_first_dated", ""),
        "ip_authors": fm.get("ip_authors", ""),
        "ip_disclosure_path": fm.get("ip_disclosure_path", ""),
        "ip_summary": fm.get("ip_summary", ""),
        "status": fm.get("status", ""),
        "version": fm.get("version", ""),
        "last_updated": fm.get("last_updated", ""),
        "purpose": fm.get("purpose", "") or fm.get("description", ""),
        "byte_count": p.stat().st_size,
        "content_hash": file_hash(p),
    }


def walk_md(d: Path) -> list:
    if not d.exists():
        return []
    # Walk top-level + audience subfolders (public/, internal/, confidential/, rescue/).
    # Skip _meta-mirror/, drafts/, archive/, guides/, proposed/ — those are walked separately
    # (guides via walk_md(GUIDES_DIR), proposed via walk_md(PROPOSED_DIR)).
    SKIP = {"_meta-mirror", "drafts", "archive", "guides", "proposed"}
    out = [doc_entry(p) for p in sorted(d.glob("*.md"))]
    for sub in sorted(d.iterdir()):
        if sub.is_dir() and sub.name not in SKIP and not sub.name.startswith("."):
            out.extend(doc_entry(p) for p in sorted(sub.glob("*.md")))
    return out


def audit_audience_folders(docs_dir: Path) -> list[str]:
    """Verify each doc's frontmatter audience matches its physical folder.
    Returns a list of error messages (empty = clean)."""
    errors = []
    valid_audiences = {"public", "internal", "confidential", "rescue", "archive"}
    for sub in sorted(docs_dir.iterdir()):
        if not sub.is_dir() or sub.name in {"_meta-mirror", "guides"} or sub.name.startswith("."):
            continue
        if sub.name not in valid_audiences:
            continue
        expected = sub.name
        for md_file in sorted(sub.glob("*.md")):
            try:
                fm, _ = parse_frontmatter(md_file.read_text(encoding="utf-8"))
                actual = (fm.get("audience") or "").strip().lower()
                if actual != expected:
                    errors.append(
                        f"AUDIENCE MISMATCH: {md_file.relative_to(REPO)} "
                        f"is in '{expected}/' but frontmatter says audience='{actual}'"
                    )
            except Exception as e:
                errors.append(f"PARSE ERROR: {md_file.relative_to(REPO)}: {e}")
    return errors


def walk_toolkit(d: Path) -> list:
    tools_dir = d / "tools"
    if not tools_dir.exists():
        return []
    return [doc_entry(p) for p in sorted(tools_dir.glob("*.md"))]


def walk_library(d: Path) -> list:
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.html")):
        out.append({
            "filename": p.name,
            "path": str(p.relative_to(REPO)).replace("\\", "/"),
            "byte_count": p.stat().st_size,
            "content_hash": file_hash(p),
        })
    return out


def walk_progress(d: Path) -> list:
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.md")):
        out.append({
            "filename": p.name,
            "path": str(p.relative_to(REPO)).replace("\\", "/"),
            "byte_count": p.stat().st_size,
            "last_updated": datetime.fromtimestamp(p.stat().st_mtime).date().isoformat(),
        })
    return out


def _toc_relative_path(stored_path: str, target_format: str) -> str:
    """Convert a stored project-root-relative path (e.g. '0-MD/0-Documentation/foo.md',
    '0-MD/library/foo.html', '0-toolkit/tools/foo.md') to a path relative to TOC.md's
    location (TOC_DIR = 0-MD/0-Documentation/), with optional .md→.html sibling swap.
    Forward-slash output regardless of platform.
    """
    abs_target = REPO / stored_path
    if target_format == "html" and abs_target.suffix == ".md":
        abs_target = abs_target.with_suffix(".html")
    return os.path.relpath(abs_target, TOC_DIR).replace("\\", "/")


def _verify_target(stored_path: str, target_format: str) -> bool:
    """Check whether the target file (.md or .html sibling) actually exists on disk."""
    abs_target = REPO / stored_path
    if target_format == "html" and abs_target.suffix == ".md":
        abs_target = abs_target.with_suffix(".html")
    return abs_target.exists()


def _missing_indicator(target_format: str, link_format_label: str) -> str:
    """Visible indicator appended after a missing-target link.
    link_format_label is the rendering layer ('md' or 'html'); used in stderr.
    """
    if target_format == "html":
        return " `[NEEDS RENDER]`"
    return " `[missing source]`"


def render_doc_table(entries: list, title: str, empty_msg: str, target_format: str = "md") -> str:
    """Build a doc table. target_format='md' for TOC.md (.md links), 'html' for TOC.html (.html sibling links).
    Each link's target is verified to exist on disk; missing → stderr WARN + visible indicator on the row.
    Output is never refused.
    """
    out = [f"## {title}", ""]
    if not entries:
        out.append(f"_{empty_msg}_")
        return "\n".join(out)
    # Group by audience tier
    by_tier = {}
    for e in entries:
        by_tier.setdefault(e.get("audience", "") or "(untagged)", []).append(e)
    for tier in AUDIENCE_ORDER + sorted(set(by_tier) - set(AUDIENCE_ORDER) - {""}):
        bucket = by_tier.get(tier or "(untagged)", []) if tier else by_tier.get("", [])
        if not bucket:
            continue
        tier_label = tier or "(untagged)"
        out.append(f"### {tier_label}")
        out.append("")
        out.append("| Doc | IP | Style | Status | Version | Updated |")
        out.append("|-----|----|-------|--------|---------|---------|")
        for e in bucket:
            link_path = _toc_relative_path(e["path"], target_format)
            indicator = ""
            if not _verify_target(e["path"], target_format):
                sys.stderr.write(
                    f"[build_toc TOC.{target_format}] WARN missing {target_format} target: {link_path}\n"
                )
                indicator = _missing_indicator(target_format, target_format)
            link = f"[{e['title']}]({link_path}){indicator}"
            out.append(
                f"| {link} | {e.get('ip','-') or '-'} | {e.get('style','-') or '-'} "
                f"| {e.get('status','-') or '-'} | {e.get('version','-') or '-'} "
                f"| {e.get('last_updated','-') or '-'} |"
            )
        out.append("")
    return "\n".join(out)


def render_ip_register(all_entries: list, target_format: str = "md") -> str:
    """IP register section. Links to source docs in the chosen target_format."""
    novel = [e for e in all_entries if e.get("ip") == "novel"]
    if not novel:
        return "## Inventions (IP register)\n\n_No novel paradigms tagged yet._"
    novel.sort(key=lambda e: (e.get("ip_first_dated", ""), e.get("title", "")))
    out = [
        "## Inventions (IP register)",
        "",
        "Auto-generated from `ip: novel` frontmatter. This is the canonical "
        "list of paradigms originating in this project. Each entry includes a "
        "first-dated stamp for prior-art purposes and a one-paragraph summary "
        "in plain English.",
        "",
    ]
    for e in novel:
        out.append(f"### {e['title']}")
        out.append("")
        link_path = _toc_relative_path(e["path"], target_format)
        indicator = ""
        if not _verify_target(e["path"], target_format):
            sys.stderr.write(
                f"[build_toc TOC.{target_format}] WARN missing {target_format} target (IP): {link_path}\n"
            )
            indicator = _missing_indicator(target_format, target_format)
        out.append(f"**Path:** [`{link_path}`]({link_path}){indicator}  ")
        out.append(f"**First dated:** {e.get('ip_first_dated','-') or '-'}  ")
        out.append(f"**Authors:** {e.get('ip_authors','-') or '-'}  ")
        out.append(f"**Disclosure:** {e.get('ip_disclosure_path','-') or '-'}  ")
        out.append("")
        summary = (e.get("ip_summary") or "_no summary written yet — add ip_summary to frontmatter._").strip()
        out.append(f"> {summary}")
        out.append("")
    return "\n".join(out)


def render_library_table(entries: list, title: str) -> str:
    """Library bundles are pre-built .html files. Link path computed relative to
    TOC.md location (TOC_DIR), so '0-MD/library/foo.html' becomes '../library/foo.html'."""
    out = [f"## {title}", ""]
    if not entries:
        out.append("_No library bundles built yet. Run the relevant build script._")
        return "\n".join(out)
    out.append("| Bundle | Size | Hash |")
    out.append("|--------|------|------|")
    for e in entries:
        link_path = _toc_relative_path(e["path"], "html")
        indicator = "" if _verify_target(e["path"], "html") else " `[missing bundle]`"
        if indicator:
            sys.stderr.write(f"[build_toc library] WARN missing bundle: {link_path}\n")
        link = f"[{e['filename']}]({link_path}){indicator}"
        out.append(f"| {link} | {e['byte_count']:,} bytes | `{e['content_hash']}` |")
    return "\n".join(out)


def render_progress_table(entries: list, title: str, target_format: str = "md") -> str:
    """Progress entries are .md files; in TOC.html we link to the .html sibling
    if rendered, otherwise to the .md (no indicator — progress docs may not be
    rendered, that's by design). Paths are TOC-relative."""
    out = [f"## {title}", ""]
    if not entries:
        out.append("_No progress files._")
        return "\n".join(out)
    out.append("| File | Size | Modified |")
    out.append("|------|------|----------|")
    for e in entries:
        # For html target, prefer .html sibling if rendered; fall back to .md silently.
        fmt = target_format
        if target_format == "html" and not _verify_target(e["path"], "html"):
            fmt = "md"
        link_path = _toc_relative_path(e["path"], fmt)
        link = f"[{e['filename']}]({link_path})"
        out.append(f"| {link} | {e['byte_count']:,} bytes | {e['last_updated']} |")
    return "\n".join(out)


def orchestrate_renders(entries: list) -> tuple[int, int]:
    """Wave 2.0.5 principle ported (VF-364): render every .md the TOC will index
    BEFORE emitting the TOC outputs. Returns (rendered_ok_count, failed_count).
    Render failures surface via stderr + leave the .html sibling missing → caught
    by the TOC.html verify-and-warn pass downstream.
    """
    ok = 0
    fail = 0
    for e in entries:
        path = e.get("path") or ""
        if not path.endswith(".md"):
            continue
        md_abs = REPO / path
        if not md_abs.exists():
            sys.stderr.write(f"[build_toc render] SKIP {path} — source missing\n")
            fail += 1
            continue
        html_abs = md_abs.with_suffix(".html")
        try:
            render_doc(md_abs, html_abs)
            ok += 1
        except Exception as ex:
            sys.stderr.write(f"[build_toc render] FAIL {path} — {ex!r}\n")
            fail += 1
    return ok, fail


def _build_template(template_src: str, today: str, build_id: str,
                    docs: list, guides: list, proposed: list, toolkit: list,
                    library: list, progress: list, target_format: str) -> str:
    """Substitute {{generated_*}} slots with rendered tables in the chosen
    target_format ('md' for TOC.md, 'html' for TOC.html). The template source
    itself is markdown either way; format determines link targets only."""
    t = template_src
    t = t.replace("{{generated_date}}", today)
    t = t.replace("{{toc_version}}", TOC_VERSION)
    t = t.replace("{{build_id}}", build_id)
    t = t.replace(
        "{{generated_documentation_index}}",
        render_doc_table(docs, "Documentation (active)", "No docs in 0-Documentation/ yet.", target_format=target_format),
    )
    t = t.replace(
        "{{generated_guides_index}}",
        render_doc_table(guides, "Guides (user-facing how-to)", "No guides yet — agent will propose at first scaffold.", target_format=target_format),
    )
    t = t.replace(
        "{{generated_ip_register}}",
        render_ip_register(docs + proposed + guides, target_format=target_format),
    )
    t = t.replace(
        "{{generated_proposed_index}}",
        render_doc_table(proposed, "Proposed (captured thinking, not yet active)", "No proposed docs.", target_format=target_format),
    )
    t = t.replace(
        "{{generated_toolkit_index}}",
        render_doc_table(toolkit, "Toolkit (operational tools)", "No toolkit docs yet — see scripts/build_toc.py and 0-toolkit/.", target_format=target_format),
    )
    t = t.replace(
        "{{generated_library_index}}",
        render_library_table(library, "Library bundles (built artefacts)"),
    )
    t = t.replace(
        "{{generated_progress_index}}",
        render_progress_table(progress, "Session continuity (handover artefacts)", target_format=target_format),
    )
    return t


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build VibeForge+ TOC. Renders every indexed .md to its sibling .html "
                    "first (unless --no-render), then emits TOC.md (.md links) + TOC.html "
                    "(.html sibling links) + TOC.json. Per VF-364 (port of wave-2.0.5 scaffold "
                    "principles): each output verifies its link targets at emit time, surfaces "
                    "missing ones via stderr WARN + visible indicator. Output is never refused."
    )
    parser.add_argument("--no-render", action="store_true",
                        help="Skip the render-orchestration pass; only build TOC outputs. "
                             "Use only if you've just rendered separately and want to "
                             "re-emit the index. The TOC.html verify pass will still warn "
                             "about missing .html siblings.")
    args = parser.parse_args()

    # Audit physical folder vs frontmatter audience BEFORE walking — fail fast on mismatch.
    # PRESERVED public guard (load-bearing, predates the wave-2.0.5 never-refuse principle;
    # data-integrity concern, separate from link-validity).
    audit_errors = audit_audience_folders(DOCS_DIR)
    if audit_errors:
        print("ERROR: doc audience folders out of sync with frontmatter:", file=sys.stderr)
        for err in audit_errors:
            print(f"  {err}", file=sys.stderr)
        print("Fix the frontmatter or move the file. Build aborted.", file=sys.stderr)
        sys.exit(1)

    docs = walk_md(DOCS_DIR)
    guides = walk_md(GUIDES_DIR)
    proposed = walk_md(PROPOSED_DIR)
    toolkit = walk_toolkit(TOOLKIT_DIR)
    library = walk_library(LIBRARY_DIR)
    progress = walk_progress(PROGRESS_DIR)

    today = date.today().isoformat()
    build_id = hashlib.sha256(
        f"{today}-{len(docs)}-{len(proposed)}-{len(toolkit)}".encode()
    ).hexdigest()[:8]

    # VF-364: render-orchestration pass. Every indexed .md gets rendered to its
    # sibling .html so TOC.html links land on real files. Renders for docs +
    # guides + proposed + toolkit (NOT library — those are pre-built .html;
    # NOT progress — progress docs are intentionally unrendered most of the time,
    # render_doc would still work but adds noise).
    rendered_ok = 0
    rendered_fail = 0
    if not args.no_render:
        renderable = docs + guides + proposed + toolkit
        rendered_ok, rendered_fail = orchestrate_renders(renderable)

    # JSON manifest — the canonical machine-readable index (unchanged shape).
    manifest = {
        "toc_version": TOC_VERSION,
        "generated_date": today,
        "build_id": build_id,
        "documentation": docs,
        "guides": guides,
        "proposed": proposed,
        "toolkit": toolkit,
        "library": library,
        "progress": progress,
        "counts": {
            "documentation": len(docs),
            "guides": len(guides),
            "proposed": len(proposed),
            "toolkit": len(toolkit),
            "library": len(library),
            "progress": len(progress),
        },
    }
    OUT_JSON.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    template_src = TEMPLATE.read_text(encoding="utf-8") if TEMPLATE.exists() else ""

    # TOC.md — links target .md sources (relative to TOC.md location at TOC_DIR).
    md_built = _build_template(template_src, today, build_id, docs, guides, proposed, toolkit, library, progress, target_format="md")
    OUT_MD.write_text(md_built, encoding="utf-8")

    # TOC.html — links target .html siblings (relative to TOC.html location at TOC_DIR).
    # VF-364 extension (PK request): route TOC.html through the SAME render_doc pipeline
    # every other VF+ doc uses, so it inherits the full MC theme + sidebar + audience pill
    # ('INDEX') automatically. Stops hand-rolling TOC.html's <head><body> with a divergent
    # minimal stylesheet; single source of theme truth lives in render_architecture_docs.TEMPLATE.
    #
    # Mechanism: build the .html-link variant of the merged TOC content as a temp .md
    # (with synthesized frontmatter so render_doc picks up audience='index' + title), then
    # call render_doc(temp_md, OUT_HTML), then clean up.
    html_built_body = _build_template(template_src, today, build_id, docs, guides, proposed, toolkit, library, progress, target_format="html")
    html_md_with_frontmatter = (
        "---\n"
        "title: VibeForge+ Documentation TOC\n"
        "audience: index\n"
        "status: generated\n"
        f"version: {TOC_VERSION}\n"
        f"last_updated: {today}\n"
        f"build_id: {build_id}\n"
        "---\n\n"
        + html_built_body
    )
    temp_html_md = TOC_DIR / ".toc_html_source.md"
    try:
        temp_html_md.write_text(html_md_with_frontmatter, encoding="utf-8")
        render_doc(temp_html_md, OUT_HTML)
    finally:
        if temp_html_md.exists():
            temp_html_md.unlink()

    print(f"TOC v{TOC_VERSION} build {build_id} ({today})")
    print(f"  -> {OUT_JSON.relative_to(REPO)}")
    print(f"  -> {OUT_MD.relative_to(REPO)}  (.md links, TOC-relative)")
    print(f"  -> {OUT_HTML.relative_to(REPO)}  (.html links, TOC-relative)")
    print(
        f"  Counts: {len(docs)} docs · {len(guides)} guides · {len(proposed)} proposed · "
        f"{len(toolkit)} toolkit · {len(library)} library · {len(progress)} progress"
    )
    if not args.no_render:
        print(
            f"  Rendered: {rendered_ok} ok"
            + (f" · {rendered_fail} FAILED — see stderr" if rendered_fail else "")
        )


if __name__ == "__main__":
    main()
