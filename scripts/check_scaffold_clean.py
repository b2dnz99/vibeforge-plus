"""Audience-separation guard for VibeForge+ customer-facing surfaces.

Scans the surfaces that customers see for internal-jargon patterns that
would land as dangling pointers in customer projects. Three surfaces:

  1. SCAFFOLD ARTEFACTS (default; no flags) — files in app/onboard_scaffold/
     that materialise verbatim into customer projects via /api/v2/onboard/scaffold.

  2. OUR-BLOCK SOURCE (`--our-block`) — the markdown file at
     0-MD/0-Documentation/internal/customer-onboard-our-block.md that gets
     pasted verbatim into every customer's CLAUDE.md/AGENTS.md at onboard
     step 7, AND its inline-fallback constant in app/api/v2/onboard.py.

  3. LIVE /agentnotes RESPONSE (`--live <base_url> <bearer_token>`) — the
     JSON returned by /api/v2/agentnotes/{slug} on every authenticated read.
     Walks all string values; ignores keys; flags forbidden patterns.
     This is the most authoritative scan — eliminates comment-vs-string
     ambiguity in app/api/v2/contract.py by checking what the customer
     actually receives, not what's in the source.

     KNOWN LIMITATION: this mode currently false-positives on per-agent
     context fields (e.g. `agent.slug`, bootstrap commands templated with
     the agent's actual project slug). For VibeForge+'s own dogfood agent
     these contain "vibeforge-plus" — which is correct contextualisation,
     not a leak (a customer agent on project "their-slug" would correctly
     return "their-slug" in the same fields). Refinement needed to skip
     per-agent context paths and scan only generic rule/note content.
     For now, treat live-mode flags as a rough sanity check — focus on
     leaks in `*.rule` / `*.note` / `*.detail` / `*.description` paths
     which ARE generic and SHOULD be slug-free.

Why these surfaces matter: each is something a customer's agent / customer's
terminal / customer's editor will display verbatim. Internal-only references
in any of them become dangling pointers (no project context to dereference).
Same category of mistake as committing internal hostnames in public Dockerfiles.

Usage:
    python scripts/check_scaffold_clean.py                       # scaffold only (default)
    python scripts/check_scaffold_clean.py --our-block           # + OUR-block source + inline
    python scripts/check_scaffold_clean.py --all                 # all static surfaces (scaffold + OUR-block)
    python scripts/check_scaffold_clean.py --live <base_url>     # + live /agentnotes scan (requires VIBEFORGE_TOKEN env)

Exit code 0 if all scanned surfaces clean; 1 if any leak found; 2 on missing files.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCAFFOLD_DIR = REPO_ROOT / "app" / "onboard_scaffold"
OUR_BLOCK_SOURCE = REPO_ROOT / "0-MD" / "0-Documentation" / "internal" / "customer-onboard-our-block.md"
ONBOARD_PY = REPO_ROOT / "app" / "api" / "v2" / "onboard.py"

# Forbidden patterns. Comment after each = customer-experience problem.
FORBIDDEN_PATTERNS = [
    (r"\bfeedback_[a-z_]+\b",       "Memory key (feedback_*) — only resolves on PK's local agent state, dangling for customers"),
    (r"\bIC-\d{3}\b",                "Internal ticket prefix (IC-XXX) — VibeForge+'s round-tracking, meaningless to customers"),
    (r"\bVF-\d+\b",                  "Internal ticket prefix (VF-XXX) — VibeForge+ board ticket, no resolver in customer's project"),
    (r"\bR\d+\.\d+(\.\d+)?\b",       "Internal round reference (R2.X) — VibeForge+ release-numbering jargon"),
    (r"\bCUSTOMER-ONBOARD-[A-Z\-]+\b", "Internal proposed-doc filename"),
    (r"\bvibeforge-plus\b",          "Internal project slug — every customer has their own slug; this is ours"),
]

SHIPPED_FILES = ["vf_render.py", "vf_toc.py", "template.html", "README.md"]


def _scan_text(text: str, source_label: str) -> list[tuple[str, int, str, str, str]]:
    """Scan text line-by-line; return list of (source, lineno, match, line_text, why)."""
    leaks = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for pattern, why in FORBIDDEN_PATTERNS:
            m = re.search(pattern, line)
            if m:
                leaks.append((source_label, lineno, m.group(0), line.strip()[:120], why))
    return leaks


def _scan_json_value(value, path: str, leaks: list) -> None:
    """Walk a JSON value recursively; for any string, scan against FORBIDDEN_PATTERNS.
    Only string VALUES are scanned (keys are part of the contract spec). Path tracks
    the nested location so the leak report tells you exactly where in the response."""
    if isinstance(value, str):
        for pattern, why in FORBIDDEN_PATTERNS:
            m = re.search(pattern, value)
            if m:
                snippet = value[max(0, m.start() - 30):m.end() + 60]
                leaks.append((f"/agentnotes:{path}", 0, m.group(0), snippet[:160], why))
    elif isinstance(value, dict):
        for k, v in value.items():
            _scan_json_value(v, f"{path}.{k}", leaks)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _scan_json_value(v, f"{path}[{i}]", leaks)


def scan_scaffold() -> list:
    leaks = []
    if not SCAFFOLD_DIR.exists():
        print(f"ERROR: scaffold dir not found at {SCAFFOLD_DIR}", file=sys.stderr)
        sys.exit(2)
    for filename in SHIPPED_FILES:
        path = SCAFFOLD_DIR / filename
        if not path.exists():
            print(f"WARN: shipped artefact missing: {path}", file=sys.stderr)
            continue
        leaks.extend(_scan_text(path.read_text(encoding="utf-8"), filename))
    return leaks


def _extract_our_block_shipped_content(full_text: str) -> str:
    """Mirror the loader logic in app/api/v2/onboard.py:_load_our_block_text.
    Only the slice between BEGIN/END markers ships to the customer; frontmatter
    + maintainer notes before/after stay internal. Returns the shipped slice or
    the whole file if markers not found (which would itself be a fail-safe
    behaviour matching the loader's fallback)."""
    begin_marker = "<!-- BEGIN OUR-BLOCK CONTENT"
    end_marker = "<!-- END OUR-BLOCK CONTENT"
    bi = full_text.find(begin_marker)
    ei = full_text.find(end_marker)
    if bi == -1 or ei == -1 or ei <= bi:
        # Markers missing — the loader falls back to inline; conservative here
        # is to scan the whole file so we still flag drift. But it's worth
        # noting in the report.
        return full_text
    line_end = full_text.find("\n", bi)
    if line_end == -1:
        return full_text
    return full_text[line_end + 1 : ei].strip()


def scan_our_block() -> list:
    leaks = []
    if not OUR_BLOCK_SOURCE.exists():
        print(f"WARN: OUR-block source not found at {OUR_BLOCK_SOURCE}", file=sys.stderr)
    else:
        full = OUR_BLOCK_SOURCE.read_text(encoding="utf-8")
        shipped = _extract_our_block_shipped_content(full)
        # Scan only the shipped slice — frontmatter + maintainer-notes outside
        # BEGIN/END markers stay internal and aren't part of what the customer sees.
        leaks.extend(_scan_text(shipped,
                                "0-MD/0-Documentation/internal/customer-onboard-our-block.md (shipped slice between BEGIN/END markers)"))
    # Extract OUR_BLOCK_TEXT_INLINE constant from onboard.py.
    if ONBOARD_PY.exists():
        text = ONBOARD_PY.read_text(encoding="utf-8")
        m = re.search(r'OUR_BLOCK_TEXT_INLINE\s*=\s*r?"""(.*?)"""', text, re.DOTALL)
        if m:
            inline_text = m.group(1)
            # Lineno reporting against the constant body (not the source file lineno);
            # caller can grep for the matched string in onboard.py if needed.
            for sub_leak in _scan_text(inline_text, "app/api/v2/onboard.py:OUR_BLOCK_TEXT_INLINE"):
                leaks.append(sub_leak)
    return leaks


def scan_live_agentnotes(base_url: str, project_slug: str) -> list:
    """Fetch live /agentnotes/{slug} JSON and walk its string values."""
    token = os.environ.get("VIBEFORGE_TOKEN")
    if not token:
        print("ERROR: --live requires VIBEFORGE_TOKEN env var", file=sys.stderr)
        sys.exit(2)
    url = f"{base_url.rstrip('/')}/agentnotes/{project_slug}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"ERROR: GET {url} -> HTTP {e.code}: {e.read().decode()[:200]}", file=sys.stderr)
        sys.exit(2)
    leaks: list = []
    _scan_json_value(data, "", leaks)
    return leaks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--our-block", action="store_true", help="Also scan OUR-block source + inline constant")
    parser.add_argument("--all", action="store_true", help="Scan all static surfaces (scaffold + OUR-block)")
    parser.add_argument("--live", metavar="BASE_URL", help="Also scan live /agentnotes JSON (e.g. https://vibeforge-dev.hydra.net.au)")
    parser.add_argument("--project", default="vibeforge-plus", help="Project slug for --live (default: vibeforge-plus)")
    args = parser.parse_args()

    do_scaffold = True
    do_our_block = args.our_block or args.all
    do_live = bool(args.live)

    all_leaks: list = []
    surface_count = 0

    if do_scaffold:
        all_leaks.extend(scan_scaffold())
        surface_count += len(SHIPPED_FILES)

    if do_our_block:
        all_leaks.extend(scan_our_block())
        surface_count += 2  # OUR-block source + inline constant

    if do_live:
        all_leaks.extend(scan_live_agentnotes(args.live, args.project))
        surface_count += 1

    if not all_leaks:
        print(f"OK: {surface_count} surface(s) scanned, all clean (no internal-jargon leaks).")
        return 0

    print(f"FAIL: {len(all_leaks)} internal-jargon leak(s) found across customer-facing surfaces:\n", file=sys.stderr)
    for source, lineno, match, line_text, why in all_leaks:
        loc = f"{source}:{lineno}" if lineno else source
        print(f"  {loc}", file=sys.stderr)
        print(f"    match:    {match!r}", file=sys.stderr)
        print(f"    context:  {line_text}", file=sys.stderr)
        print(f"    why:      {why}", file=sys.stderr)
        print(file=sys.stderr)
    print(
        "Each surface above is something a customer's agent / customer's terminal\n"
        "/ customer's editor will display verbatim. Internal references in them\n"
        "become dangling pointers for the customer (no project context to resolve\n"
        "them). Rewrite the offending lines using the 'drop the lead-in, keep the\n"
        "content' pattern: customer keeps the rule's full meaning; internal\n"
        "change-history (which version added it, which ticket drove it) gets\n"
        "stripped because customer can't dereference it. Internal audit lives in\n"
        "commit messages + FINDINGS doc + memory entries — surfaces customer\n"
        "doesn't see.\n",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
