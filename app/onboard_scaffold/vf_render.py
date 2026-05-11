"""
Project documentation renderer — board-shipped default.

Renders one Markdown file to a sibling HTML file using `template.html` from
the same directory as the styled shell. Full MC theme (violet accent,
Plus Jakarta Sans + JetBrains Mono fonts, sticky topbar with neon glow,
sidebar TOC, doc-meta card with violet stripe).

USAGE (from the project root, by default):
  python 0-MD/.tools/vf_render.py 0-MD/0-Documentation/internal/some-doc.md
  python 0-MD/.tools/vf_render.py --root /path/to/project 0-MD/some.md

Supported Markdown (see `0-MD/.tools/README.md` for full style guide + ASCII
diagram patterns):
  - Frontmatter (YAML between --- fences; stripped from output, surfaced in
    the doc-meta card)
  - Headings (#-######, anchored by slug for TOC linkage)
  - Paragraphs (multi-line joined; blank line breaks)
  - Lists (- / * unordered; 1. ordered)
  - Tables (Markdown pipe tables with alignment)
  - Blockquotes (> prefix; multi-line collapsed)
  - Fenced code blocks (``` with optional lang; preserves whitespace)
  - Horizontal rule (---)
  - Inline: `code`, **bold**, *italic*, [text](url)

DOC LAYOUT (the bundled defaults assume this shape; see `README.md` Doc
classes section for the full rationale):

  0-MD/
    0-Documentation/
      TOC.md             (built by vf_toc.py; canonical index)
      internal/          contributor-facing docs (default)
        archived/        out-of-TOC; kept for history
      proposed/          captured-thinking, pre-canonical (default)
        archived/        out-of-TOC; shelved without graduation
      public/            customer / outside-reader docs (CREATED ON DEMAND, not by default)
        archived/        out-of-TOC; kept for history

`internal/` and `proposed/` are the two default classes. `public/` is
deliberately NOT pre-created — most projects don't have a public technical
readership, and pre-creating the directory invites unnecessary content +
maintenance debt. Create `public/` only when an outside reader explicitly
asks for documentation (graduate the relevant `internal/` or `proposed/`
doc into it then). The `audience: public` leak NOTE + scan activate the
moment that directory exists. `archived/` subdirs are kept on disk but
excluded from the TOC. `.scratch/` lives at the project root, gitignored,
never in the TOC.

ADAPT VOICE: this layout is a sensible default, not a contract. If your
project already has a different doc tree that works, keep yours and either
(a) edit `vf_toc.py`'s `SCAN_DIRS` to match it, or (b) replace the TOC builder
entirely. The only contractual ask is that `vf_render.py <path>` works.
ASK THE HUMAN before reorganising an existing doc tree to match this default —
re-homing docs en-masse is the kind of move that wants explicit human sign-off.

EDIT/REPLACE freely — this is a default. Your contract may name `vf_render.py`
as MANDATORY (per the OUR-block Render & TOC Discipline), but the
*implementation* is yours. Swap the markdown subset, swap the template, swap
the entire renderer if you have a preferred toolchain. Just keep the
entry-point command working.

This script:
  - Uses Path.cwd() as the project root by default; --root <path> overrides.
  - Refuses to render files outside the chosen project root.
  - Reads `template.html` from the same dir as this script for the styled shell.
  - Substitutes {title}, {path}, {brand}, {audience_class}, {audience_short},
    {audience_label}, {toc}, {meta_dl}, {body} into template.html.
  - Emits stderr `[VF-RENDER WARN]` for missing/incomplete frontmatter.
  - Emits stderr `[VF-RENDER NOTE]` on EVERY `audience: public` render —
    fires unconditionally to remind agent + human that the bundled scan
    catches only generic internal-jargon markers, not IP / PII / trade
    secrets / client names. A clean leak-pattern run is the most dangerous
    moment for false confidence; the NOTE is the standing caveat that
    fires even when the regex finds nothing.
  - Emits stderr `[VF-RENDER WARN]` for likely-internal patterns in `audience:
    public` doc bodies (ticket-shape codes, memory keys) — heuristic; ADAPT
    the regex or rephrase the doc if the pattern is legitimate for your
    public audience. ASK THE HUMAN if unsure.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


# ── Markdown → HTML (lifted from dogfood render_architecture_docs.py) ──────

def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    fm_block = text[4:end]
    body = text[end + 5:]
    fm: dict[str, str] = {}
    for line in fm_block.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return fm, body


def md_to_html(md: str) -> str:
    """Markdown subset: frontmatter, headings, paragraphs, lists, tables, code blocks, blockquotes, hr, inline."""
    lines = md.splitlines()
    out: list[str] = []
    i = 0
    in_code = False
    code_lang = ""
    code_buf: list[str] = []
    in_list: str | None = None  # 'ul' | 'ol' | None
    list_buf: list[str] = []

    def flush_list() -> None:
        nonlocal in_list, list_buf
        if in_list and list_buf:
            out.append(f"<{in_list}>")
            for item in list_buf:
                out.append(f"  <li>{inline(item)}</li>")
            out.append(f"</{in_list}>")
        in_list = None
        list_buf = []

    def inline(s: str) -> str:
        s = s.replace("&", "&amp;").replace("<", "&lt;").replace("&gt;", "&gt;")
        s = re.sub(r"`([^`]+)`", lambda m: f"<code>{m.group(1)}</code>", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", s)
        s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
        return s

    def render_table(table_lines: list[str]) -> str:
        if len(table_lines) < 2:
            return ""
        header_cells = [c.strip() for c in table_lines[0].strip().strip("|").split("|")]
        sep_cells = [c.strip() for c in table_lines[1].strip().strip("|").split("|")]
        align: list[str] = []
        for sc in sep_cells:
            if sc.startswith(":") and sc.endswith(":"):
                align.append("center")
            elif sc.endswith(":"):
                align.append("right")
            else:
                align.append("left")
        rows: list[list[str]] = []
        for line in table_lines[2:]:
            row = [c.strip() for c in line.strip().strip("|").split("|")]
            rows.append(row)
        html: list[str] = ['<div class="table-wrap"><table>']
        html.append("<thead><tr>")
        for j, h in enumerate(header_cells):
            a = align[j] if j < len(align) else "left"
            html.append(f'<th style="text-align:{a}">{inline(h)}</th>')
        html.append("</tr></thead>")
        html.append("<tbody>")
        for row in rows:
            html.append("<tr>")
            for j, c in enumerate(row):
                a = align[j] if j < len(align) else "left"
                html.append(f'<td style="text-align:{a}">{inline(c)}</td>')
            html.append("</tr>")
        html.append("</tbody></table></div>")
        return "\n".join(html)

    while i < len(lines):
        line = lines[i]

        # Code blocks
        if line.startswith("```"):
            if in_code:
                flush_list()
                code_text = "\n".join(code_buf)
                code_text = code_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                out.append(f'<pre class="code-block lang-{code_lang}"><code>{code_text}</code></pre>')
                in_code = False
                code_lang = ""
                code_buf = []
            else:
                flush_list()
                in_code = True
                code_lang = line[3:].strip()
            i += 1
            continue
        if in_code:
            code_buf.append(line)
            i += 1
            continue

        # Tables: detect by line starting with | and next line being separator
        if line.lstrip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            flush_list()
            table_lines = [line]
            i += 1
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                table_lines.append(lines[i])
                i += 1
            out.append(render_table(table_lines))
            continue

        # Headings
        m = re.match(r"^(#{1,6})\s+(.+)$", line)
        if m:
            flush_list()
            level = len(m.group(1))
            text = m.group(2)
            anchor = re.sub(r"[^\w\s-]", "", text).strip().lower().replace(" ", "-")
            out.append(f'<h{level} id="{anchor}">{inline(text)}</h{level}>')
            i += 1
            continue

        # Blockquote
        if line.startswith(">"):
            flush_list()
            quote_lines = []
            while i < len(lines) and lines[i].startswith(">"):
                quote_lines.append(lines[i].lstrip("> ").rstrip())
                i += 1
            out.append(f'<blockquote>{inline(" ".join(quote_lines))}</blockquote>')
            continue

        # Horizontal rule
        if re.match(r"^---+\s*$", line):
            flush_list()
            out.append("<hr>")
            i += 1
            continue

        # Unordered list
        m = re.match(r"^(\s*)[-*]\s+(.+)$", line)
        if m:
            if in_list != "ul":
                flush_list()
                in_list = "ul"
            list_buf.append(m.group(2))
            i += 1
            continue

        # Ordered list
        m = re.match(r"^(\s*)\d+\.\s+(.+)$", line)
        if m:
            if in_list != "ol":
                flush_list()
                in_list = "ol"
            list_buf.append(m.group(2))
            i += 1
            continue

        # Blank line
        if not line.strip():
            flush_list()
            i += 1
            continue

        # Paragraph (multi-line until blank or block-start)
        flush_list()
        para_lines = [line]
        i += 1
        while i < len(lines) and lines[i].strip() and not lines[i].startswith(("#", ">", "```", "|", "---")) and not re.match(r"^(\s*)[-*\d]", lines[i]):
            para_lines.append(lines[i])
            i += 1
        para = " ".join(para_lines)
        out.append(f"<p>{inline(para)}</p>")

    flush_list()
    return "\n".join(out)


def build_toc(html: str) -> str:
    """Build sidebar TOC from h1-h3 anchors."""
    headings = re.findall(r'<h([1-3]) id="([^"]+)">([^<]+)</h\1>', html)
    toc = ['<ul class="toc">']
    for level, anchor, text in headings:
        toc.append(f'<li class="toc-l{int(level)}"><a href="#{anchor}">{text}</a></li>')
    toc.append("</ul>")
    return "\n".join(toc)


def _audience_class(fm: dict[str, str]) -> tuple[str, str, str]:
    """Map frontmatter audience to (class, label, short)."""
    raw = (fm.get("audience") or "").lower()
    if "confidential" in raw or raw.startswith("sa"):
        return "confidential", "SA / Confidential", "SA"
    if "internal" in raw:
        return "internal", "Internal", "Internal"
    if "rescue" in raw:
        return "confidential", "Rescue (Operator)", "Rescue"
    return "public", "Public", "Public"


def _check_frontmatter_warn(fm: dict[str, str], src: Path) -> None:
    """Emit a stderr WARN for missing/incomplete frontmatter so the writer
    sees the gap at render time instead of discovering it later via '-'
    columns in the TOC. Errors should teach: when this fires, it tells you
    exactly what to add and where.

    REQUIRED (per house style in 0-MD/.tools/README.md): `title` + `audience`.
    RECOMMENDED (TOC metadata columns): `status` + `version` + `last_updated`.
    """
    REQUIRED = ("title", "audience")
    RECOMMENDED = ("status", "version", "last_updated")
    if not fm:
        sys.stderr.write(
            f"[VF-RENDER WARN] {src} has no YAML frontmatter. The TOC will "
            f"show '-' for audience/status/version/last_updated columns. "
            f"Add a canonical block at the top of the file:\n"
            f"  ---\n"
            f"  title: {src.stem}\n"
            f"  audience: internal      # or public / confidential\n"
            f"  status: draft           # or active / superseded\n"
            f"  version: 0.1.0\n"
            f"  last_updated: YYYY-MM-DD\n"
            f"  ---\n"
            f"  See 0-MD/.tools/README.md (Frontmatter section) for the full spec.\n"
        )
        return
    missing_required = [k for k in REQUIRED if k not in fm]
    missing_recommended = [k for k in RECOMMENDED if k not in fm]
    if missing_required:
        sys.stderr.write(
            f"[VF-RENDER WARN] {src} frontmatter is missing REQUIRED fields {missing_required}. "
            f"House style (0-MD/.tools/README.md): every doc must have at minimum `title` + `audience`. "
            f"TOC will show '-' for missing columns.\n"
        )
    if missing_recommended:
        sys.stderr.write(
            f"[VF-RENDER INFO] {src} frontmatter is missing recommended fields {missing_recommended}. "
            f"Add once the doc accumulates history; TOC will show '-' for these columns until then.\n"
        )


# ── Public-audience leak scan (heuristic; agent ADAPTs as needed) ──────────

# Patterns that LOOK LIKE internal-only references when they appear in a doc
# whose frontmatter says `audience: public`. Each row: (regex, why).
#
# These are HEURISTICS, not absolute rules. If your project legitimately uses
# any of these patterns externally — e.g. a public roadmap that exposes board
# ticket IDs by design — silence the scan by editing this list, or rephrase
# the doc, or move the file out of `public/`. ASK THE HUMAN when in doubt.
PUBLIC_LEAK_PATTERNS = [
    (r"\b[A-Z]{2,5}-\d{1,5}\b",       "Ticket-shape code (e.g. ABC-123) — usually points at an internal board the public reader can't dereference"),
    (r"\bIC-\d{2,4}\b",                "Internal change-marker (IC-XXX) — typically a private round-tracking shorthand"),
    (r"\bfeedback_[a-z][a-z_]+\b",    "Memory-key shape (feedback_*) — agent-private memory file; no public resolver"),
]


def _emit_public_audience_note(fm: dict[str, str], src: Path) -> None:
    """Always-fires NOTE on every audience: public render — fires BEFORE the
    leak-pattern scan, regardless of whether the scan finds anything.

    The NOTE is the standing caveat that the bundled scan only catches a
    narrow class of generic internal-jargon markers. It is NOT an
    IP / PII / trade-secret / client-name / commercial-confidence check.

    A clean PUBLIC_LEAK_PATTERNS run is the most dangerous moment for false
    confidence — if anything, that's when the agent + human most need to be
    reminded that the automated scope is narrow and the rest is human
    judgement. Emit unconditionally on audience: public so neither agent nor
    human concludes "the guard passed, ship it."
    """
    raw = (fm.get("audience") or "").lower()
    if "public" not in raw:
        return
    sys.stderr.write(
        f"[VF-RENDER NOTE] {src} (audience=public): the bundled scan only "
        f"catches a narrow class of generic internal-jargon markers "
        f"(ticket-shape codes, IC-XXX, memory-key shapes). It is NOT an "
        f"IP / PII / trade-secret / client-name / commercial-confidence "
        f"check. You know your customer, your contracts, your codenames - "
        f"protect your own IP. ASK THE HUMAN to eyeball any public doc "
        f"before it ships externally; the agent's review is necessary but "
        f"not sufficient.\n"
    )


def _check_public_audience_warn(fm: dict[str, str], body_md: str, src: Path) -> None:
    """Heuristic: flag likely-internal patterns in `audience: public` doc bodies.
    Public docs ship to readers outside your project; references like ticket IDs
    or memory keys land as dangling pointers for them.

    AGENT NEEDS TO ADAPT: this is a default scan that suits most projects.
    False positives are expected when your project uses these shapes legitimately
    (e.g. a public roadmap that names public ticket IDs). Either edit
    PUBLIC_LEAK_PATTERNS to silence the rule, rephrase the doc, or re-classify
    the file (move out of `public/`). ASK THE HUMAN before silencing a class
    of warning — a one-off rephrase is cheaper than a missed leak.
    """
    raw = (fm.get("audience") or "").lower()
    if "public" not in raw:
        return
    seen: set[tuple[str, str]] = set()
    for lineno, line in enumerate(body_md.splitlines(), start=1):
        for pattern, why in PUBLIC_LEAK_PATTERNS:
            m = re.search(pattern, line)
            if m and (m.group(0), why) not in seen:
                seen.add((m.group(0), why))
                sys.stderr.write(
                    f"[VF-RENDER WARN] {src} (audience=public) line {lineno}: "
                    f"pattern '{m.group(0)}' looks internal-only. {why}. "
                    f"If legitimate for this audience, edit PUBLIC_LEAK_PATTERNS "
                    f"in vf_render.py or rephrase the doc; if unsure, ASK THE HUMAN.\n"
                )


def render(path: Path, project_root: Path) -> Path:
    """Render markdown file at `path` (relative to project_root unless absolute) to sibling .html."""
    template_path = Path(__file__).resolve().parent / "template.html"
    if not template_path.exists():
        raise SystemExit(f"Template not found at {template_path} — restore from board scaffold or your own version.")

    src = (project_root / path).resolve() if not path.is_absolute() else path.resolve()
    project_root = project_root.resolve()
    if project_root not in src.parents and src != project_root:
        raise SystemExit(f"Refusing to render outside project root: {src}")

    text = src.read_text(encoding="utf-8")
    fm, body_md = parse_frontmatter(text)
    _rel_for_warn = src.relative_to(project_root) if project_root in src.parents else src
    _check_frontmatter_warn(fm, _rel_for_warn)
    _emit_public_audience_note(fm, _rel_for_warn)
    _check_public_audience_warn(fm, body_md, _rel_for_warn)
    body_html = md_to_html(body_md)
    toc_html = build_toc(body_html)
    title = fm.get("title", src.stem)
    audience_class, audience_label, audience_short = _audience_class(fm)

    meta_items: list[str] = []
    for k in ("audience", "status", "version", "last_updated", "authors", "supersedes"):
        if k in fm:
            meta_items.append(f'<div><dt>{k.replace("_", " ")}</dt><dd>{fm[k]}</dd></div>')
    if not meta_items:
        meta_items.append('<div><dt>type</dt><dd>Project documentation</dd></div>')

    rel_path = src.relative_to(project_root)
    brand = "Project Docs"  # customer can edit template.html to change

    html = (
        template_path.read_text(encoding="utf-8")
        .replace("{title}", title)
        .replace("{brand}", brand)
        .replace("{path}", str(rel_path))
        .replace("{audience_class}", audience_class)
        .replace("{audience_label}", audience_label)
        .replace("{audience_short}", audience_short)
        .replace("{toc}", toc_html)
        .replace("{meta_dl}", "\n        ".join(meta_items))
        .replace("{body}", body_html)
    )

    target = src.with_suffix(".html")
    target.write_text(html, encoding="utf-8")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a markdown file to sibling HTML (full MC theme).")
    parser.add_argument("path", help="Markdown file path (relative to project root unless absolute)")
    parser.add_argument("--root", help="Project root override (default: Path.cwd())", default=None)
    args = parser.parse_args()
    project_root = Path(args.root) if args.root else Path.cwd()
    if not project_root.exists():
        raise SystemExit(f"Project root does not exist: {project_root}")
    target = render(Path(args.path), project_root)
    print(target.relative_to(project_root.resolve()))


if __name__ == "__main__":
    main()
