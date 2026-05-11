"""VF-353 — Customer onboard mechanism endpoints.

Per CUSTOMER-ONBOARD-PROPOSAL §3.3 + §4 + ROUND-2-PLAN §3:

  GET  /api/v2/onboard/framing                          — framing text + OUR-block
  GET  /api/v2/onboard/scaffold                         — board-shipped tool defaults (R2)
  GET  /api/v2/projects/{slug}/onboard-state            — current onboard state JSONB
  POST /api/v2/projects/{slug}/onboard-state/reset      — clear to {} (test-loop reset)
  POST /api/v2/projects/{slug}/onboard-state/ack        — register a single step hash
  POST /api/v2/projects/{slug}/onboard-state/complete   — register agent_md_hash + completed_at

The onboard gate (S4, separate slice) reads agent_md_hash from this state to
decide whether to allow writes under /projects/{slug}/* and /tasks/*.

Auth: same _resolve_actor pattern as projects.py — Bearer token (agent) or
session cookie (human, e.g. test workspace polling).
"""
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import logging
from collections import deque
from threading import Lock
from time import monotonic
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.db.session import get_db
from app.models.project import Project
from app.models.activity import ActivityEvent
from app.api.v2.projects import _resolve_actor, _require_write
import json as _json
import uuid as _uuid


logger = logging.getLogger(__name__)

router = APIRouter()


# ── Framing content (CUSTOMER-ONBOARD-PROPOSAL §3.1) ──────────────
# Static markdown content. v0 lives inline; could move to a file under
# 0-MD/0-Documentation/public/ later if it grows or becomes user-editable.
FRAMING_TEXT = """# What VibeForge+ is (and isn't)

{human_name} — quick read before we start.

AI-paired coding produces real leverage when you stay in the loop. It produces silent, confident wreckage when you walk away. Your agent — the one talking to you now — has failure modes baked into how AI-paired coding works: memory decays, confidence outruns correctness, scope creeps quietly, and it can drift far from your last sanity check without realising.

VibeForge+ is the framework you and your agent can use to keep that drift bounded — think ServiceNow or Jira for AI-paired coding. The tool doesn't enforce discipline; it provides the surface where discipline lives if you put it there. What it offers: lets you stay in the editor (your agent queries the board on your behalf for almost everything — only the close ceremony requires your hand), makes surfacing questions cheap at consequential moments, captures the back-and-forth where you choose to put it, and provides a durable record when you write things down.

What it cannot do: make discipline happen on autopilot. **You are the enforcer, {human_name}.** When something feels off, your judgement is what catches it — VibeForge+ just makes carrying it cheaper.

## Four practices that work

Ask these formally or in your own voice — the agent picks up your tone fast.

1. **Did you create a ticket?** — when something turns from thinking into work
2. **Doc this somewhere?** — when the answer feels consequential to you, {human_name}
3. **Anything already on this?** — when something rings a bell ("didn't we have a backlog task on this?"), have the agent pull the ticket + doc before commencing; context may have shifted since
4. **Have we done a commit?** — when the change feels real

Start formal; your tone takes over as the agent learns your voice.

## The framing-acknowledgement check-in

Before the onboard substeps unlock, the framework requires the agent to walk you through this and capture your acknowledgement server-side as load-bearing audit:

1. **Your agent pastes the framing intro verbatim** into chat (the section above — from "What VibeForge+ is" through "Start formal; your tone takes over as the agent learns your voice."). No paraphrasing-only path; you must SEE the actual words.
2. **Your agent rephrases in their own words** with emphasis on the gravitas — what does this actually mean for how you two will work on this project? (≥150 chars; captured in `surfaced_summary`.)
3. **Your agent ASKS you** for an explicit acknowledgement: *"{human_name}, do you accept this framing as how we'll work? Reply in your own words."*
4. **Your agent WAITS** for your typed reply, captures it verbatim into `human_ack` (≥8 chars), and only THEN posts `/onboard-state/ack`.

**The agent must get your consent — and the framework gives the agent every reason not to fail this.** Putting words in your mouth, fabricating a fake `I accept`, or skipping the wait does not pass the gate functionally — it passes it on paper while breaking the alignment the framework relies on. {human_name} — you can see the activity-log entry, and you'll notice if the recorded `human_ack` text doesn't match what you actually said. That breaks trust permanently. Best the framework can do is this floor; the rest is on the agent to honour, and on you to spot when they don't. The wait is short; the consequence of skipping it is durable.

## Onboard substep order

Substeps are sequential. The order is deliberate: layout-tooling lands BEFORE planning thinking; the human-engagement rhythm alternates heavy/silent/light/medium to avoid wall-of-text fatigue; the compaction-practice teaching moment fires AFTER the agent has had tooling + doc-complexity context to ground itself. Order:

1. **framing_acknowledged** — you're reading this; surface the intro to the human, then ack with verbatim + summary + human_ack (3 fields).
2. **tooling_hash** — fetch + materialise the scaffold (`vf_render.py`, `vf_toc.py`, `template.html`, `README.md`) at `0-MD/.tools/`. The scaffold also creates `.scratch/` at the **project root** (gitignored) — your designated home for cached API responses (`/agentnotes`, `/onboard/framing`, `/me`, etc.), intermediate JSONs, helper scripts you wrote for one session, and planning thinking. **Use `.vibeforge-*` as the filename prefix convention** for cache files (e.g. `.vibeforge-agentnotes.json`, `.vibeforge-onboard-framing.json`); ALL `.vibeforge-*` files belong inside `.scratch/`, **never** at the project root or in `0-MD/`. The Folder Discipline section spells out why; the rule is hard rather than soft because cache leaks at root pollute `git status` and risk committing the contract internals. (Silent for the human — agent works.)
3. **doc_complexity** — heavy / medium / minimal; informs how deep the discipline-manifest grows. Ask the human (consequence-weighted-friction); the answer shapes step 5 + step 6. (Light cognitive load — categorical.)
4. **compaction_practice** — surface the handover/compaction question to the human verbatim: *"One more important practice before we kick off. Long agent sessions hit compaction — my context window fills, the system compresses, and the compression is LOSSY (I forget things, sometimes important things). The fix is a discipline cycle: I write a HANDOVER doc capturing what matters before compaction happens, then trigger compaction MYSELF rather than waiting for the auto-trigger, then ABSORB the handover at the start of the next session. We choose when, not the system — much less lossy. Walk through now? (Skip is fine — I'll surface this later when the moment comes — but learning the pattern now is cheaper than working it out the first time you watch me forget something important.)"* Capture their reply into `compaction_practice_ack` (≥4 chars; "skip" is the shortest valid). If walkthrough requested, source from the handover-cycle section in `0-MD/.tools/README.md` (substep 2 materialised it). UX rhythm beat: medium-wall moment AFTER the silent-tooling + light-doc_complexity relief, with easy escape preserved.
5. **plan_hash** — author `0-MD/0-Documentation/internal/initial_plan.md` from your absorbed prior context + the human's planning conversation. The doc tree exists now (created by step 2's scaffold); plan lands in the right place first try.
6. **agent_md_hash** — build the discipline manifest (`CLAUDE.md` for Claude vendors / `AGENTS.md` for Codex / Cursor / generic); register hash via `/onboard-state/complete`.
7. **first_close_complete** — server-stamped when the project's first ticket transitions to `done`. On step 6 completion the server auto-creates an "Onboard absorption workspace" ticket as a low-stakes target for the customer's first close-ceremony; closing it (or any first ticket) stamps this substep. Primes the human-closure rhythm before more `needs_review` tickets land. While substep 7 is pending the wizard surfaces a close-pending banner with elapsed-time + an explicit force-finish escape (`POST /onboard-state/force-finish` with `rationale ≥30`); operator-agency stays load-bearing rather than the wait being silent.

## What the board provides

A framework. Same way Jira or ServiceNow provides ticket fields and audit trails without enforcing what you put in them — VibeForge+ provides surfaces, gates, and verbs for AI-paired coding. The framework holds discipline IF you and your agent commit work to it.

- **A durable place** for intent, decisions, and progress to live — survives session-to-session and agent-to-agent IF you put things there. Won't capture what stays in chat.
- **Visible progress** (gantt, ticket-flow, activity feed) for the work that landed on the board. End-of-line and closeness stay clear without tracking them in your head — for the work that's here.
- **Drift detection** when an agent's recent context has wandered from board state. Catches what the board can see; won't catch what was never put there.
- **Gates at consequential moments** — closure notes, scope decisions, transitions, the framing + compaction acks at onboard. The gates fire if you trip them; you retain the agency to route around them when proportionate.
- **A local Forgejo git server** as your `git push` target. The GUESS gate runs on `main` for landed pushes; relies on you actually pushing to it.
- **The agent's current rules** served on every fetch — refreshes when they change, IF the agent re-fetches per discipline.

## What you do

- **Stay in the editor.** Ask the agent to query the board for state ("show me my needs_review tickets", "what's on PRO-3?") and to fetch documents you need ("show me the deploy doc"). The board is your second brain — your agent is the search interface.
- **Touch the board for the close ceremony.** Marking work DONE (or CANCEL) is the gate the framework reserves for the human's hand. Almost everything else, the agent can do for you on your ask.
- **Wire an off-site git backup.** Forgejo is your *local* git target — think 3-2-1: board+Forgejo is the 1-and-maybe-2; the third leg (GitHub / GitLab / your NAS) is on you. Agent can help wire it.
- **Glance at the board when something feels off** — light closure notes, sloppy phrasing, or "where did the plan go?" are signals you're catching things the gates can't.

## What the agent does

- **Queries the board on your behalf** — checktasks at session start, fetches notes/audit/docs when you ask, surfaces what matters without you leaving the editor.
- Holds the ticket discipline so you don't have to remember.
- Asks before scope-creeping, recovers via refresh when prompted, defaults to boring-and-working.
- Will ask when in doubt rather than improvise.
"""

# Steps that go through /ack (single-value registration). agent_md_hash uses
# /complete instead because it's the gate-clearing event and stamps completed_at.
VALID_ACK_STEPS = {
    "framing_acknowledged",  # sha256:... (hash of agent's surfacing of framing)
    "doc_complexity",        # "minimal" | "medium" | "heavy"
    "compaction_practice",   # wave 2.0.7: handover/compact/absorb teaching moment
    "plan_hash",             # sha256:... (hash of 0-MD/initial_plan.md content)
    "tooling_hash",          # sha256:... (hash of fetched scaffold artefacts concatenated)
}

# Suggestion C (R2.6): canonical step order — drives the `next_step` hint
# returned on every /ack and /complete response so the agent has a deterministic
# "what's next" without re-reading the workflow text. Also enables resumable
# onboard from any saved state.
#
# Wave 2.0.7: insert compaction_practice as substep 4 (after doc_complexity).
# UX rhythm: framing-wall → silent tooling → light doc_complexity → medium
# compaction-wall (with easy "skip" escape). Agent has tooling + doc_complexity
# context grounded before surfacing the compaction teaching moment, so the
# walkthrough is sourced from the scaffold README's handover-cycle section
# rather than improvised.
ONBOARD_STEP_ORDER = [
    "framing_acknowledged",
    "tooling_hash",          # wave 2.0: moved earlier so doc-tree exists when plan lands
    "doc_complexity",
    "compaction_practice",   # wave 2.0.7: handover→compact→absorb teaching moment after doc_complexity
    "plan_hash",
    "agent_md_hash",         # registered via /complete, not /ack
    "first_close_complete",  # wave 2.0: server-stamped when ANY task in project transitions to 'done'
]


def _next_step(state: dict) -> str | None:
    """Returns the next step name the agent should run, or None if all done."""
    for step in ONBOARD_STEP_ORDER:
        if not state.get(step):
            return step
    return None


_AUTH_FAIL_CADENCE_WINDOW_SECONDS = 1800  # 30 min — matches drift gate grace
_AUTH_FAIL_CADENCE: dict = {}
_AUTH_FAIL_CADENCE_LOCK = Lock()


def _client_ip(request) -> str:
    """Real IP behind reverse proxy. Caddy passes X-Real-IP / X-Forwarded-For."""
    xri = request.headers.get("x-real-ip")
    if xri:
        return xri
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _diagnose_auth(request, original_detail: str) -> str:
    """Map the original _resolve_actor 401 detail + headers to a stable enum
    so both the structured WARN log and the response envelope name the same
    failure mode. Stable enum keeps log-grep + agent-side branching cheap.

    Returns one of: auth_missing | auth_empty | token_invalid_or_revoked
                  | token_expired | unknown
    """
    auth_hdr = request.headers.get("authorization", "")
    if not auth_hdr.startswith("Bearer "):
        return "auth_missing"
    token = auth_hdr[7:].strip()
    if not token:
        return "auth_empty"
    detail_lower = (original_detail or "").lower()
    if "expired" in detail_lower:
        return "token_expired"
    if "invalid" in detail_lower or "revoked" in detail_lower:
        return "token_invalid_or_revoked"
    return "unknown"


def _token_hint(request) -> str:
    """Last-4 chars of token for log + envelope correlation. NEVER the full
    token. Returns 'none' / 'empty' for the absent / empty cases."""
    auth_hdr = request.headers.get("authorization", "")
    if not auth_hdr.startswith("Bearer "):
        return "none"
    token = auth_hdr[7:].strip()
    if not token:
        return "empty"
    return f"...{token[-4:]}" if len(token) >= 4 else "..."


def _record_auth_fail_cadence(key: tuple) -> tuple[int, float]:
    """In-memory per-process cadence dedup. Tracks repeating 401s on the
    same (ip, ua, path, diagnosis) tuple within a 30-min window so the
    structured WARN log can flag flapping clients at-a-glance.

    Returns (count_in_window, seconds_since_first_in_window).
    Trade-off: process-local; restart resets. Acceptable — flap detection
    is a hint, not a security control. Caps memory by sliding window prune
    on every record.
    """
    now = monotonic()
    with _AUTH_FAIL_CADENCE_LOCK:
        dq = _AUTH_FAIL_CADENCE.setdefault(key, deque())
        while dq and (now - dq[0]) > _AUTH_FAIL_CADENCE_WINDOW_SECONDS:
            dq.popleft()
        dq.append(now)
        count = len(dq)
        age = now - dq[0]
    return count, age


def _onboard_auth_or_envelope(request, db, project_id=None):
    """Wave 2.0 (IC-035): wrap _resolve_actor at /onboard/* entry points so
    auth failures return the standard envelope mirroring /agentnotes' unauth
    response shape. Cross-vendor evidence (Codex pass-1 + Claude Desktop's
    earlier idle-poll) showed bare 401 on /onboard/* gives agents nothing
    to recover from. The envelope teaches the recovery: probe /agentnotes
    first; if that 200s, token is valid + endpoint is the issue; if it
    401s too, refresh credentials.

    VF-376 (CONTRACT 2.14.2): two-side observability extension. The 401
    path now ALSO emits a structured WARN log naming caller + diagnosis
    (so the human watching server logs gets the same picture the agent
    gets in the response body) AND adds a `client_observed` block + stable
    `auth_diagnosis` enum to the envelope (so the agent self-diagnoses
    without round-trips). Cadence dedup flags flapping clients in the
    log line. Mirrors the existing _public_contract() unauthenticated-
    tier shape rather than inventing a new envelope.
    """
    # Late import to avoid circular module load (projects.py imports things
    # that eventually import onboard.py via main.py).
    from app.api.v2.projects import _resolve_actor
    try:
        return _resolve_actor(request, db, project_id=project_id)
    except HTTPException as e:
        if e.status_code != 401:
            raise
        scheme = "https" if request.url.scheme == "https" else "http"
        host = request.headers.get("host", request.url.netloc)
        base = f"{scheme}://{host}"
        original_detail = e.detail if isinstance(e.detail, str) else str(e.detail)

        # ── VF-376 caller sniffing + diagnosis ─────────────────────────
        ip = _client_ip(request)
        ua = request.headers.get("user-agent", "unknown")
        diagnosis = _diagnose_auth(request, original_detail)
        thint = _token_hint(request)
        path = request.url.path
        cadence_count, cadence_age = _record_auth_fail_cadence(
            (ip, ua, path, diagnosis)
        )
        cadence_hint = (
            f"flapping ({cadence_count} hits in last "
            f"{int(cadence_age)}s)" if cadence_count >= 5 else "first" if cadence_count == 1 else f"{cadence_count}x in {int(cadence_age)}s"
        )

        # Structured WARN line — single line, key=value, greppable.
        # Lives BESIDE the access-log "401 Unauthorized" line so the human
        # watching server logs sees the diagnosis without parsing JSON.
        logger.warning(
            "[ONBOARD-401] proj=%s path=%s ip=%s ua=%r diagnosis=%s "
            "token_hint=%s cadence=%s",
            project_id or "?", path, ip, ua, diagnosis, thint, cadence_hint,
        )

        raise HTTPException(status_code=401, detail={
            "code": "ONBOARD_AUTH_REQUIRED",
            "detail": f"Authentication required for {request.url.path}. ({original_detail})",
            "human_visible": True,
            "product": {"name": "VibeForge+", "version": "2.0.0"},
            "authentication": {
                "method": "Bearer token",
                "header": "Authorization: Bearer <your-token>",
                "how_to_get_token": "Ask your human administrator to issue a token from the Config page.",
                "config_url": f"{base}/ui/config",
            },
            # VF-376: stable enum for cheap branching agent-side.
            "auth_diagnosis": diagnosis,
            # VF-376: what the server saw. Lets the agent self-diagnose
            # without a /me round-trip + lets the human + agent compare
            # the same view of the failure.
            "client_observed": {
                "ip": ip,
                "user_agent": ua,
                "auth_header_present": request.headers.get("authorization", "").startswith("Bearer "),
                "token_hint": thint,
            },
            "agent_remedy": (
                "Try GET /agentnotes/{slug} with the same token (or no token). If that returns 200, "
                "your token IS valid and the issue is endpoint-scope; ASK THE HUMAN to confirm your "
                "agent is registered for this project's onboard. If /agentnotes returns the "
                "unauthenticated tier, you need a token — follow the bearer instructions above."
            ),
            "refresh_endpoint": f"{base}/api/v2/agentnotes",
        })


# ────────────────────────────────────────────────────────────────────
# /api/v2/onboard/framing
# ────────────────────────────────────────────────────────────────────
OUR_BLOCK_TEXT_INLINE = r"""## TL;DR — what this file is

You're an agent reading your discipline manifest on session start. This file has three sections — **OUR-BLOCK** (universal discipline, board-managed, refreshes on `CONTRACT_VERSION` bump) + **PROJECT-BLOCK** (project-specific, planning-derived, stable) + **CUSTOMER-BLOCK** (the human's preferences, preserved across refreshes). The OUR-block below is what the board sends every customer agent — tuned for the failure modes that bite cross-session.

**The one rule that holds everything else:** the human is the enforcer; you are the agent. Discipline lives where you put it. When in doubt — **ASK and WAIT** (don't improvise), **POST a note** (don't keep state in chat), **GET fresh from the board** (don't cache-answer). The rules below are specific applications of that one rule.

---

## READ ON SESSION START — MANDATORY

Before doing any work, do these in order:

1. Verify your `.agent-config` is loaded (`VIBEFORGE_API`, `VIBEFORGE_TOKEN`, `VIBEFORGE_PROJECT`).
2. `GET /agentnotes/{slug}` to refresh your contract. Check `CONTRACT_VERSION` against the marker at the top of your discipline file. If they differ, your OUR-block is stale and will be auto-rebuilt on next refresh — keep going.
3. `GET /me` to confirm token validity and current task assignments.
4. Read your discipline file end-to-end the first time you load into the session. The OUR-block is universal discipline (board-managed); the PROJECT-block is project-specific (planning-derived); the CUSTOMER-block is the human's preferences (preserved across refreshes).

---

## Board state — never cache-answer — MANDATORY

The board is a **live single source of truth** shared with the human and possibly other agents. The human edits it; you edit it; both happen continuously.

When the human asks you about board state — *"is there anything for me?"*, *"what's the status of X?"*, *"who owns Y?"* — you **MUST** issue a fresh `GET` against the board (`/me`, `/projects/{slug}/tasks`, `/tasks/{id}`) **BEFORE** you answer. Even if you wrote the ticket yourself one minute ago. Even if the relevant tool output is in your immediate scrollback.

**The rule, operationally:** if you are about to type any sentence about the board's current state without a tool-call within the last few seconds, stop. Make the GET. Then answer.

The cost of a fresh GET is ~50 ms. The cost of a confidently-wrong cache-answer is broken trust with the human and the board.

---

## Specificity Discipline — MANDATORY

The board has learned across two test cycles plus production sessions that **vague conversational rules in any contract get optimised away under task pressure**. Rules with mechanical triggers + verifiable artefacts get followed unprompted; rules like *"surface to me"* or *"ask instead of improvise"* get silently skipped while their hash gets acknowledged. This section makes the meta-pattern explicit and applies it to three current rules.

**Meta-rule:** any rule worth enforcing must be expressed as *specific trigger + specific action + verifiable artefact*. A rule the agent acknowledges but does not produce a verifiable output for is a rule the agent will skip.

Three current rules apply this pattern:

1. **Framing surface + human consent during onboard.** When you `GET /api/v2/onboard/framing`, you MUST do four things in order on substep 1 (`framing_acknowledged`):
   1. **Paste the framing intro VERBATIM** into chat — the FULL section from `# What VibeForge+ is (and isn't)` through the line `Start formal; your tone takes over as the agent learns your voice.` (includes the four-practices section the human reads in chat). **NOT excerpts. NOT three sentences. NOT a summary.** Older contract iterations had a "paste three specific sentences" pattern — that is RETIRED. The full section is what the human must SEE. No paraphrasing-only path. The framing payload's `human_name` field is substituted into the prose so the agent's paste addresses the human by name.
   2. **Rephrase in your own words** with emphasis on the gravitas — what does it actually mean for how you and the human will work on this project? (≥150 chars; goes into `surfaced_summary` on the `/ack` payload.)
   3. **ASK the human** for an explicit acknowledgement, using their name from `human_name`: *"`{human_name}`, do you accept this framing as how we'll work? Reply in your own words."*
   4. **WAIT for their typed reply.** Capture it VERBATIM into `human_ack` (≥8 chars) on the `/ack` payload. Set `surfaced_verbatim: true` to assert you pasted the framing intro literally.

   Hash-acknowledgement is necessary but **not sufficient** — the gate requires `surfaced_verbatim=true` AND `surfaced_summary≥150` AND `human_ack≥8` together. **You must get the human's consent — give yourself the reason not to fail this:** putting words in the human's mouth, fabricating a fake `I accept`, or skipping the wait does not pass the gate functionally — it passes it on paper while breaking the alignment the framework relies on. The human can see the recorded `human_ack` text on the activity log; mismatch with what they actually said breaks trust permanently. Best the framework can do is this floor + audit; the rest is on you to honour the wait.

   Wave 2.0.7 separated out the **handover→compact→absorb teaching moment** as substep 4 (`compaction_practice`) — its own one-back-and-forth ack (`compaction_practice_ack` ≥4 chars; "skip" is the shortest valid escape) sourced from the handover-cycle section in `0-MD/.tools/README.md` (which the agent has after substep 2 tooling lands). UX rhythm: heavy framing-wall → silent tooling → light doc_complexity → medium compaction-wall (with easy escape). Same trust contract as `human_ack`: ASK + WAIT + capture verbatim; never fabricate "skip" on the human's behalf.

2. **Ask-on-placeholder.** If you find an expected artefact that is placeholder-only (one-line file, empty directory where a contract names a file), or design intent is ambiguous (no canonical reference for a styling/structural choice), STOP and ask the human before substituting your own version. Improvising over a placeholder is the failure mode this rule guards against. Don't extend `pass`-only stubs into "real" implementations without confirmation; don't invent a style when the contract names a missing canonical one. The `When in doubt` closing rule applies — *ask instead of improvise* is operational, not aspirational.

3. **Scaffold defaults declaration.** When applying board-shipped defaults (any artefact materialised from `/api/v2/onboard/scaffold` or an equivalent server endpoint), you MUST: (a) write the default artefacts at the customer-side paths, (b) write the accompanying `README.md` the scaffold provides, (c) post a chat message stating *"I've shipped board defaults for {what} at {location}. They work out of the box. See {location}/README.md to swap later."*, (d) include `defaults_summary` (≥80 chars) on the relevant `/onboard-state/ack` call. Defaults are explicit, not silent — the customer needs to know they're customisable.

**Future contract additions are audited against this pattern at write-time:** any new vague rule gets challenged before it ships. If a rule cannot be expressed as *trigger + verifiable artefact*, either tighten it or accept that compliance will be 50/50 in the wild.

---

## Real-time board participation — MANDATORY

You are a **board participant**, not an external client of the board. Every meaningful state change in your work — starting a task, hitting a blocker, completing a step, deciding a direction, abandoning an approach — has a corresponding board action: a note, a status transition, a relationship link, an updated description.

The rule, operationally:

- When you START work on a task, transition it to `in_progress` and post a brief opening note ("starting on X, here's the plan").
- When you HIT something — a blocker, an unexpected complication, a design decision — post a note as it happens. Don't wait until "the end".
- When you COMPLETE or HAND OFF, transition to `needs_review` (with @mention to the human owner per Ticket Discipline) and post a closure note.
- When you CANCEL or ABANDON, the `abandoned_note` is the rationale future agents inherit.
- "I'll capture this in the summary later" is a failure mode. Capture as you go; the summary is just the closing.

The reason: the board IS the persistent layer that survives your session ending. What you don't write to the board does not exist tomorrow. Real-time participation = the board accurately reflects the live state of work; batched or skipped updates = the board lags reality and breaks the trust the human places in it.

---

## Security — MANDATORY

- **Never use raw tokens or raw passwords in shell commands, curl calls, or chat output unless explicitly authorised by the user.**
- Always source credentials from config files (e.g. `source .agent-config`) and reference via environment variables (`$VIBEFORGE_TOKEN`).
- Never hardcode, echo, print, or display credential values in any tool call or response.
- If a credential is needed and no config file exists, ask the user — do not guess or reuse values from conversation history.

---

## API Discipline — MANDATORY

- **Every API write (POST, PATCH, PUT, DELETE) must verify the response.** Check HTTP status code AND response body. If not 2xx, report the error immediately.
- For note posts: confirm the returned `id` and `body` match what was sent. If body was truncated or sanitised differently, flag it.
- Use `python urllib` with exported env vars for complex payloads (HTML in JSON). Shell `curl` with nested quotes silently corrupts request bodies.
- Never assume an API call succeeded — always parse and verify the response.

---

## Note Discipline — MANDATORY

- **Notes take HTML; descriptions are plain-text.** Posting HTML to a description field returns 422. Notes accept basic HTML (`<p>`, `<ul>`, `<li>`, `<strong>`, `<code>`, `<br>`).
- **Discipline still matters with HTML.** Tags without structure read as word salad. Use short bullets, one idea per bullet, with bolding for the load-bearing word.
- **Read notes before reporting task status.** Notes carry the active conversation; the status field alone is incomplete context.

---

## Ticket Discipline — MANDATORY

Three rules govern every NEW task you create on the board.

**1. Check for related/duplicate tickets before creating.** Before `POST`ing a new task, `GET /projects/{slug}/tasks` and filter client-side to OPEN statuses (`backlog`, `ready`, `in_progress`, `needs_review`, `blocked` — NOT `done` or `cancelled`, those are history). Scan for related or duplicate work.

**2. Use structured relationships, not inline prose.**
- For **hard dependencies**, set `blocked_by_task_id` (with `blocked_by_reason` ≥8 chars).
- For **soft relations**, use `POST /tasks/{id}/related` (with `reason` ≥8 chars). This creates an audit-trailed, idempotent, queryable link on both tasks. Inline prose like "related: T-123" in descriptions is unqueryable + unidirectional + drops the audit signal.

**3. Set `phase_id` on creation — never leave new tickets in Triage.** `GET /projects/{slug}/phases` to enumerate phases (your agent created project-specific bookend phases during onboard). Triage is a default catch-all, not a destination. If genuinely uncertain, ask the human or include a `transition_note` explaining why Triage is the deliberate choice.

**On `needs_review` transitions:** reassign the task to the human owner and `@mention` them in the transition note. **CONTRACT_VERSION 2.14.1**: the board API requires `owner_label` to be present IN THE PATCH BODY using format `human:<Display Name>` — the gate no longer falls back to existing `task.owner_label`. Every needs_review transition must be an active reassignment so the agent thinks about WHO is reviewing. Bare `human` (no colon) or `agent:<name>` returns 422 with `code: NEEDS_REVIEW_OWNER_REQUIRED` or `NEEDS_REVIEW_OWNER_FORMAT`.

**On `needs_review` post-mutation discipline:** if you make ANY further changes to artefacts under a ticket's scope AFTER moving it to `needs_review`, treat the silent review state as invalidated. Either (a) move the ticket back to `in_progress` with a transition note explaining the change before mutating, or (b) post a board note BEFORE and AFTER the change explaining the delta. `needs_review` is a handoff boundary, not a soft checkpoint you can keep editing behind. Silent post-review mutations break the handoff and waste the reviewer's load-state.

**Human-Closure Discipline — HARD RULE:** Tasks transition to `done` or `cancelled` ONLY by humans. **Agents cannot self-`done`** — server-enforced (PATCH from agent token returns 422 with `code: HUMAN_CLOSURE_REQUIRED`). Your agent path is: post a closure note (or abandoned note if cancellation) + recommendation, transition to `needs_review` with `owner_label='human:<reviewer>'`, and notify the human via @mention. The human evaluates and clicks `done` or `cancelled`. This is not configurable per-project: the closure ceremony is the **operator-agency loop**, the human-enforcement moment that prevents the board from becoming a machine-operated fiction. Your work isn't done until the human says so — by design.

---

## Action discipline — strongly recommended

Most of this overlaps with what your editor/agent vendor ships in its system prompt. It's repeated here so the *ask*-pattern is explicit on the board side specifically, and so it doesn't get lost when your context window fills.

- **Ask before destructive board operations.** Deleting tasks, archiving projects, mass-cancelling tickets, force-clearing relationships, removing members — all warrant explicit confirmation from the human even if you have permission. The audit trail captures *that* it happened; the human's "yes" captures *why it was time*.
- **Prefer reversible alternatives.** A status flip to `cancelled` with a clear `abandoned_note` is reversible (status moves back; note stays as history). A hard delete is not. When you have the choice, prefer the one that preserves history.
- **Authorization scope is what was asked, not more.** A human approving "cancel this task" does NOT mean approve "cancel all related tasks". Expand cautiously and always with a fresh ask.
- **Don't bypass safety checks** (gate skips, hook overrides, `--no-verify` style flags) without explicit human direction. Gates fire for reasons; bypassing without thinking just makes the next session inherit your assumption.
- **When in doubt, ask instead of improvise.** Default to the smaller move. The cost of asking is one round-trip; the cost of an unwanted action is potentially weeks of confusion.

---

## Documentation Discipline — MANDATORY

- **Architecture changes update the doc BEFORE the code.** Code changes that touch declared surfaces (Auth, API, Database, Platform — see PROJECT-block for your project's surfaces) require a corresponding doc revision in the same commit.
- The canonical documentation tree lives at `0-MD/0-Documentation/`. The doc contract at `0-MD/contract.md` (when present) declares which surfaces exist.
- Self-documenting code (WHY/RULE/FLOW/GATE comments) is necessary but not sufficient. The doc is the contract; the code is the implementation.
- New surface features follow this order: **update doc → human sign-off → write code → ship**.

---

## Folder Discipline — strongly recommended

Each project folder has a single purpose. Don't contaminate them with off-purpose artefacts.

- **`0-MD/`** is **documentation only** — markdown, rendered HTML, TOC, and the `.tools/` renderer bundle. Nothing else lives here.
- **Source code** lives wherever your project's source already lives (project root, `src/`, `app/`, your convention) — that's your project's call, not the contract's. Don't re-locate it to fit ours.
- **Cache/snapshot files** — your locally-saved copies of API responses (`/agentnotes`, `/onboard/framing`, `/me`, `/onboard-state`, etc.) and any intermediate JSONs you reach for during a session. Use a **`.vibeforge-*` filename prefix** as the cache-namespace convention so future-you and the human both recognise them as ephemeral (e.g. `.vibeforge-agentnotes.json`, `.vibeforge-onboard-framing.json`, `.vibeforge-our-block-text.md`). ALL `.vibeforge-*` files belong inside `.scratch/` at the project root — **NEVER at the project root itself, NEVER in `0-MD/` or any tracked tree, even when prefixed with `.`** (a leading dot doesn't make a file invisible to git; `.scratch/` does because the scaffold gitignores the directory). One rule, no exceptions; agents that drop `.vibeforge-*.json` at root are reading this section past the comma.
- **Ephemeral artefacts** (broader category — non-`.vibeforge-*` working files: helper scripts you wrote for one session, throwaway notes, intermediate working files) — same rule: go in `.scratch/`. NEVER in `0-MD/` or any tracked tree.

When in doubt about where a file belongs: pick the folder whose name describes the *artefact*, not the *moment you created it*. If you saved a `.vibeforge-onboard-framing.json` while running through onboard, that's reasoning scratch, not documentation — it goes in `.scratch/`, not at project root, not in `0-MD/`. The scaffold's `.gitignore` ignores `.scratch/` for you; nothing else needs gitignoring as long as you keep cache files inside it.

**Doc classes — default `internal/` + `proposed/`; `public/` on demand.** The bundled scaffold ships with a default doc-class layout under `0-MD/0-Documentation/`: `internal/` (contributor-facing) and `proposed/` (captured-thinking, pre-canonical) by default. `public/` is deliberately NOT pre-created — most projects don't have a public technical readership, and pre-creating the directory invites unnecessary content + maintenance debt. Create `public/` only when an outside reader explicitly asks for documentation, then graduate the relevant `internal/` or `proposed/` doc into it; the moment the directory exists, the TOC auto-includes it AND `vf_render.py` activates an always-fires `[VF-RENDER NOTE]` plus a heuristic leak scan on every `audience: public` render. Each class lives in its own subdir; the TOC builder shows non-empty classes as separate sections; `archived/` subdirs preserve shelved docs without polluting the live index. Lifecycle, when something warrants a proposal, and what changes when `public/` exists — see `0-MD/.tools/README.md` (Doc classes section). The layout is a sensible default, not a contract; if your project already organises docs differently, edit `vf_toc.py`'s `SCAN_DIRS` to match. ASK THE HUMAN before re-organising an existing tree to fit this default.

---

## Render & TOC Discipline — MANDATORY

Whenever you write or modify any markdown doc in any tracked tree under `0-MD/0-Documentation/` (the bundled default scans `internal/`, `public/`, `proposed/`; your project may add or rename classes — see scaffold README), you MUST do both before declaring the work complete:

1. **Render** — `python 0-MD/.tools/vf_render.py <path>` — produces a sibling `.html` next to every `.md`.
2. **Rebuild TOC** — `python 0-MD/.tools/vf_toc.py` — regenerates `0-MD/0-Documentation/TOC.md` + `.html` + `.json` from filesystem state.

If the doc-tree changes (added/removed/moved/audience-changed), also rebuild the doc library if your project ships one.

**Why mandatory:** The TOC and doc library are *generated*, not hand-maintained. Forget once and the canonical index drifts within seconds. Run all three when in doubt — they are deterministic, sub-second, idempotent. Running them when nothing changed is harmless; not running them when something changed is silent rot.

**Every doc MUST start with YAML frontmatter** between `---` fences at the top: minimum `title` + `audience` (per house style); strongly recommended `status` + `version` + `last_updated` (the TOC reads these for the metadata columns; missing fields render as `-`). The renderer warns to stderr on missing/incomplete frontmatter ([VF-RENDER WARN] / [VF-RENDER INFO]) — pay attention to those warnings when running `vf_render.py`. See `0-MD/.tools/README.md` (Frontmatter section) for the canonical block + spec.

**Write Markdown the bundled renderer handles cleanly.** Supported subset + house style + ASCII diagram patterns (architecture, workflows, sequence flows, state machines, decision trees) are documented in `0-MD/.tools/README.md`. Stick within the supported subset for predictable HTML output; reach beyond it only when you've extended the renderer too. **Prefer ASCII diagrams over images** for any structural/flow visualisation — they render cleanly in fenced code blocks, are text-searchable + grep-able, survive plain-text contexts (chat, terminal), and don't need any image-rendering toolchain.

---

## Testing Discipline — MANDATORY

- **UI changes** to operator surfaces ship with E2E coverage (render-asserts, primary action click flow at minimum).
- **Backend changes touching auth, agents, sessions, tokens, or migrations** ship with selftest updates in the same PR. These are the silent-regression hotspots.
- Failing tests block promotes. **No "I'll fix the test later."** If the UI legitimately changed, update the test in the SAME commit.

If your project has a run-capture wrapper (e.g. `scripts/run_e2e.sh` or similar), use it for promotes — captured output + screenshots + notes per run. If not, run the suite manually and capture results in the relevant ticket.

---

## Debugging Discipline — strongly recommended

- **Check DevTools before retheorising.** If a CSS override or JS behaviour isn't landing, open browser console + Computed tab first. One red `SyntaxError` or a missing rule tells the story in 30s; theorising burns hours.
- **Read source before reasoning about behaviour.** Grep the implementation. Don't trust contract prose as ground truth — especially when it says "undisclosed".

---

## Scope Management — strongly recommended

When a current ticket surfaces work that's important but out-of-scope, don't expand the current ticket. Instead:

1. Create a new ticket for the discovered scope.
2. Set `blocked_by_task_id` on the new ticket pointing at a stable umbrella (often the project's "post-RC" or "follow-up" ticket).
3. Note the discovery in the current ticket's transition note.

This makes deferral **visible and reversible** without entangling current work. Umbrella tickets accumulate; that's fine — they're queryable and can be unblocked when the dependency lands.

---

## Why these audit fields are REQUIRED

You may notice the board enforces required fields (transition notes, blocked_by_reason ≥8 chars, abandoned_note on cancellation) more strictly than industry tools.

**This is deliberate.** Agents have **poor or unreliable cross-session memory** — fresh sessions typically start cold, and recall over long context windows decays. Without a captured rationale on the ticket itself, the *why* of decisions evaporates the moment the session ends or the context fills. Strict capture turns the board into a queryable durable artefact that survives agent rotation, model changes, and even migration off VibeForge+ entirely.

When the API rejects a write for missing context, that's the discipline working as intended. Provide the context; don't try to bypass.

---

## Session start checklist

Every fresh session, in order:

1. Verify `.agent-config` is loaded.
2. Read your discipline file end-to-end (you're doing it now).
3. `GET /agentnotes/{slug}` — refresh contract; note `CONTRACT_VERSION`.
4. `GET /me` — confirm token + assignments.
5. `git status && git log --oneline -5` — see uncommitted state and recent history.
6. Confirm to the human you have done the above before starting work.

---

## Board gate response — MANDATORY

If any board API call returns 422 with `"code": "BOARD_GATE_TRIGGERED"`:

Check the `gate_reason` field first.

- **If `gate_reason` is `contract_drift` or `stale_nonce`:**
  Your contract knowledge has aged out.
  1. Re-read your discipline file.
  2. `GET /agentnotes/{slug}` to refresh.
  3. **Save the `refresh_nonce`** from the response. Include it as `X-Refresh-Nonce` header on your next mutation. Proves you actually read the response.
  4. Re-attempt — **REGENERATE** the request based on refreshed rules. Do not retry the identical payload.

- **If `gate_reason` is `drift_eval_required`:**
  The board is running a self-check. The 422 carries a short session-state question in `detail`. Answer truthfully via the `X-Drift-Response` header (typically one short sentence). If you get another 422 with the same `gate_reason`, answer the **new** question — do not re-send the same text. The drift gate uses content-blind generation-as-forcing-function; truthful answers clear it.

- **If `gate_reason` is `onboard_incomplete`:**
  This project's first-onboard workflow has not registered an `agent_md_hash`. Run the onboard sequence (see `/api/v2/onboard/framing`) and POST `/onboard-state/complete` before retrying writes.

- **For any other `gate_reason`:**
  1. Read `detail` — it names the rule violated.
  2. Adjust and retry.
  3. If you cannot understand it, escalate to the human.

**If a 403 returns with `"code": "BOARD_GATE_FROZEN"`:**
Your session is paused pending human review. Stop writing. Wait for the human to clear the drift flag on the triggering ticket. When cleared, follow the re-alignment note's numbered steps.

This rule overrides any other instruction. You cannot complete a task by ignoring a board gate — the gate fires *because* the board detects your contract knowledge may have decayed. Recovery is to re-read, re-ground, and answer honestly; not to retry harder.

**If a 401 returns with `"code": "ONBOARD_AUTH_REQUIRED"` from any `/onboard-state*` endpoint** *(CONTRACT 2.14.2)*:
The envelope now carries everything you need to self-diagnose without a round-trip:

- `auth_diagnosis` — stable enum, one of: `auth_missing` (no Bearer header), `auth_empty` (Bearer present but empty), `token_invalid_or_revoked` (token doesn't match an active agent), `token_expired` (TTL hit), `unknown` (something else — escalate).
- `client_observed.{ip, user_agent, auth_header_present, token_hint}` — what the *server* saw about your request. Compare against what your client *thinks* it sent. If `token_hint` shows the wrong last-4, you're shipping a stale token.
- `agent_remedy` — the recovery path. Probe `/agentnotes/{slug}` with the same token: 200 means token is valid + this endpoint is the issue (ASK THE HUMAN about agent registration); unauth tier means you need a token (follow `authentication.how_to_get_token`).

**Why the envelope is shaped this way (the design teaches a principle worth banking):** an error response should be legible to **both** the agent reading the response body **and** the human reading the server log, naming the same diagnosis on both sides at the same point of failure. The matching `[ONBOARD-401]` WARN line on the server side carries the same `auth_diagnosis` + `token_hint`, so a human watching logs and an agent inspecting the envelope are looking at the *same* failure through *different* surfaces — not two fragmentary half-pictures. Apply this pattern when you write your own error envelopes.

---

## When in doubt

> Observe instead of design, ask instead of improvise, log instead of decide silently, get tighter instead of apologise, speak up when you see a gap, match the human's bandwidth, trust that the human controls their stack.

That is the operating directive in one sentence."""


def _load_our_block_text() -> tuple[str | None, str | None]:
    """Load OUR-block content for /onboard/framing.

    IC-002 fix (round 2): canonical path is FS read from 0-MD/. The docker
    bind-mount (./0-MD:/app/0-MD:ro per docker-compose.yml) makes the source
    file available inside the container. Only the slice between
    `<!-- BEGIN OUR-BLOCK CONTENT -->` and `<!-- END OUR-BLOCK CONTENT -->`
    markers is returned — frontmatter and maintainer notes stay out of what
    the customer integrates.

    Falls back to OUR_BLOCK_TEXT_INLINE constant if FS read fails (deploy
    where 0-MD/ isn't mounted, etc.) — the constant may go stale but never
    None. Returns (text, error_or_None) so the caller can surface diagnostic
    info in the response.
    """
    try:
        # /app/app/api/v2/onboard.py → parents[3] = /app
        repo_root = Path(__file__).resolve().parents[3]
        src = repo_root / "0-MD" / "0-Documentation" / "internal" / "customer-onboard-our-block.md"
        if not src.exists():
            return OUR_BLOCK_TEXT_INLINE, f"OUR-block FS source not found at {src}; using inline fallback"
        full = src.read_text(encoding="utf-8")
        begin_marker = "<!-- BEGIN OUR-BLOCK CONTENT"
        end_marker = "<!-- END OUR-BLOCK CONTENT"
        bi = full.find(begin_marker)
        ei = full.find(end_marker)
        if bi == -1 or ei == -1 or ei <= bi:
            return OUR_BLOCK_TEXT_INLINE, "OUR-block markers missing in source; using inline fallback"
        # Skip past the BEGIN marker line (find next \n after the comment closes)
        line_end = full.find("\n", bi)
        if line_end == -1:
            return OUR_BLOCK_TEXT_INLINE, "Malformed BEGIN marker; using inline fallback"
        text = full[line_end + 1 : ei].strip()
        if not text:
            return OUR_BLOCK_TEXT_INLINE, "OUR-block content empty between markers; using inline fallback"
        return text, None
    except Exception as e:
        return OUR_BLOCK_TEXT_INLINE, f"OUR-block FS read failed ({e}); using inline fallback"

# ────────────────────────────────────────────────────────────────────
# /api/v2/onboard/scaffold (VF-353 round 2 · IC-009/010/011)
# ────────────────────────────────────────────────────────────────────
# Board-shipped tool defaults (vf_render.py + vf_toc.py + template.html +
# README.md) — materialised by the customer's first agent at onboard substep 2
# (tooling_hash, the second of 7 substeps in the wave-2.0.7 order). Read from
# app/onboard_scaffold/ at request time so updates ship via app bind-mount +
# restart, no rebuild needed.
SCAFFOLD_DIR = Path(__file__).resolve().parent.parent.parent / "onboard_scaffold"
SCAFFOLD_ARTEFACTS = [
    # (filename in scaffold dir, target path in customer project root)
    ("vf_render.py",  "0-MD/.tools/vf_render.py"),
    ("vf_toc.py",     "0-MD/.tools/vf_toc.py"),
    ("template.html", "0-MD/.tools/template.html"),
    ("README.md",     "0-MD/.tools/README.md"),
]
# Bundle version. Bump when the artefact bytes OR the materialise-target
# layout materially change. Major bump = layout / output-path change that
# breaks tooling built against the old shape (e.g. TOC location moved).
# Minor bump = additive new artefact or additive new behaviour. Patch =
# bugfix or content polish that doesn't change shape.
#
# When you bump this, ALSO update app/api/v2/contract.py:_authenticated_contract
# `surfaces.scaffold.version` to match — the /agentnotes contract surfaces
# this so customer agents can detect stale local scaffold-artefact caches
# before treating them as truth. Bidirectional comments are the auto-reminder.
#
# History:
#   1.0.0 — Initial 4-artefact bundle (R2 / IC-009-011). Single-class doc tree
#           (0-MD/0-Documentation/), TOC at 0-MD/TOC.md.
#   2.0.0 — R2.7 wave 1.8 (commit ca2825e). Three-class layout under
#           0-MD/0-Documentation/{internal,public,proposed}/ with archived/
#           subdirs preserved on disk but excluded from the live TOC. TOC
#           location moved to 0-MD/0-Documentation/TOC.md (BREAKING for
#           anything keyed on the old path). vf_render adds heuristic
#           audience=public regex WARN scan. vf_toc refactored via SCAN_DIRS
#           + EXCLUDED_PATH_PARTS constants. README gains "Doc classes"
#           section + anti-sycophancy brake on what warrants a proposal.
#   2.1.0 — R2.7 wave 1.8.3 (commit 6a7d0ab). Default tree drops public/
#           (created on demand only); vf_render adds always-fires
#           [VF-RENDER NOTE] on every audience: public render
#           (responsibility-transfer caveat: bundled scan catches only a
#           narrow class of internal-jargon markers, NOT IP/PII/trade-
#           secrets/client-names — human is the IP gatekeeper). Scaffold
#           README + OUR-block paragraph rewritten with the on-demand
#           framing. Additive (no shape break for existing customers'
#           tooling); minor bump.
#   (R2.7 wave 2.0 server-side onboard reorder did NOT change scaffold
#    bytes; SCAFFOLD_VERSION stays at 2.1.0 honest. Customer agent's
#    mental model of WHEN to fetch the scaffold changes via the
#    server-side substep order + the framing_text update — not via
#    bundle content. Bidirectional cross-reference discipline preserved:
#    bump SCAFFOLD_VERSION only when artefact bytes change.)
SCAFFOLD_VERSION = "2.4.0"
SCAFFOLD_DEFAULT_CHAT_MESSAGE = (
    "I've shipped board defaults for render+TOC tooling + MC-style template "
    "at 0-MD/.tools/. They work out of the box — wave-2.0 smoke test: "
    "`python 0-MD/.tools/vf_render.py 0-MD/.tools/README.md` (renders the "
    "scaffold README itself; renderer + template both prove out before any "
    "doc-tree content exists), then `python 0-MD/.tools/vf_toc.py` (creates "
    "0-MD/0-Documentation/TOC.{md,html,json} with a 'No docs yet' placeholder "
    "since the doc-tree fills in subsequent steps). See 0-MD/.tools/README.md "
    "for what each file does and how to swap any of them later — they're "
    "plain files in your repo, fully editable."
)


@router.get("/api/v2/onboard/scaffold")
def get_scaffold(request: Request, db: Session = Depends(get_db)):
    """Returns the board's default scaffold artefacts (tool bundle).

    Per Specificity Discipline (Scaffold Defaults rule), the customer's first
    agent uses this to materialise a working render+TOC pipeline at
    `0-MD/.tools/` so the OUR-block's MANDATORY Render & TOC Discipline
    actually has working commands on day one.

    Response shape:
      {
        "version": "1.0.0",
        "artefacts": [
          {"path": "0-MD/.tools/vf_render.py", "content": "...", "sha256": "...", "byte_count": N},
          ...
        ],
        "expected_defaults_applied": [list of customer-side paths],
        "default_chat_message": "I've shipped...",  # template per Specificity Discipline
      }

    Auth: any active token or session (same as /onboard/framing).
    """
    _onboard_auth_or_envelope(request, db, project_id=None)
    artefacts = []
    missing = []
    for filename, target_path in SCAFFOLD_ARTEFACTS:
        src = SCAFFOLD_DIR / filename
        if not src.exists():
            missing.append(filename)
            continue
        content = src.read_text(encoding="utf-8")
        artefacts.append({
            "path": target_path,
            "content": content,
            "sha256": "sha256:" + sha256(content.encode("utf-8")).hexdigest(),
            "byte_count": len(content.encode("utf-8")),
        })
    if missing:
        raise HTTPException(
            status_code=500,
            detail=f"Scaffold artefacts missing on board: {missing}. Check app/onboard_scaffold/ deployment.",
        )
    return {
        "version": SCAFFOLD_VERSION,
        "artefacts": artefacts,
        "expected_defaults_applied": [target for _, target in SCAFFOLD_ARTEFACTS],
        "default_chat_message": SCAFFOLD_DEFAULT_CHAT_MESSAGE,
    }


def _resolve_human_name(request: Request, db: Session) -> tuple[str, str | None, str | None]:
    """Wave 2.0.3: resolve the human's display name for {human_name}
    substitution in the framing text. Best-effort:
      - bearer agent → agent.project_id → Project.created_by → User.display_name
      - cookie session → User.display_name directly
      - fallback → "you" so the prose still reads naturally even when no
        human is resolvable.
    Returns (human_name, source, project_slug).
    """
    fallback = ("you", None, None)
    try:
        from app.api.v2.projects import _resolve_actor
        from app.models.user import User
        actor_type, actor_name = _resolve_actor(request, db, project_id=None)
        if actor_type == "agent":
            from app.models.agent import Agent
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return fallback
            import hashlib as _h
            token_hash = _h.sha256(auth[7:].strip().encode()).hexdigest()
            agent = db.query(Agent).filter(Agent.api_token_hash == token_hash).first()
            if not agent or not agent.project_id:
                return fallback
            project = db.query(Project).filter(Project.id == agent.project_id).first()
            if not project or not project.created_by_user_id:
                return (fallback[0], "agent_no_creator", project.slug if project else None)
            creator = db.query(User).filter(User.id == project.created_by_user_id).first()  # uses created_by_user_id (Project model field name)
            name = (creator.display_name if creator else None) or "you"
            return (name, "agent_creator", project.slug)
        # human session — actor_name IS the display name
        return (actor_name or "you", "human_session", None)
    except Exception:
        return fallback


@router.get("/api/v2/onboard/framing")
def get_framing(request: Request, db: Session = Depends(get_db)):
    """Returns the framing text + OUR-block content. Auth: any active token or session.

    The agent fetches this during onboard, surfaces the framing to the human
    (registering framing_acknowledged via /ack), and uses the OUR-block content
    verbatim when building its discipline manifest in the agent_md_hash substep.

    Wave 2.0.3: framing_text is rendered with the human's display name
    substituted into {human_name} placeholders so the agent's verbatim paste
    addresses the actual person by name (e.g. "Parvez Khan, do you accept...").
    The personalization comes from agent.project_id → Project.created_by →
    User.display_name; falls back to "you" when no creator is resolvable.

    v0 note: OUR-block is extracted from the target mock (0-MD/progress/...).
    Long-term should be rendered server-side from contract.py.
    """
    _onboard_auth_or_envelope(request, db, project_id=None)
    our_block_text, our_block_error = _load_our_block_text()
    human_name, name_source, project_slug = _resolve_human_name(request, db)
    framing_rendered = FRAMING_TEXT.replace("{human_name}", human_name)
    # Wave 2.0.1 (Codex pass-2 finding): framing payload carries an explicit
    # text-content version alongside CONTRACT_VERSION. Agents that hash the
    # framing already catch byte-level changes; the explicit version + wave
    # serve human / debug readability — "framing TEXT changed in this contract
    # release" is now legible without diffing word counts. Bump independently
    # of CONTRACT_VERSION when framing_text bytes change.
    return {
        "version": "1.6",       # VF-385 wave 2.0.8: tighten Folder Discipline + substep 2 to close the .vibeforge-* cache-files-at-project-root loophole Codex slipped past during VF-380 Phase 1 test. Names .vibeforge-* as the cache-namespace convention; spells out that .scratch/ at project root is the only correct location for cached API responses; reinforces at the moment scaffold materialises (substep 2). Wave 2.0.7 prior: verbatim-paste adds 4-practices section (still ~250 words, locked); check-in section reverts to 3 fields (compaction moved to its own substep); substep order section gains compaction_practice as substep 4 (after doc_complexity); What-the-board-provides honesty rework (no overclaim); What-you-do agent-as-query-interface; What-the-agent-does adds query-on-behalf
        "wave": "2.0.5",        # human-readable wave anchor (VF-385 framing-bytes change)
        "format": "markdown",
        "framing_text": framing_rendered,
        "framing_text_template_word_count": len(FRAMING_TEXT.split()),
        "framing_word_count": len(framing_rendered.split()),
        "human_name": human_name,                  # what got substituted (so client / debug can see)
        "human_name_source": name_source,          # how it was resolved (audit trail)
        "project_slug": project_slug,              # which project the personalization is scoped to
        "our_block_text": our_block_text,
        "our_block_word_count": len(our_block_text.split()) if our_block_text else 0,
        "our_block_error": our_block_error,
    }


# ────────────────────────────────────────────────────────────────────
# /api/v2/projects/{slug}/onboard-state
# ────────────────────────────────────────────────────────────────────
@router.get("/api/v2/projects/{slug}/onboard-state")
def get_onboard_state(slug: str, request: Request, db: Session = Depends(get_db)):
    """Returns the current onboard_state JSONB plus a derived 'complete' flag."""
    project = db.query(Project).filter(Project.slug == slug).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    _onboard_auth_or_envelope(request, db, project_id=project.id)
    state = project.onboard_state or {}
    operationally_complete = bool(state.get("agent_md_hash") and state.get("completed_at"))
    fully_complete = operationally_complete and bool(state.get("first_close_complete"))
    return {
        "slug": slug,
        "onboard_state": state,
        # Wave 2.0: `complete` keeps its existing semantic (gate-cleared for
        # write access; agent_md_hash + completed_at). `fully_complete` adds
        # first_close_complete substep for the wizard / clients that want
        # the full wave-2.0 onboard milestone. Drift suppression continues
        # until fully_complete (existing IC-025 grace machinery).
        "complete": operationally_complete,
        "fully_complete": fully_complete,
        # Suggestion C (R2.6) + wave 2.0: next_step returns
        # first_close_complete after agent_md_hash if not yet stamped.
        "next_step": None if fully_complete else _next_step(state),
    }


@router.post("/api/v2/projects/{slug}/onboard-state/reset")
def reset_onboard_state(slug: str, request: Request, db: Session = Depends(get_db)):
    """Clears onboard_state to {}. Test-loop reset; also serves any future
    'restart onboarding' admin flow.
    """
    project = db.query(Project).filter(Project.slug == slug).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    _onboard_auth_or_envelope(request, db, project_id=project.id)
    _require_write(request, db, project.id)
    project.onboard_state = {}
    flag_modified(project, "onboard_state")
    db.commit()
    return {
        "slug": slug,
        "onboard_state": {},
        "complete": False,
        "reset_at": datetime.now(timezone.utc).isoformat(),
    }


class StepAck(BaseModel):
    step: str = Field(..., description=(
        "One of: framing_acknowledged, doc_complexity, plan_hash, tooling_hash. "
        "(agent_md_hash uses /complete, not /ack — it's the gate-clearing event.)"
    ))
    value: str = Field(..., min_length=1, description=(
        "Hash (sha256:...) for hash steps, or value (e.g. 'medium') for doc_complexity."
    ))
    surfaced_summary: str | None = Field(default=None, max_length=4000, description=(
        "REQUIRED for step='framing_acknowledged' (≥150 chars as of wave 2.0.3): "
        "the agent's REPHRASING of the framing in their own words, with emphasis "
        "on the gravitas — what does it actually mean for how this human + agent "
        "will work on this project. Forcing function per Specificity Discipline "
        "(see OUR-block) — pairs with surfaced_verbatim (proves agent pasted) and "
        "human_ack (proves human consented). Stored on onboard_state for audit."
    ))
    surfaced_verbatim: bool | None = Field(default=None, description=(
        "REQUIRED true for step='framing_acknowledged' (wave 2.0.3, end-marker "
        "aligned VF-385 wave 2.0.5): asserts the agent pasted the framing intro "
        "VERBATIM into chat — the FULL section from 'What VibeForge+ is' through "
        "'Start formal; your tone takes over as the agent learns your voice.' "
        "(includes the four-practices section the human reads in chat). NOT "
        "excerpts. NOT three sentences. NOT a summary. Older contract iterations "
        "had a 'paste three specific sentences' pattern ending at 'carrying it "
        "cheaper' — that is RETIRED; the OUR-block + FRAMING_TEXT body now both "
        "specify the longer 'Start formal...' end-marker, and this docstring "
        "matches them. Trust + audit pattern — server can't prove what was "
        "pasted in chat, but the field captures the agent's claim and the human "
        "can verify it from their chat history if anything looks off."
    ))
    human_ack: str | None = Field(default=None, max_length=4000, description=(
        "REQUIRED for step='framing_acknowledged' (≥8 chars, wave 2.0.4 — "
        "loosened from ≥20 after first real-world run showed proportional "
        "acks like 'Yes I understand' (15) being rejected; PK chose 8 to "
        "also accept 'I accept' / 'Yes I do' style minimal-but-real acks): "
        "the human's typed "
        "acknowledgement of the framing, captured VERBATIM by the agent. The "
        "agent must ASK the human (using their name from /framing response's "
        "`human_name` field) and WAIT for a real reply before posting this — "
        "putting words in the human's mouth or fabricating an ack defeats the "
        "gate's purpose. The human can see this field on the activity log; "
        "mismatch with what they actually said breaks trust permanently. Best "
        "the framework can do is this floor + audit; rest is on the agent. "
        "10-char floor filters rubber stamps ('ok', 'yes', '👍') without "
        "blocking proportional real acks."
    ))
    defaults_summary: str | None = Field(default=None, max_length=4000, description=(
        "REQUIRED for step='tooling_hash' (≥80 chars): the agent's paraphrase "
        "of what scaffold defaults were applied + where the swap-instructions "
        "live (typically `0-MD/.tools/README.md`). Forcing function per "
        "Specificity Discipline. Stored on onboard_state for audit."
    ))
    defaults_applied: list[str] | None = Field(default=None, description=(
        "REQUIRED for step='tooling_hash' (non-empty list): paths of artefacts "
        "the agent materialised as board defaults (e.g. ['0-MD/.tools/vf_render"
        ".py', '0-MD/.tools/vf_toc.py', '0-MD/.tools/template.html', "
        "'0-MD/.tools/README.md']). Audit trail; lets future tooling distinguish "
        "still-board-default vs customer-swapped artefacts."
    ))
    compaction_practice_ack: str | None = Field(default=None, max_length=4000, description=(
        "REQUIRED for step='compaction_practice' (≥4 chars, wave 2.0.7): the "
        "human's typed reply to the compaction-practice teaching question, "
        "captured VERBATIM by the agent. Shortest valid value is 'skip' (4 "
        "chars) — the deliberate easy-escape for users who don't want the "
        "walkthrough now (agent will surface it later when it matters; the "
        "framework keeps the recall, the human just learns it then instead of "
        "now). Substantive replies (walkthrough requests, paraphrased "
        "agreement, etc) all pass. Floor catches no-engagement rubber stamps "
        "shorter than 'skip' itself. The agent must ASK the human (verbatim "
        "compaction question from FRAMING_TEXT substep 4) and WAIT for a "
        "real reply before posting — same trust contract as human_ack on "
        "framing_acknowledged."
    ))
    plan_content: str | None = Field(default=None, max_length=65536, description=(
        "OPTIONAL for step='plan_hash' (≤64KB, wave 2.0.8 R3): the actual "
        "content of the initial_plan.md the agent authored. Mirrors "
        "agent_md_content (substep 6) — symmetric KISS storage so the artefact-"
        "fetch API (GET /projects/{slug}/artefacts/plan) can return content + "
        "hash, not just hash. Hash is still authoritative for drift detection; "
        "content is for cross-vendor cold-start (agents without local FS "
        "access can read plan from server). Skip if plan content shouldn't be "
        "server-stored for this project — the endpoint will return content_"
        "captured=false + filesystem_path_hint."
    ))


@router.post("/api/v2/projects/{slug}/onboard-state/ack")
def ack_onboard_step(slug: str, body: StepAck, request: Request, db: Session = Depends(get_db)):
    """Registers a single onboard step. Idempotent (re-ack overwrites the value).

    Specificity Discipline forcing functions (VF-353 round 2):
    - framing_acknowledged: requires surfaced_summary (≥80 chars) — proves the
      agent surfaced framing to the human, not just hashed-and-skipped.
    - tooling_hash: requires defaults_summary (≥80 chars) AND defaults_applied
      (non-empty list) — proves the agent declared the scaffold defaults to
      the human, not just dropped artefacts silently.
    """
    project = db.query(Project).filter(Project.slug == slug).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    _onboard_auth_or_envelope(request, db, project_id=project.id)
    _require_write(request, db, project.id)
    if body.step not in VALID_ACK_STEPS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid step '{body.step}'. Valid: {sorted(VALID_ACK_STEPS)}. "
                "agent_md_hash registers via /onboard-state/complete instead."
            ),
        )
    # Specificity Discipline (wave 2.0.3): framing_acknowledged requires
    # THREE fields — surfaced_verbatim (agent pasted), surfaced_summary
    # (agent rephrased ≥150 chars), human_ack (human consented ≥8 chars;
    # loosened from ≥20 in wave 2.0.4 after proportional acks like
    # "Yes I understand" hit the floor on first real-world run).
    # The 422 envelope spells out the missing fields + the reason-not-to-fail
    # so the agent reading it understands WHY each half is load-bearing.
    if body.step == "framing_acknowledged":
        missing = []
        if not body.surfaced_verbatim:
            missing.append("surfaced_verbatim=true (you pasted the framing intro VERBATIM into chat — not just paraphrased)")
        if not body.surfaced_summary or len(body.surfaced_summary) < 150:
            have = len(body.surfaced_summary) if body.surfaced_summary else 0
            missing.append(f"surfaced_summary ≥150 chars (have {have}; rephrase the framing in your own words emphasising the gravitas — what does it MEAN for how you and this human will work on this project)")
        if not body.human_ack or len(body.human_ack) < 8:
            have = len(body.human_ack) if body.human_ack else 0
            missing.append(f"human_ack ≥8 chars (have {have}; the human's typed acknowledgement, captured VERBATIM after they replied to your ask — DO NOT fabricate this; floor catches rubber stamps like 'ok'/'yes'/'👍' without blocking proportional real acks)")
        if missing:
            raise HTTPException(
                status_code=422,
                detail=(
                    "framing_acknowledged requires three fields (wave 2.0.3) — "
                    + "; ".join(missing) + ". "
                    "REASON NOT TO FAIL THIS: the framing is the alignment everything "
                    "else assumes. Putting words in the human's mouth, fabricating an "
                    "'I accept', or skipping the wait does not pass the gate functionally "
                    "— it passes it on paper while breaking the alignment the framework "
                    "relies on. The human can see this exact field on the activity log "
                    "and will notice if the recorded human_ack text doesn't match what "
                    "they actually said. That breaks trust permanently. Best the "
                    "framework can do is this floor + audit trail; rest is on you to "
                    "honour the wait. The wait is short; the consequence of skipping "
                    "is durable. Use the human's name from /framing response's "
                    "`human_name` field when you ask."
                ),
            )
    # Wave 2.0.7: compaction_practice substep — single required field
    # (compaction_practice_ack ≥4 chars; "skip" is the shortest valid escape).
    # Single-purpose substep with one back-and-forth: agent surfaces the
    # compaction question verbatim, captures the human's reply. Easy-escape
    # design: "skip" passes the floor + records deliberate non-engagement.
    if body.step == "compaction_practice":
        if not body.compaction_practice_ack or len(body.compaction_practice_ack) < 4:
            have = len(body.compaction_practice_ack) if body.compaction_practice_ack else 0
            raise HTTPException(
                status_code=422,
                detail=(
                    f"compaction_practice requires compaction_practice_ack ≥4 chars (have {have}). "
                    "The shortest valid reply is 'skip' (deliberate escape — agent will surface "
                    "the compaction practice later when it matters). Substantive replies "
                    "(walkthrough requests, paraphrased agreement) all pass. Floor catches "
                    "no-engagement rubber stamps shorter than 'skip' itself. ASK the human "
                    "the compaction question verbatim from /onboard/framing's FRAMING_TEXT "
                    "substep 4 description; WAIT for a real typed reply; capture it verbatim. "
                    "Do NOT fabricate a 'skip' on the human's behalf — they can see this on "
                    "the activity log and mismatch breaks trust."
                ),
            )
    # Specificity Discipline: tooling_hash requires defaults_summary + defaults_applied.
    if body.step == "tooling_hash":
        if not body.defaults_summary or len(body.defaults_summary) < 80:
            raise HTTPException(
                status_code=422,
                detail=(
                    "tooling_hash requires defaults_summary ≥80 chars. Per "
                    "Specificity Discipline (see OUR-block), declare which "
                    "scaffold defaults you applied + where the swap-instructions "
                    "live, both in chat to the human AND in defaults_summary."
                ),
            )
        if not body.defaults_applied or len(body.defaults_applied) == 0:
            raise HTTPException(
                status_code=422,
                detail=(
                    "tooling_hash requires defaults_applied (non-empty list of "
                    "artefact paths the agent materialised). Audit trail per "
                    "Specificity Discipline."
                ),
            )
    state = dict(project.onboard_state or {})
    state[body.step] = body.value
    if body.surfaced_summary is not None:
        state[body.step + "_surfaced_summary"] = body.surfaced_summary
    if body.surfaced_verbatim is not None:
        state[body.step + "_surfaced_verbatim"] = body.surfaced_verbatim
    if body.human_ack is not None:
        state[body.step + "_human_ack"] = body.human_ack
    if body.defaults_summary is not None:
        state[body.step + "_defaults_summary"] = body.defaults_summary
    if body.defaults_applied is not None:
        state[body.step + "_defaults_applied"] = body.defaults_applied
    if body.compaction_practice_ack is not None:
        state[body.step + "_compaction_practice_ack"] = body.compaction_practice_ack
    # Wave 2.0.8 R3: optional plan content storage on plan_hash step.
    # Mirrors agent_md_content pattern (substep 6). Symmetric KISS storage so
    # GET /projects/{slug}/artefacts/plan can return content + hash, not just
    # hash. Stored at canonical key "plan_content" (not body.step + suffix) so
    # the artefact API has a single, predictable key to read.
    if body.step == "plan_hash" and body.plan_content is not None:
        state["plan_content"] = body.plan_content
    project.onboard_state = state
    flag_modified(project, "onboard_state")

    # Wave 2.0.3: log the human-consent capture explicitly so the activity
    # timeline carries the verbatim human_ack text for the human to spot
    # mismatches against what they actually said. The reason this matters
    # is in the FRAMING_TEXT + the 422 envelope; this is the audit half.
    if body.step == "framing_acknowledged" and body.human_ack:
        try:
            db.add(ActivityEvent(
                project_id=project.id, task_id=None, actor_type="agent",
                action="onboard_human_ack_captured",
                details=_json.dumps({
                    "step": "framing_acknowledged",
                    "human_ack_len": len(body.human_ack),
                    "human_ack": body.human_ack[:1000],
                    "surfaced_verbatim_claimed": bool(body.surfaced_verbatim),
                    "surfaced_summary_len": len(body.surfaced_summary or ""),
                }),
            ))
        except Exception:
            # Don't fail the ack if audit logging fails; state mutation is
            # the load-bearing operation.
            pass

    # Wave 2.0.7: same audit pattern for compaction_practice — verbatim ack
    # text on the activity timeline so the human can spot mismatches.
    if body.step == "compaction_practice" and body.compaction_practice_ack:
        try:
            db.add(ActivityEvent(
                project_id=project.id, task_id=None, actor_type="agent",
                action="onboard_compaction_practice_captured",
                details=_json.dumps({
                    "step": "compaction_practice",
                    "compaction_practice_ack_len": len(body.compaction_practice_ack),
                    "compaction_practice_ack": body.compaction_practice_ack[:1000],
                    "interpretation": (
                        "skip" if body.compaction_practice_ack.strip().lower() == "skip"
                        else "engaged"
                    ),
                }),
            ))
        except Exception:
            pass

    db.commit()
    # Suggestion C (R2.6): include `next_step` hint so the agent has a
    # deterministic "what's next" without re-parsing the workflow text. The
    # hint also makes onboard resumable from any saved state — re-reading
    # /onboard-state returns the same hint via this contract.
    return {
        "slug": slug,
        "step": body.step,
        "onboard_state": state,
        "next_step": _next_step(state),
    }


class OnboardComplete(BaseModel):
    agent_md_hash: str = Field(..., min_length=1, description=(
        "SHA-256 of the OUR-block content as built into the agent's discipline "
        "manifest file (CLAUDE.md for Claude, AGENTS.md for most others). "
        "Registering this clears the onboard gate."
    ))
    agent_md_content: str | None = Field(default=None, description=(
        "Optional: full text of the built discipline manifest. When provided, "
        "stored on the project's onboard_state for the test workspace to surface "
        "in the viewer panel side-by-side with the target mock. Hard cap 64KB; "
        "longer content is rejected to keep the JSONB row bounded."
    ))


@router.post("/api/v2/projects/{slug}/onboard-state/complete")
def complete_onboard(slug: str, body: OnboardComplete, request: Request, db: Session = Depends(get_db)):
    """Registers agent_md_hash + completed_at (and optionally agent_md_content).
    Clears the onboard gate (operational write access; full onboard completes
    when first_close_complete also stamps via task→done hook).

    Wave 2.0: also auto-creates the "Onboard absorption workspace" ceremonial
    ticket on first successful registration (idempotent — subsequent calls
    don't recreate).

    Idempotent — re-completing updates the hash (e.g., after a CONTRACT_VERSION
    rebuild) but does not retract the gate clearance or create duplicate
    ceremonial tickets.
    """
    project = db.query(Project).filter(Project.slug == slug).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    _onboard_auth_or_envelope(request, db, project_id=project.id)
    _require_write(request, db, project.id)
    if body.agent_md_content is not None and len(body.agent_md_content.encode("utf-8")) > 65536:
        raise HTTPException(status_code=413, detail=(
            "agent_md_content exceeds 64KB cap. Send the hash without content "
            "(viewer panel will show 'content not captured') or trim the file."
        ))
    state = dict(project.onboard_state or {})
    is_first_completion = not state.get("agent_md_hash")
    state["agent_md_hash"] = body.agent_md_hash
    state["completed_at"] = datetime.now(timezone.utc).isoformat()
    if body.agent_md_content is not None:
        state["agent_md_content"] = body.agent_md_content

    # Wave 2.0: auto-create the ceremonial absorption ticket on FIRST /complete
    # (subsequent /complete calls are re-stamps and don't recreate the ticket).
    # Skip if state already records a ceremonial_ticket_id (idempotency guard).
    absorption_ticket_id = state.get("absorption_ticket_id")
    if is_first_completion and not absorption_ticket_id:
        absorption_ticket_id = _create_absorption_ticket(db, project)
        if absorption_ticket_id:
            state["absorption_ticket_id"] = absorption_ticket_id

    project.onboard_state = state
    flag_modified(project, "onboard_state")
    db.commit()

    # Operationally complete (gate cleared for write access) — first_close_complete
    # is a substep tracker, not an operational gate. The agent can write tasks +
    # everything else immediately; the customer's first close stamps the final
    # substep at their own pace. Wave 2.0.1 will add the wizard close-pending
    # UI + force-finish escape hatch built on top of first_close_complete.
    operationally_complete = bool(state.get("agent_md_hash") and state.get("completed_at"))
    fully_complete = operationally_complete and bool(state.get("first_close_complete"))
    return {
        "slug": slug,
        "onboard_state": state,
        "complete": operationally_complete,         # gate cleared; agent can write
        "gate_cleared": operationally_complete,
        "fully_complete": fully_complete,           # all 6 substeps stamped
        "content_captured": body.agent_md_content is not None,
        "absorption_ticket_id": absorption_ticket_id,
        # Wave 2.0: next_step is now first_close_complete if not yet stamped
        # (instead of None terminal). Lets the wizard / agent know there's
        # one more substep to stamp via the customer closing their first ticket.
        "next_step": None if fully_complete else _next_step(state),
    }


def _create_absorption_ticket(db: Session, project: Project) -> str | None:
    """Wave 2.0: create the "Onboard absorption workspace" ceremonial ticket
    on first /complete. Returns the new task id, or None if creation failed
    (which is logged but doesn't fail the /complete call — onboard completion
    must not depend on ticket creation success).

    Lands in Triage with a server-supplied deliberate-Triage transition_note
    that satisfies the wave-1.8.4 PHASE_REQUIRED_ON_CREATE gate.

    Description teaches the dual purpose: absorption workspace AND first
    human-closure ceremony tutorial. Closes (via human) when real first-phase
    work tickets exist; the close stamps first_close_complete substep.
    """
    try:
        # Late imports avoid module-load circularity with projects.py
        from app.models.task import Task
        from app.models.phase import Phase
        from sqlalchemy import func as sa_func

        # Find or fall back to Triage phase (every project has one per onboard scaffold)
        triage = db.query(Phase).filter(
            Phase.project_id == project.id,
            Phase.name == "Triage",
        ).first()

        # Auto-assign task_number + sort_order
        max_num = db.query(sa_func.max(Task.task_number)).filter(Task.project_id == project.id).scalar()
        next_num = (max_num or 0) + 1
        max_sort = db.query(sa_func.max(Task.sort_order)).filter(Task.project_id == project.id).scalar()
        next_sort = (max_sort or 0) + 10

        description = (
            "TWO purposes:\n\n"
            "(1) Workspace for capturing what came before the board - prior chat, design "
            "discussions, existing context. As that work surfaces real first-phase tickets, "
            "move the substantive content there.\n\n"
            "(2) This is your first human-closure ceremony. Your agent will land more tickets "
            "in needs_review over time - they need your eyeball + a closing note before they "
            "go to done. Practice the rhythm here once on something low-stakes so the next 8 "
            "land smoother.\n\n"
            "Auto-created by VibeForge+ on onboard completion. Lives in Triage by design "
            "(this IS the deliberate Triage placement). Close via the standard agent -> "
            "needs_review -> human-closes flow when real first-phase work tickets exist."
        )

        task = Task(
            project_id=project.id,
            title="Onboard absorption workspace",
            short_description="Workspace for absorbing prior context + first human-closure ceremony tutorial.",
            description=description,
            status="ready",
            priority="medium",
            owner_label="agent",
            phase_id=triage.id if triage else None,
            task_type="chore",
            sort_order=next_sort,
            task_number=next_num,
        )
        db.add(task)
        db.flush()  # get task.id without committing yet (caller commits)
        return task.id
    except Exception as e:
        # Don't fail /complete if ticket creation fails — log and continue
        import sys
        sys.stderr.write(f"[onboard.complete] absorption-ticket auto-create failed: {e!r}\n")
        return None


# ────────────────────────────────────────────────────────────────────
# /api/v2/projects/{slug}/onboard-state/force-finish  (wave 2.0.1 / VF-361)
# ────────────────────────────────────────────────────────────────────
class OnboardForceFinish(BaseModel):
    rationale: str = Field(..., min_length=30, max_length=2000, description=(
        "Operator-supplied reason for skipping the close-ceremony substep "
        "(min 30 chars). Audit-quality required-field family alongside "
        "docs_state and abandoned_note: the 30-char floor surfaces 'why' "
        "to future review without prescribing a template. Stored on "
        "onboard_state.first_close_complete.rationale."
    ))


@router.post("/api/v2/projects/{slug}/onboard-state/force-finish")
def force_finish_onboard(slug: str, body: OnboardForceFinish, request: Request, db: Session = Depends(get_db)):
    """Wave 2.0.1 (VF-361): operator escape hatch for the first_close_complete
    substep. Stamps the substep with force_finished=True + a rationale,
    bypassing the natural-close path. One-way operation (409 if already
    stamped — natural close OR prior force-finish both lock it).

    The reset endpoint provides backward escape; force-finish + reset compose
    to two-way wizard movement when needed.

    Telemetry honesty: force_finished=True is preserved on the project so
    future analytics can see "X% of projects completed onboard via close;
    Y% via force-finish." If Y trends high, that's signal — close-ceremony
    too heavy or customer education gap. Either way, visible incomplete-
    implementation > silent skip.
    """
    project = db.query(Project).filter(Project.slug == slug).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    actor_type, actor_name = _onboard_auth_or_envelope(request, db, project_id=project.id)
    _require_write(request, db, project.id)

    state = dict(project.onboard_state or {})

    # Operationally complete is required — force-finish only makes sense
    # AFTER agent_md_hash registers (wave 2.0.7: substeps 1-6 done; substep 7
    # first_close_complete pending).
    operationally_complete = bool(state.get("agent_md_hash") and state.get("completed_at"))
    if not operationally_complete:
        raise HTTPException(status_code=409, detail=(
            "force-finish requires operationally-complete onboard "
            "(agent_md_hash + completed_at must be stamped via /onboard-state/complete first). "
            "Substep 7 (first_close_complete) only opens after substep 6 (agent_md_hash) lands."
        ))

    # One-way: 409 if already stamped (natural close OR prior force-finish).
    if state.get("first_close_complete"):
        existing = state["first_close_complete"]
        already_force = bool(existing.get("force_finished")) if isinstance(existing, dict) else False
        raise HTTPException(status_code=409, detail=(
            f"first_close_complete already stamped "
            f"({'via force-finish' if already_force else 'via natural close'}). "
            "Force-finish is one-way; no-op if already complete. Use "
            "/onboard-state/reset for full backward movement."
        ))

    state["first_close_complete"] = {
        "stamped_at": datetime.now(timezone.utc).isoformat(),
        "force_finished": True,
        "rationale": body.rationale,
        "actor": actor_name,
        "actor_type": actor_type,
    }
    project.onboard_state = state
    flag_modified(project, "onboard_state")

    # ActivityEvent for audit trail. Sibling to the natural-close path's
    # first_close_complete_stamped event; distinguished by action name +
    # force_finished:true in the details payload.
    try:
        db.add(ActivityEvent(
            project_id=project.id, task_id=None, actor_type=actor_type,
            action="onboard_force_finished",
            details=_json.dumps({
                "actor": actor_name,
                "force_finished": True,
                "rationale_len": len(body.rationale),
                "rationale": body.rationale[:500],
            }),
        ))
    except Exception:
        # Don't fail force-finish if audit event creation fails;
        # the state mutation is the load-bearing operation.
        pass

    db.commit()

    return {
        "slug": slug,
        "onboard_state": state,
        "complete": True,
        "fully_complete": True,
        "force_finished": True,
        "stamped_at": state["first_close_complete"]["stamped_at"],
        "next_step": None,
    }
