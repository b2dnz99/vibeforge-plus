"""
Build the VibeForge+ Toolkit Library + Rescue Card.

Two outputs from the same source:

  0-MD/library/VIBEFORGE-TOOLKIT-LIBRARY.html      ← all toolkit docs, tabbed (latest pointer)
  0-MD/library/VIBEFORGE-TOOLKIT-LIBRARY-v{N}.html ← versioned snapshot
  0-MD/library/VIBEFORGE-RESCUE-CARD.html          ← rescue-tier docs only, single-tier emergency reading

Source: 0-MD/0-Documentation/TOC.json (regenerate with scripts/build_toc.py first).

Run from repo root:
  python scripts/build_toc.py
  python scripts/build_toolkit_library.py
"""
import json
import re
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
from render_architecture_docs import md_to_html, build_toc, parse_frontmatter  # noqa

VERSION = "1.0"
CONTENT_DATE = date.today().isoformat()

TOC_JSON = REPO / "0-MD" / "0-Documentation" / "TOC.json"
LIB_DIR = REPO / "0-MD" / "library"

CSS = """
:root{--bg:#0f1520;--surface:#141e32;--sub:#0f172a;--border:#334155;--text:#c8d0dc;--mute:#64748b;--strong:#e2e8f0;--accent:#f59e0b;--mono:Consolas,monospace}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:var(--bg);color:var(--text);font-size:14px;line-height:1.6}
header{padding:1.2rem 2rem;border-bottom:1px solid var(--border);background:var(--sub);display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:1rem}
header h1{font-size:1.3rem;color:var(--strong)}
header .version{font-family:var(--mono);font-size:.65rem;color:var(--accent);background:rgba(245,158,11,.1);padding:.3rem .7rem;border-radius:4px;border:1px solid rgba(245,158,11,.3)}
header p{color:var(--mute);font-size:.75rem;margin-top:.3rem;width:100%}
.tabs{display:flex;border-bottom:1px solid var(--border);background:var(--sub);padding:0 2rem;flex-wrap:wrap}
.tab{background:none;border:none;color:var(--mute);font-family:var(--mono);font-size:.7rem;text-transform:uppercase;letter-spacing:.06em;padding:.9rem 1.4rem;cursor:pointer;border-bottom:2px solid transparent;transition:all .15s}
.tab:hover{color:var(--text)}
.tab.active{color:var(--accent);border-bottom-color:var(--accent)}
.pane{display:none;grid-template-columns:260px 1fr;gap:1.5rem;padding:2rem;max-width:1400px;margin:0 auto}
.pane.active{display:grid}
.toc{position:sticky;top:1rem;align-self:start;max-height:calc(100vh - 4rem);overflow-y:auto;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:1rem}
.toc ul{list-style:none}
.toc a{color:var(--mute);text-decoration:none;font-size:.72rem;display:block;padding:.25rem .5rem;border-left:2px solid transparent;border-radius:0 4px 4px 0;transition:all .15s}
.toc a:hover{color:var(--text);border-left-color:var(--accent);background:rgba(245,158,11,.08)}
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
main blockquote{border-left:3px solid var(--accent);padding:.6rem 1rem;margin:1rem 0;background:rgba(245,158,11,.08);border-radius:0 6px 6px 0;font-style:italic;font-size:.85rem}
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


def load_toolkit_entries():
    if not TOC_JSON.exists():
        print("ERROR: 0-MD/0-Documentation/TOC.json not found. Run scripts/build_toc.py first.", file=sys.stderr)
        sys.exit(1)
    toc = json.loads(TOC_JSON.read_text(encoding="utf-8"))
    return toc.get("toolkit", [])


def build_pane(entry, active):
    p = REPO / entry["path"]
    text = p.read_text(encoding="utf-8")
    fm, body_md = parse_frontmatter(text)
    body = md_to_html(body_md)
    slug = entry["slug"]
    body = re.sub(r'id="([^"]+)"', lambda m: f'id="{slug}-{m.group(1)}"', body)
    body = re.sub(r'href="#([^"]+)"', lambda m: f'href="#{slug}-{m.group(1)}"', body)
    toc = build_toc(body)
    label = fm.get("title", slug)
    meta_items = [f"<b>{k}</b> {v}" for k, v in fm.items() if k in ("status", "version", "last_updated", "audience", "tool_target")]
    meta = f'<div class="meta">{" · ".join(meta_items)}</div>' if meta_items else ""
    cls = "pane active" if active else "pane"
    return (
        f'<button class="tab{" active" if active else ""}" onclick="show(\'{slug}\',this)">{label}</button>',
        f'<div class="{cls}" id="pane-{slug}"><aside class="toc">{toc}</aside><main>{meta}{body}</main></div>',
    )


def build_html(title, subtitle, version, entries):
    if not entries:
        body_html = '<div style="padding:3rem;text-align:center;color:var(--mute)">No toolkit docs yet.</div>'
    else:
        tabs = []
        panes = []
        for i, entry in enumerate(entries):
            tab, pane = build_pane(entry, active=(i == 0))
            tabs.append(tab)
            panes.append(pane)
        body_html = f'<div class="tabs">{"".join(tabs)}</div>{"".join(panes)}'

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{CSS}</style></head><body>
<header>
  <div>
    <h1>{title}</h1>
    <p>{subtitle}</p>
  </div>
  <div class="version">v{version} · {CONTENT_DATE}</div>
</header>
{body_html}
<script>
function show(slug, btn) {{
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.pane').forEach(p => p.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('pane-' + slug).classList.add('active');
  window.scrollTo(0, 0);
}}
</script></body></html>"""


def main():
    LIB_DIR.mkdir(parents=True, exist_ok=True)
    entries = load_toolkit_entries()
    rescue_entries = [e for e in entries if e.get("audience") == "rescue"]

    toolkit_html = build_html(
        "VibeForge+ Toolkit Library",
        f"Operational tools — bootstrap, recovery, verify · {len(entries)} tools",
        VERSION, entries,
    )
    (LIB_DIR / f"VIBEFORGE-TOOLKIT-LIBRARY-v{VERSION}.html").write_text(toolkit_html, encoding="utf-8")
    (LIB_DIR / "VIBEFORGE-TOOLKIT-LIBRARY.html").write_text(toolkit_html, encoding="utf-8")

    rescue_html = build_html(
        "VibeForge+ Rescue Card",
        f"Emergency reference — read when things break · {len(rescue_entries)} rescue docs",
        VERSION, rescue_entries,
    )
    (LIB_DIR / "VIBEFORGE-RESCUE-CARD.html").write_text(rescue_html, encoding="utf-8")

    print(f"Built v{VERSION} ({CONTENT_DATE})")
    print(f"  -> 0-MD/library/VIBEFORGE-TOOLKIT-LIBRARY-v{VERSION}.html ({len(toolkit_html):,} bytes)")
    print(f"  -> 0-MD/library/VIBEFORGE-TOOLKIT-LIBRARY.html (latest)")
    print(f"  -> 0-MD/library/VIBEFORGE-RESCUE-CARD.html ({len(rescue_html):,} bytes, {len(rescue_entries)} rescue tools)")
    print(f"  Tools: {len(entries)}  ·  Rescue: {len(rescue_entries)}")


if __name__ == "__main__":
    main()
