"""
Render the public + internal architecture markdown docs to styled HTML.

Output:
  0-MD/architecture/public/AUTH-AGENT-ARCHITECTURE.html
  0-MD/architecture/internal/AUTH-AGENT-ARCHITECTURE-INTERNAL.html

These are also served live via /docs/auth-agent and /admin/docs/auth-agent-internal.

Run from repo root:
  python scripts/render_architecture_docs.py
"""
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

DOCS_DIR = REPO / "0-MD" / "0-Documentation"

PUBLIC_DIR = DOCS_DIR
INTERNAL_DIR = DOCS_DIR

PUBLIC_MD = DOCS_DIR / "auth-agent.md"
INTERNAL_MD = DOCS_DIR / "auth-agent-internal.md"
PUBLIC_HTML = PUBLIC_MD.with_suffix(".html")
INTERNAL_HTML = INTERNAL_MD.with_suffix(".html")

# Proposed docs — rendered to HTML for review but not wired to live routes
PROPOSED_DIR = DOCS_DIR / "proposed"

# NOTE: This script no longer copies HTML fragments into app/static/docs/.
# The doc routes were ripped out 2026-04-08 (see VF-248). Engineering docs
# now live ONLY in 0-MD/ as source MDs + sibling HTMLs, consumed by the
# library bundles in 0-MD/library/. The board UI does not surface them.


# ── Minimal markdown → HTML ──────────────────────────────────
def parse_frontmatter(text):
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    fm_block = text[4:end]
    body = text[end + 5:]
    fm = {}
    for line in fm_block.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return fm, body


def md_to_html(md):
    """Tiny markdown subset: headings, paragraphs, lists, tables, code blocks, blockquotes, inline code, bold, em, links."""
    lines = md.splitlines()
    out = []
    i = 0
    in_code = False
    code_lang = ""
    code_buf = []
    in_list = None  # 'ul' or 'ol' or None
    list_buf = []

    def flush_list():
        nonlocal in_list, list_buf
        if in_list and list_buf:
            out.append(f"<{in_list}>")
            for item in list_buf:
                out.append(f"  <li>{inline(item)}</li>")
            out.append(f"</{in_list}>")
        in_list = None
        list_buf = []

    def inline(s):
        # Escape HTML first (but not pre-escaped entities or our own placeholders)
        # We do a careful pass: escape <, >, & except in code spans which we mark
        s = s.replace("&", "&amp;").replace("<", "&lt;").replace("&gt;", "&gt;")
        # inline code
        s = re.sub(r"`([^`]+)`", lambda m: f"<code>{m.group(1)}</code>", s)
        # bold
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        # italic (avoid matching ** which we already replaced)
        s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", s)
        # links [text](url)
        s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
        return s

    def render_table(table_lines):
        if len(table_lines) < 2:
            return ""
        # First line = header, second = separator (with alignment), rest = body
        header_cells = [c.strip() for c in table_lines[0].strip().strip("|").split("|")]
        sep_cells = [c.strip() for c in table_lines[1].strip().strip("|").split("|")]
        align = []
        for sc in sep_cells:
            if sc.startswith(":") and sc.endswith(":"):
                align.append("center")
            elif sc.endswith(":"):
                align.append("right")
            else:
                align.append("left")
        rows = []
        for line in table_lines[2:]:
            row = [c.strip() for c in line.strip().strip("|").split("|")]
            rows.append(row)
        html = ['<div class="table-wrap"><table>']
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
                # Escape inside code
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

        # Paragraph
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


# ── Build TOC from headings ───────────────────────────────────
def build_toc(html):
    headings = re.findall(r'<h([1-3]) id="([^"]+)">([^<]+)</h\1>', html)
    toc = ['<ul class="toc">']
    last_level = 1
    for level, anchor, text in headings:
        level = int(level)
        toc.append(f'<li class="toc-l{level}"><a href="#{anchor}">{text}</a></li>')
    toc.append("</ul>")
    return "\n".join(toc)


# ── Page template (admin-portal design language: MC-vibe + violet + neon glow strip) ──
TEMPLATE = """<!DOCTYPE html>
<html lang="en" data-theme="dark" data-preset="violet">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  /* ============ TOKENS (mirror app/static/css/tokens.css — admin portal palette) ============ */
  :root {{
    --hue: 262;
    --color-brand: hsl(var(--hue) 68% 55%);
    --color-brand-hover: hsl(var(--hue) 68% 48%);
    --color-brand-text: hsl(var(--hue) 68% 75%);
    --color-bg: #0a0f1e;
    --color-surface: rgba(15, 23, 42, 0.85);
    --color-surface-sub: rgba(20, 30, 50, 0.9);
    --color-sidebar: #060b16;
    --color-border: rgba(124, 58, 237, 0.18);
    --color-border-strong: rgba(124, 58, 237, 0.35);
    --color-text: #e2e8f0;
    --color-text-muted: #94a3b8;
    --color-text-subtle: #64748b;
    --color-done: #10b981;
    --color-amber: #f59e0b;
    --color-blocked: #ef4444;
    --font-sans: 'Plus Jakarta Sans', system-ui, -apple-system, sans-serif;
    --font-mono: 'JetBrains Mono', 'Fira Code', monospace;
    --radius-md: 0.5rem;
    --radius-lg: 0.75rem;
    --shadow-card: 0 1px 3px 0 rgb(0 0 0 / 0.4);
  }}

  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html, body {{ min-height: 100%; }}
  body {{
    font-family: var(--font-sans);
    background: var(--color-bg);
    background-image:
      radial-gradient(ellipse at top left, rgba(124, 58, 237, 0.10), transparent 50%),
      radial-gradient(ellipse at bottom right, rgba(56, 189, 248, 0.06), transparent 55%);
    background-attachment: fixed;
    color: var(--color-text);
    line-height: 1.65;
    font-size: 14.5px;
    -webkit-font-smoothing: antialiased;
  }}
  ::-webkit-scrollbar {{ width: 6px; height: 6px; }}
  ::-webkit-scrollbar-thumb {{ background: var(--color-border-strong); border-radius: 99px; }}
  ::-webkit-scrollbar-track {{ background: transparent; }}

  /* ============ TOPBAR ============ */
  .topbar-glow {{
    height: 2px;
    background: linear-gradient(90deg, transparent, var(--color-brand), #38bdf8, var(--color-brand), transparent);
    position: sticky;
    top: 0;
    z-index: 11;
  }}
  .topbar {{
    background: var(--color-surface);
    backdrop-filter: blur(8px);
    border-bottom: 1px solid var(--color-border);
    padding: 0.7rem 1.5rem;
    display: flex;
    align-items: center;
    gap: 1rem;
    position: sticky;
    top: 2px;
    z-index: 10;
  }}
  .brand-block {{ display: flex; flex-direction: column; gap: 1px; }}
  .brand {{
    font-weight: 700;
    font-size: 1rem;
    color: var(--color-brand-text);
    letter-spacing: 0.02em;
  }}
  .brand-sub {{
    font-size: 0.7rem;
    color: var(--color-text-muted);
    font-family: var(--font-mono);
  }}
  .topbar-meta {{
    margin-left: auto;
    display: flex;
    gap: 0.5rem;
    align-items: center;
  }}
  .audience-pill {{
    padding: 0.22rem 0.65rem;
    border-radius: 99px;
    font-size: 0.65rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.09em;
    border: 1px solid;
    font-family: var(--font-mono);
  }}
  .audience-pill.public {{
    background: rgba(124, 58, 237, 0.15);
    color: var(--color-brand-text);
    border-color: rgba(124, 58, 237, 0.4);
  }}
  .audience-pill.internal {{
    background: rgba(245, 158, 11, 0.15);
    color: var(--color-amber);
    border-color: rgba(245, 158, 11, 0.4);
  }}
  .audience-pill.confidential {{
    background: rgba(239, 68, 68, 0.15);
    color: var(--color-blocked);
    border-color: rgba(239, 68, 68, 0.4);
  }}
  .audience-pill.index {{
    background: rgba(148, 163, 184, 0.12);
    color: var(--color-text-muted);
    border-color: rgba(148, 163, 184, 0.35);
  }}

  /* ============ LAYOUT ============ */
  .layout {{
    display: grid;
    grid-template-columns: 260px 1fr;
    max-width: 1280px;
    margin: 0 auto;
  }}
  .sidebar {{
    border-right: 1px solid var(--color-border);
    padding: 1.5rem 1.25rem;
    position: sticky;
    top: 50px;
    align-self: start;
    max-height: calc(100vh - 50px);
    overflow-y: auto;
    background: rgba(6, 11, 22, 0.4);
  }}
  .sidebar-kicker {{
    font-family: var(--font-mono);
    font-size: 0.6rem;
    color: var(--color-brand-text);
    text-transform: uppercase;
    letter-spacing: 0.13em;
    margin-bottom: 0.85rem;
    font-weight: 700;
  }}
  .toc {{ list-style: none; }}
  .toc li {{ margin: 0.15rem 0; }}
  .toc a {{
    color: var(--color-text-muted);
    text-decoration: none;
    font-size: 0.78rem;
    display: block;
    padding: 0.25rem 0.5rem;
    border-left: 2px solid transparent;
    transition: all 150ms ease;
    border-bottom: none;
  }}
  .toc a:hover {{
    color: var(--color-text);
    border-left-color: var(--color-brand);
    background: rgba(124, 58, 237, 0.06);
  }}
  .toc-l1 > a {{ color: var(--color-text); font-weight: 600; }}
  .toc-l2 > a {{ padding-left: 1.2rem; }}
  .toc-l3 > a {{ padding-left: 2rem; font-size: 0.72rem; }}

  /* ============ CONTENT ============ */
  .content {{
    padding: 2rem 2.5rem 4rem;
    max-width: 920px;
  }}

  /* Doc meta card (frontmatter — violet stripe like wizard cards) */
  .doc-meta {{
    background: var(--color-surface);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-lg);
    padding: 1rem 1.25rem 1rem 1.5rem;
    margin-bottom: 2rem;
    box-shadow: var(--shadow-card);
    position: relative;
    overflow: hidden;
  }}
  .doc-meta::before {{
    content: '';
    position: absolute;
    top: 0; left: 0;
    width: 3px; height: 100%;
    background: linear-gradient(180deg, var(--color-brand), #38bdf8);
  }}
  .doc-meta dl {{
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 0.35rem 1rem;
    font-size: 0.78rem;
  }}
  .doc-meta dl > div {{ display: contents; }}
  .doc-meta dt {{
    color: var(--color-text-subtle);
    text-transform: uppercase;
    letter-spacing: 0.09em;
    font-size: 0.62rem;
    font-weight: 700;
    align-self: center;
    font-family: var(--font-mono);
  }}
  .doc-meta dd {{ color: var(--color-text); align-self: center; }}

  /* ============ TYPOGRAPHY ============ */
  h1 {{
    font-size: 1.85rem;
    color: var(--color-text);
    margin: 2rem 0 0.6rem;
    font-weight: 700;
    border-bottom: 1px solid var(--color-border);
    padding-bottom: 0.5rem;
    letter-spacing: -0.01em;
  }}
  h1:first-of-type {{ margin-top: 0.5rem; }}
  h2 {{
    font-size: 1.3rem;
    color: var(--color-brand-text);
    margin: 2.25rem 0 0.5rem;
    font-weight: 700;
    letter-spacing: -0.005em;
  }}
  h2::before {{
    content: '// ';
    color: var(--color-brand);
    font-family: var(--font-mono);
    font-size: 0.85em;
    font-weight: 700;
    opacity: 0.7;
  }}
  h3 {{
    font-size: 1.05rem;
    color: var(--color-text);
    margin: 1.5rem 0 0.4rem;
    font-weight: 600;
  }}
  h4 {{
    font-size: 0.85rem;
    color: var(--color-text-muted);
    margin: 1.2rem 0 0.3rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }}

  p {{ margin: 0.6rem 0; }}
  ul, ol {{ margin: 0.6rem 0 0.6rem 1.5rem; }}
  li {{ margin: 0.25rem 0; }}
  li::marker {{ color: var(--color-brand); }}

  blockquote {{
    border-left: 3px solid var(--color-brand);
    padding: 0.85rem 1.15rem;
    margin: 1.2rem 0;
    background: rgba(124, 58, 237, 0.08);
    border-radius: 0 var(--radius-md) var(--radius-md) 0;
    color: var(--color-text);
  }}
  blockquote p {{ margin: 0.3rem 0; }}
  blockquote strong {{ color: var(--color-brand-text); }}
  blockquote em {{ color: var(--color-text-muted); }}

  code {{
    font-family: var(--font-mono);
    background: rgba(124, 58, 237, 0.12);
    padding: 1px 6px;
    border-radius: 3px;
    font-size: 0.82em;
    color: var(--color-brand-text);
  }}
  .code-block {{
    background: rgba(6, 11, 22, 0.7);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-md);
    padding: 0.95rem 1.15rem 0.95rem 1.3rem;
    margin: 1rem 0;
    overflow-x: auto;
    font-family: var(--font-mono);
    font-size: 0.78rem;
    line-height: 1.55;
    position: relative;
  }}
  .code-block::before {{
    content: '';
    position: absolute;
    top: 0; left: 0;
    width: 2px; height: 100%;
    background: var(--color-brand);
    border-radius: 0 0 0 var(--radius-md);
  }}
  .code-block code {{
    background: none;
    color: var(--color-text);
    padding: 0;
    font-size: inherit;
  }}

  hr {{
    border: none;
    border-top: 1px dashed var(--color-border-strong);
    margin: 2.5rem 0;
  }}
  a {{
    color: var(--color-brand-text);
    text-decoration: none;
    border-bottom: 1px dotted rgba(124, 58, 237, 0.5);
    transition: all 150ms ease;
  }}
  a:hover {{
    color: var(--color-brand);
    border-bottom-style: solid;
  }}
  strong {{ color: var(--color-text); font-weight: 700; }}
  em {{ color: var(--color-text); font-style: italic; }}

  /* ============ TABLES ============ */
  .table-wrap {{
    overflow-x: auto;
    margin: 1.2rem 0;
    border-radius: var(--radius-md);
    border: 1px solid var(--color-border);
    background: var(--color-surface);
  }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.8rem; }}
  th {{
    background: rgba(124, 58, 237, 0.12);
    color: var(--color-brand-text);
    padding: 0.6rem 0.85rem;
    font-family: var(--font-mono);
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    border-bottom: 1px solid var(--color-border-strong);
    text-align: left;
    font-weight: 700;
  }}
  td {{
    padding: 0.55rem 0.85rem;
    border-bottom: 1px solid var(--color-border);
    color: var(--color-text);
  }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: rgba(124, 58, 237, 0.05); }}
  td code {{ font-size: 0.8em; }}

  /* Overview highlight (first paragraph after #overview anchor) */
  h1#overview + p,
  h2#overview + p {{
    background: linear-gradient(135deg, rgba(124, 58, 237, 0.12), transparent);
    border-left: 3px solid var(--color-brand);
    padding: 1rem 1.25rem;
    border-radius: 0 var(--radius-md) var(--radius-md) 0;
  }}

  /* ============ MOBILE ============ */
  @media (max-width: 900px) {{
    .layout {{ grid-template-columns: 1fr; }}
    .sidebar {{
      position: relative;
      top: 0;
      max-height: none;
      border-right: none;
      border-bottom: 1px solid var(--color-border);
    }}
    .content {{ padding: 1.5rem; }}
  }}
</style>
</head>
<body>
<div class="topbar-glow"></div>
<div class="topbar">
  <div class="brand-block">
    <span class="brand">VibeForge+</span>
    <span class="brand-sub">{title}</span>
  </div>
  <div class="topbar-meta">
    <span class="audience-pill {audience_class}">{audience_short}</span>
  </div>
</div>
<div class="layout">
  <aside class="sidebar">
    <div class="sidebar-kicker">// {audience_label}</div>
    {toc}
  </aside>
  <main class="content">
    <div class="doc-meta">
      <dl>
        {meta_dl}
      </dl>
    </div>
    {body}
  </main>
</div>
</body>
</html>"""


def render_doc(md_path: Path, html_path: Path, body_only_path: Path = None, toc_path: Path = None):
    print(f"Rendering {md_path.name}...")
    text = md_path.read_text(encoding="utf-8")
    fm, body_md = parse_frontmatter(text)
    body_html = md_to_html(body_md)
    toc = build_toc(body_html)
    title = fm.get("title", md_path.stem)
    audience_raw = fm.get("audience", "public").lower()
    if "confidential" in audience_raw or audience_raw.startswith("sa"):
        audience_class = "confidential"
        audience_label = "SA / Confidential"
        audience_short = "SA"
    elif "internal" in audience_raw:
        audience_class = "internal"
        audience_label = "Internal"
        audience_short = "Internal"
    elif "rescue" in audience_raw:
        audience_class = "confidential"
        audience_label = "Rescue (Operator)"
        audience_short = "Rescue"
    elif "index" in audience_raw:
        # VF-364 extension: TOC / generated-index docs render with neutral
        # grey pill + "// Index" sidebar kicker, distinct from public/internal/etc.
        audience_class = "index"
        audience_label = "Index"
        audience_short = "Index"
    else:
        audience_class = "public"
        audience_label = "Public"
        audience_short = "Public"
    meta_items = []
    for k in ("audience", "status", "version", "last_updated", "authors", "supersedes", "public_companion"):
        if k in fm:
            meta_items.append(
                f'<div><dt>{k.replace("_", " ")}</dt><dd>{fm[k]}</dd></div>'
            )
    html = TEMPLATE.format(
        title=title,
        audience_class=audience_class,
        audience_label=audience_label,
        audience_short=audience_short,
        toc=toc,
        meta_dl="\n        ".join(meta_items),
        body=body_html,
    )
    html_path.write_text(html, encoding="utf-8")
    print(f"  -> {html_path.relative_to(REPO)}")

    # Body-only fragment for embedding in the tabbed Jinja template
    if body_only_path:
        meta_html = '<div class="doc-meta-inline"><dl>' + "".join(meta_items) + "</dl></div>"
        body_only_path.write_text(meta_html + body_html, encoding="utf-8")
        print(f"  -> {body_only_path.relative_to(REPO)}")
    if toc_path:
        toc_path.write_text(toc, encoding="utf-8")
        print(f"  -> {toc_path.relative_to(REPO)}")


def main():
    """Render every MD in 0-Documentation/ and 0-MD/proposed/ to a sibling HTML.

    No runtime fragments. No app/static/docs copies. The board UI does not
    serve docs (see VF-248). The libraries in 0-MD/library/ are the canonical
    delivery mechanism for VibeForge+'s own engineering docs.
    """
    rendered = 0
    SKIP_DIRS = {"_meta-mirror", "drafts", "archive"}
    for src_dir in (DOCS_DIR, PROPOSED_DIR):
        if not src_dir.exists():
            continue
        # Top-level + audience subfolders (public/, internal/, confidential/, rescue/, etc.)
        md_files = list(src_dir.glob("*.md"))
        for sub in sorted(src_dir.iterdir()):
            if sub.is_dir() and sub.name not in SKIP_DIRS and not sub.name.startswith("."):
                md_files.extend(sub.glob("*.md"))
        for md_file in sorted(md_files):
            html_file = md_file.with_suffix(".html")
            render_doc(md_file, html_file)
            rendered += 1
    print(f"Done. Rendered {rendered} doc(s).")


if __name__ == "__main__":
    main()
