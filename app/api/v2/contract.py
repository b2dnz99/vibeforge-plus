"""
VibeForge+ Agent Contract (/agentnotes)
Two-tiered: unauthenticated = minimal, authenticated = full contract.
v2.1 — Self-contained bootstrap: rules, workflows, CLAUDE.md template.
"""
import hashlib
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.models.project import Project
from app.models.agent import Agent

router = APIRouter()

# VF-353 R2.6: 2.9.0 -> 2.10.0 on 2026-05-01 (pm).
#
# Versioning intent (PK directive 2026-05-01):
#   - 2.x = customer-onboard mechanism evolution (round 1 + 2 + 2.5 + 2.6 + ...)
#   - 3.x = reserved for Forgejo + Vaultwarden integration era (pre RC/1.0,
#     post wizard). The big version-3 milestone marks the auth/git/vault
#     integration, not contract additions.
#   - 1.0  = RC.
#
# History:
#   - 2.7.0  Last stable pre-round-2.
#   - 3.0.0  Bumped premature on 2026-05-01 morning (this stamp self-corrected
#            same day; should have been 2.8.0 for round 2 alone).
#   - 2.9.0  Round 2 + R2.5 combined. Self-corrected from 3.0.0.
#   - 2.10.0 R2.6 wave 1 (post Claude Desktop's external review).
#   - 2.10.1 IC-026 vendor-native filename for discipline manifest.
#   - 2.11.0 R2.7 wave 1 (VF-356 phases PATCH/DELETE-rejection + VF-357
#            PATCH allow-list strict mode + standard 422 shape).
#   - 2.11.1 IC-034: vf_render warns on missing frontmatter + OUR-block
#            explicit frontmatter requirement.
#   - 2.11.2 R2.7 wave 1.6 audience-separation fix: scaffold artefacts
#            sanitised — internal-jargon refs removed from vf_render.py
#            customer-facing surface; stderr WARN tags repointed from
#            [IC-034 WARN] to [VF-RENDER WARN]; new scripts/check_scaffold_clean.py
#            guard prevents regression.
#   - 2.11.3 R2.7 wave 1.7 audit-scope extension: scope was too narrow in
#            1.6 — only audited app/onboard_scaffold/ and missed the other
#            customer-facing surfaces. This release sanitises 7 customer-
#            string leaks in contract.py /agentnotes JSON output, fixes 2
#            outdated tag refs in OUR-block source + inline copy now that
#            the actual stderr tags are [VF-RENDER WARN], and extends
#            check_scaffold_clean.py with 2 new modes: --our-block (scans
#            OUR-block shipped slice + inline constant) and --live <base_url>
#            (fetches /agentnotes JSON and walks all string values,
#            eliminating string-vs-comment ambiguity in contract.py).
#   - 2.14.3 CURRENT (VF-377 — VF-353 finish: thesis capture + stale-framing
#            #1 fix + confidential/ collapse + parent CLAUDE.md dogfood
#            hint). Doc-only ship; no /agentnotes JSON content change. Bump
#            advertises the new framing so fresh agents refreshing the
#            contract get pointers to the rewritten agent-contract.md §5
#            (CLAUDE.md is a thin pointer, NOT an "abridged on-disk copy of
#            the contract" — that was the stale framing that re-taught fresh
#            Claudes the wrong mental model) + the new §5.5 "Lifecycle and
#            dogfood/customer asymmetry" thesis section (board-as-source /
#            CLAUDE.md-intentionally-thin / dogfood-iterates-customer-static
#            / 10-year-context claim). Adjacent: confidential/ tier
#            collapsed into internal/ (4 docs migrated; documentation-
#            architecture.md §1b updated; the "to-be-sold product" framing
#            that justified the third tier didn't materialise) + parent
#            CLAUDE.md (+ meta-mirror) gained a top-of-file dogfood-warning
#            hint pointing at the new thesis section. Closes VF-353
#            architectural cleanup at 100% of original scope (was 30%
#            after wave 2.0.x landed only the soft-relations rule fix +
#            VF-365 + the 2 critical spawn-bugs).
#   - 2.14.2 (VF-376 — IC-035 follow-up two-side observability on
#            the /onboard-state 401 path). Problem PK observed 2026-05-05
#            during an idle session: the IC-035 helpful-envelope landed in
#            the response body but the access-log line stayed bare
#            ("401 Unauthorized"). Both sides of the same failure see
#            different fragments — the human watching server logs has no
#            idea who/what/why; the agent gets a teaching envelope but
#            still has to parse JSON to learn it. KISS fix in
#            _onboard_auth_or_envelope (additive, no breaking changes):
#            (1) sniff caller (X-Real-IP, User-Agent, Authorization
#            header presence + token last-4); (2) derive a stable
#            auth_diagnosis enum (auth_missing | auth_empty |
#            token_invalid_or_revoked | token_expired | unknown) so log +
#            envelope name the same failure mode; (3) emit a structured
#            WARN log line ([ONBOARD-401] proj=... path=... ip=... ua=...
#            diagnosis=... token_hint=... cadence=...) — single-line,
#            greppable, beside the access log; (4) extend envelope with
#            client_observed block + auth_diagnosis field so agent self-
#            diagnoses without a /me round-trip. Tiny in-memory cadence
#            dedup (per-process, 30-min sliding window keyed on (ip, ua,
#            path, diagnosis)) flags flapping clients (count >= 5 in
#            window → "flapping"). Helper-not-babysitter check: PASSES —
#            additive observability, doesn't gate or block; the actual
#            401 behaviour is unchanged. Teaches the design principle:
#            error responses should be legible to BOTH the agent reading
#            the body AND the human reading the log, naming the same
#            diagnosis on both sides at the same point of failure.
#   - 2.14.1  (post-2.14.0 patch — needs_review owner_label gate
#            tightening; PK live observation during Codex cross-vendor
#            traffic). Prior gate accepted body.owner_label OR
#            task.owner_label OR "" — so an agent could PATCH status=
#            needs_review without including owner_label in the body, and
#            ride a stale human owner from a prior assignment. Defeats
#            the active-handoff intent (every needs_review = explicit
#            reassign so the agent THINKS about who is reviewing).
#            Tightened: require body.owner_label present + format
#            'human:<non-empty Display Name>'. Colon-required check
#            also closes a startswith("human") false-positive that
#            would have accepted bare "human" or accidental
#            "humanitarian". Two new 422 codes:
#            NEEDS_REVIEW_OWNER_REQUIRED (no owner_label in body) +
#            NEEDS_REVIEW_OWNER_FORMAT (wrong format). Both teach the
#            fix in agent_remedy. Agent contract task_discipline rule
#            on needs_review updated to name the explicit-body
#            requirement + the colon-required format. Helper-not-
#            babysitter check: PASSES — proportional gate (catches
#            silent-skip; teaches the fix; doesn't add ceremony to
#            human or correctly-formed agent calls).
#   - 2.14.0  (R2.7 wave 2.0.8 round 5 / VF-368 — OpenAPI public-
#            route filter + minor-bump rollup of the wave 2.0.8 cumulative
#            change). PK call: 2.13.x → 2.14.x because wave 2.0.8 added
#            net-new endpoints (artefacts), changed agent-contract behaviour
#            (drift gate grace window), expanded the contract surface
#            (relationships + artefacts now in /agentnotes), restructured
#            FRAMING_TEXT — cumulatively a minor bump, not another patch.
#            The OpenAPI filter is the natural ship moment for the version
#            roll-up since it changes what the public schema exposes.
#            Filter shape (KISS, default-deny + explicit-allow): include-
#            list of prefixes covering /agentnotes, /me, /onboard/*,
#            project-scoped data surfaces (/tasks, /milestones, /phases,
#            /members, /mentionables, /dashboard, /resume, /artefacts,
#            /archive-summary, /onboard-state*), task-scoped surfaces
#            (/tasks/{id}, /notes, /audit, /relationships, /related,
#            /blocks), milestone close/reopen, /triggers/. EXCLUDED by
#            default (any prefix not in the allow-list): admin/portal/
#            bootstrap/proxy/tokens/agents-lifecycle/drift-telemetry/
#            sessions/users + any future routes added without explicit
#            allow-list inclusion. Implementation: app.openapi function
#            replaced with filter wrapper at app/main.py; original
#            schema-generator preserved + cached normally; only the
#            paths dict gets pruned. Codex 2026-05-04 follow-up audit
#            confirmed earlier findings #1 (relationships) + #2
#            (artefacts) FIXED; this ships #3 (OpenAPI exposure). #4
#            (drift batch friction) shipped in 2.13.12 R4 but Codex
#            hadn't deeply re-tested at note time. Operator-note: the
#            FULL schema is still discoverable via container exec /
#            source read; this filter only affects what's served at
#            public /openapi.json + the /api/v2/openapi.json mirror.
#   - 2.13.12  (R2.7 wave 2.0.8 round 4 — drift gate batch
#            consistency window). Codex blind cross-vendor onboard test
#            surfaced that drift_eval prompt cycled repeatedly during
#            batch task/relationship operations. Root cause: every
#            /agentnotes refresh nuked drift_eval_passed_at unconditionally,
#            forcing re-eval on the next mutation. Codex's defensive
#            refresh-between-batch-mutations habit hit this every iteration.
#            Fix: 30-min grace window. _refresh_agent_drift now keeps a
#            recent pass intact across refreshes (only resets if the pass
#            is older than DRIFT_EVAL_PASS_GRACE_SECONDS = 1800). Pivot
#            count still resets on every refresh (in-flight pivots are
#            reset by the refresh, as before). Hash history (drift_eval_
#            hashes, lifetime, persists across cycles) still catches replay
#            attempts during the grace window — agents can't cache one
#            response and reuse it. After grace expires (or escalation, or
#            human clear), the gate re-arms normally. Net effect: batch
#            operations within 30 min get one drift_eval challenge, not
#            one per mutation; long sessions still get the periodic
#            challenge as designed. New constant exposed:
#            DRIFT_EVAL_PASS_GRACE_SECONDS = 1800 in drift_gate.py.
#            Behaviour change tested with live Codex batch — was the
#            primary observed friction during wave 2.0.8 prep; now
#            suppressed.
#   - 2.13.11  (R2.7 wave 2.0.8 round 3 — VF-367 KISS read-only
#            artefact-fetch API). Codex blind cross-vendor onboard test
#            surfaced agent-side expectation that artefacts (plan,
#            agent_md, contract, handover) should be fetchable via API.
#            New endpoint: GET /api/v2/projects/{slug}/artefacts/{type}.
#            Type-routed: plan + agent_md return content + hash from
#            onboard_state (KISS over already-persisted data, zero new
#            storage burden); contract is a 308 redirect to /agentnotes/
#            {slug} (canonical contract source); handover returns 404
#            with FS pointer (not server-captured; queued as proposal
#            VF-372 backlog). Unknown types return 404 with the explicit
#            list of supported types — fixes the contract-vs-API drift
#            Codex flagged ("available types: X, Y, Z then 404 on those").
#            Symmetric KISS-extended: new optional `plan_content` field
#            (≤64KB) on /onboard-state/ack for step=plan_hash, mirroring
#            agent_md_content pattern (substep 6). Stored at canonical
#            key "plan_content" so artefact API has predictable read.
#            Backward compatible — agents that don't include plan_content
#            still pass; endpoint returns content_captured=false +
#            filesystem_path_hint. New endpoints.artefacts block in
#            contract.py exposing the new endpoint so /agentnotes carries
#            what OpenAPI exposes — same drift-cleanup pattern as VF-365.
#            Eats own dogfood: ticket VF-367 documented + uses the new
#            structured /related rule from VF-365.
#   - 2.13.10  (R2.7 wave 2.0.8 round 2 — handover doc placement
#            convention surfaced in scaffold README + scaffold version
#            bump 2.3.0 -> 2.4.0). PK contribution from Codex blind
#            cross-vendor onboard findings: handovers in 0-MD/ are
#            intentionally TOC-categorically-separate but visually
#            confusing when they live under same `0-MD/` root. KISS fix:
#            no file moves; explain the convention in scaffold README +
#            update doc-classes diagram to show `0-MD/progress/` as a
#            peer to `0-MD/0-Documentation/`, with explicit "session-
#            continuity escape-hatch artefacts, NOT durable documentation"
#            label. Adds a callout block in the handover section
#            explaining why the placement is what it is. The bundled TOC
#            builder already lists progress/ in a dedicated section
#            ("Progress (session state, handover)") — README now makes
#            that visibility intentional rather than incidental. SCAFFOLD
#            artefact bytes change -> SCAFFOLD_VERSION bump 2.3.0 -> 2.4.0
#            in lockstep with surfaces.scaffold.version. No FRAMING_TEXT
#            changes -> no framing payload version bump. No code-side
#            changes other than versions + history.
#   - 2.13.9  (R2.7 wave 2.0.8 round 1 — contract surface text
#            updates from Codex blind cross-vendor onboard test triage:
#            (A) framing-rule anti-pattern hardening — OUR-block rule #1
#            now explicitly retires the older "paste three specific
#            sentences" pattern that Codex's AGENTS.md inherited (likely
#            from cross-session memory of older contract iterations);
#            full-section paste is RETIRED via positive guidance + explicit
#            NOT-list ("NOT excerpts. NOT three sentences. NOT a summary.").
#            (F) NEW TL;DR section added at top of OUR-block — mirrors the
#            wizard's TLDR pattern; gives the agent a one-paragraph load-
#            bearing summary before the rule walls. Lands in customer-
#            generated CLAUDE.md / AGENTS.md as the first section.
#            (G) wave-tag prose stripped from FRAMING_TEXT chat-paste
#            prose: "## Onboard substep order (wave 2.0.7)" -> "## Onboard
#            substep order"; "**compaction_practice** (NEW — wave 2.0.7)"
#            -> "**compaction_practice**". Wave-anchor stays in JSON
#            payload (version + wave fields) — agent debug awareness
#            preserved without leaking debug noise into the human-facing
#            wall.
#            (C) Compaction question expanded ~50 -> ~110 words to land
#            as the "medium" rhythm beat the wave 2.0.7 substep order
#            was designed for (Codex pasted the prior question as one-
#            liner; the ceremony beat got lost). New question carries
#            its own weight: opens with "important practice", names the
#            failure mode (LOSSY compression, agent forgets things),
#            names the discipline (HANDOVER + manual compact + ABSORB),
#            preserves the easy-skip escape, ends with concrete cost
#            framing ("first time you watch me forget"). Same handover-
#            cycle README cross-reference for walkthrough sourcing.
#            FRAMING_TEXT bytes change -> framing payload version still
#            advertised as 1.5 + wave anchor "2.0.4" (host comment notes
#            this is the wave 2.0.8 R1 byte change; payload version field
#            stays for audit continuity through the wave-2.0.8 batch).
#            test_wizard.html prompt updated lockstep with the new
#            verbatim compaction question (single source of truth for
#            what the agent says to the human). No SCAFFOLD bump (artefact
#            bytes unchanged). No wave bump in the formal sense — wave
#            2.0.8 is the umbrella; this is round 1 of that wave.
#   - 2.13.8  (post-wave-2.0.7 contract-vs-API drift fix —
#            agent contract was teaching the inline-text soft-relation
#            pattern (`related: VF-XXX` in description) when the structured
#            relationship endpoints have existed and powered the UI drawer
#            for some time. Codex blind cross-vendor onboard test today
#            surfaced this: agent followed the contract literally → wrote
#            inline text → human review caught it → agent then discovered
#            POST /tasks/{id}/related via OpenAPI on its own. Real fix is
#            documentation, not API. This bump: (1) updates
#            task_discipline.rules first rule to point to structured
#            POST /tasks/{task_id}/related (other_task_id + reason >=10)
#            with explicit "inline prose is unqueryable + unidirectional +
#            drops audit signal — avoid" deprecation; (2) adds new
#            `relationships` key under endpoints.tasks exposing
#            GET /relationships, POST /related, POST /blocks so /agentnotes
#            visibly carries what OpenAPI already exposes; (3) parent
#            CLAUDE.md updated in lockstep (meta-mirror copy follows).
#            OUR-block source already carried the correct guidance — the
#            stale path was contract.py → /agentnotes (which agents fetch
#            on session-start, before reading their own AGENTS.md/CLAUDE.md).
#            No wave bump (substep flow + framing unchanged); no scaffold
#            bump (artefact bytes unchanged).
#   - 2.13.7  (R2.7 wave 2.0.7 — compaction_practice substep
#            inserted as #4 + framing-acknowledgement check-in reverts
#            to 3 fields + FRAMING_TEXT verbatim-paste adds 4-practices
#            section + What-the-board-provides honest rework + What-you-do
#            agent-as-query-interface + What-the-agent-does adds
#            query-on-behalf bullet). PK-driven design: handover→compact
#            →absorb practice deserves its own substep recognition rather
#            than being bundled as a 4th field on framing_acknowledged
#            ack. UX rhythm: framing-wall → silent tooling → light
#            doc_complexity → medium compaction-wall (with "skip" easy
#            escape) → plan → manifest → first_close. Agent has tooling +
#            doc_complexity context grounded before surfacing the
#            compaction teaching moment, so walkthrough is sourced from
#            scaffold README's handover-cycle section rather than
#            improvised. Substep enum grows 6 → 7. New
#            compaction_practice_ack field (≥4 chars; "skip" is the
#            shortest valid escape) on /onboard-state/ack when
#            step=compaction_practice. ActivityEvent action=
#            "onboard_compaction_practice_captured" with verbatim ack
#            text + interpretation (skip vs engaged) for human spot-check.
#            Framing payload v1.4 → v1.5; wave anchor "2.0.3" → "2.0.4".
#            SCAFFOLD_VERSION bumps with the README handover-cycle section
#            addition (artefact bytes change). helper-not-babysitter check
#            on the new substep: PASSES — practice is proven (PK and Claude
#            have lived it for 2 weeks of session continuity); skip
#            preserves operator agency; teaching moment is for cold-onboard
#            users who don't yet know handover/compaction as concepts.
#   - 2.13.6  (R2.7 wave 2.0.6 — FRAMING_TEXT verbatim-paste
#            portion condensed ~340 → ~150 words). PK feedback after the
#            wave 2.0.3 ship: the in-chat banner the agent pastes verbatim
#            was wordy + overly prescriptive; wall-of-text gets skipped by
#            humans; needed to keep the vibe + ServiceNow/Jira analogy +
#            you-are-enforcer gravitas + {human_name} personalization but
#            cut the babysitting prose. New verbatim-paste copy: direct
#            opening (loop-vs-walk-away), failure modes compressed from 5
#            bullets to 1 prose sentence (memory decays / confidence
#            outruns / scope creeps / drift), "framework not a discipline
#            engine" + ServiceNow/Jira retained, "what it does is
#            narrower: path of least resistance + back-and-forth capture
#            + durable record" (3 bullets → 1 line), "you are the
#            enforcer, {human_name}" gravitas line preserved. End marker
#            "...just makes carrying it cheaper." retained so the
#            existing OUR-block verbatim-paste-stop rule still works
#            unchanged. Sections after the verbatim-paste boundary
#            (framing-acknowledgement check-in procedural section, substep
#            order, what-X-does, closing) untouched — those are agent-read
#            context, not human-paste content. Framing payload version
#            "1.3" → "1.4" + wave anchor "2.0.2" → "2.0.3". Template word
#            count drops 1180 → ~990. helper-not-babysitter check passes
#            (compressing prescriptive prose IS the helper-not-babysitter
#            move at the copy layer).
#   - 2.13.5  (R2.7 wave 2.0.5 — vf_toc.py scaffold tool fix +
#            SCAFFOLD_VERSION 2.1.0 → 2.2.0). PK observed broken-link
#            navigation in the rendered TOC.html on the Flight Tracker
#            (Project1) onboard run: links pointed to project-root-relative
#            .md paths from a TOC.html living inside 0-MD/0-Documentation/,
#            so clicks resolved to 404. Codex's pass-2 finding (README.md
#            missing-frontmatter warning despite being the prescribed
#            smoke-test target) bundles in. Tool design fixes:
#            (1) vf_toc.py now ORCHESTRATES the render step itself — every
#            indexed .md is rendered to its sibling .html before the TOC
#            builds, so TOC.html links land on real files (--no-render
#            opt-out for separate render workflows). (2) Per-output
#            dependency contract: TOC.md links to .md sources (relative to
#            TOC.md location), TOC.html links to .html siblings (relative
#            to TOC.html location). Each output verifies its targets at
#            emit time + adds visible [missing source] / [NEEDS RENDER]
#            indicators when something's gone, never refuses output
#            (helper-not-babysitter — operator decides what to fix).
#            (3) template.html gains an `index` audience-pill class
#            (neutral grey for TOC) + toc-helper styles ([NEEDS RENDER]
#            pill, empty state, meta lines). Sidebar kicker now reads
#            "// Index" via {audience_label} substitution rather than the
#            previous misleading "// Public". (4) Scaffold README.md
#            gains frontmatter so it stops emitting missing-frontmatter
#            warnings on smoke-test renders + acts as a self-demonstrating
#            example of the convention. (5) README documents the new
#            workflow (render-before-index ordering, replaceability
#            framing per the scaffold-tools-are-bootstrap-seeds principle).
#            TOC_VERSION bumps 1.1 → 1.2. surfaces.scaffold.version
#            updates in lockstep per bidirectional cross-reference
#            discipline.
#   - 2.13.4  (R2.7 wave 2.0.4 — human_ack floor loosened from
#            ≥20 to ≥8 chars after first real-world run regression: PK's
#            proportional-but-short ack "Yes I understand" (15 chars) hit
#            the floor; the workaround "I Understand in 20 characters"
#            became the captured human_ack on Project1's activity log,
#            making the babysitter-shape friction visible. Floor's real
#            purpose is filtering rubber-stamp acks ('ok'/'yes'/'👍'),
#            not extracting paragraphs from a yes/no consent moment. ≥8
#            keeps that filter ('I accept'/'Yes I do' pass; 'ok' / 'yes'
#            / single-emoji still blocked) without paternalism. Validator
#            envelope wording updated to reflect the new floor + the
#            rationale. helper-not-babysitter check on the change: PASSES
#            — proportionality fix, not gate-removal. The audit trail
#            (verbatim human_ack + onboard_human_ack_captured activity
#            event) still does the heavy lifting against fabrication.
#   - 2.13.3  (R2.7 wave 2.0.3 — framing-acknowledgement check-in
#            forcing function + FRAMING_TEXT gravitas rewrite + {human_name}
#            personalization). Three changes that bundle conceptually:
#            (1) FRAMING_TEXT rewritten with explicit gravitas: agent
#            failure-mode disclosure ("memory is unreliable / confidence
#            outruns correctness / under task pressure checks get skipped
#            / scope creeps silently / your agent can go rogue"), explicit
#            you-are-the-enforcer-not-optional-not-delegable framing, and
#            the new "framing-acknowledgement check-in" section that
#            describes the verbatim+rephrase+human_ack flow with the
#            "best the framework can do is this floor" reason-not-to-fail
#            wording verbatim. (2) Personalization: framing payload
#            substitutes {human_name} placeholders with the project
#            creator's User.display_name, resolved via agent.project_id ->
#            Project.created_by_user_id. Agent's verbatim paste now
#            addresses the human by name (e.g. "Parvez Khan, do you accept
#            this framing as how we'll work?"). Falls back to "you" when
#            no creator is resolvable. (3) StepAck for framing_acknowledged
#            now requires THREE fields: surfaced_verbatim (bool true; agent
#            asserts they pasted the framing intro verbatim), surfaced
#            _summary (>=150 chars; agent's rephrasing in own words —
#            tightened from 80 to force real engagement), human_ack
#            (>=20 chars; the human's typed reply, captured verbatim by
#            agent after asking + waiting). 422 envelope spells out which
#            field is missing/short + the reason-not-to-fail rationale so
#            the agent reading the error understands why faking it defeats
#            the gate. ActivityEvent action="onboard_human_ack_captured"
#            stamps the verbatim human_ack to the activity log so the
#            human can spot mismatches against what they actually said.
#            Framing payload version bumps "1.2" -> "1.3" + wave anchor
#            "2.0.1" -> "2.0.2" (framing TEXT bytes changed substantially:
#            624 -> ~1180 words template, ~1184 rendered). SCAFFOLD_VERSION
#            stays 2.1.0 (artefact bytes unchanged). helper-not-babysitter
#            check on the new fields: PASSES — framing alignment is the
#            highest-stakes onboard moment + the human-consent capture is
#            proportional friction; not babysitter-shape.
#   - 2.13.2  (R2.7 wave 2.0.2 — Codex live-onboard finding fixes:
#            test wizard's pasted onboard prompt was hardcoded with the
#            OLD substep order (doc_complexity → plan_hash → tooling_hash)
#            while the server enforced the new wave-2.0 order
#            (tooling_hash → doc_complexity → plan_hash). Codex hit the
#            inconsistency live and surfaced it. This release reorders the
#            wizard prompt steps 4-6 to match server enforcement, fixes
#            projects.py BOARD_GATE_TRIGGERED hint computation (was
#            hardcoded with the OLD order tuple — now imports
#            ONBOARD_STEP_ORDER from onboard.py so server hint and server
#            enforcement can never drift again), updates SCAFFOLD_DEFAULT
#            _CHAT_MESSAGE to a wave-2.0-safe smoke test (renders the
#            scaffold README itself which exists at substep 2; OLD chat
#            message named a generic doc-tree path that doesn't exist when
#            tooling lands first), updates the wizard prompt smoke test
#            similarly, and updates FRAMING_TEXT substep 6 to drop the
#            stale "follow-up release" line (the close-pending UI shipped
#            in 2.13.1). Framing payload version bumps "1.1" -> "1.2"
#            (bytes changed again; bidirectional cross-reference
#            discipline). OUR-block source updated: "during onboard step 6"
#            (stale numbering) -> "during the agent_md_hash substep"
#            (order-agnostic, no future drift). SCAFFOLD_VERSION stays
#            2.1.0 — scaffold artefact bytes unchanged in 2.13.2 (only
#            SCAFFOLD_DEFAULT_CHAT_MESSAGE prose changed; the artefacts
#            themselves don't change). helper-not-babysitter check
#            preserved across all changes (no new gates added; existing
#            gate now produces accurate hints — fixes wrong help, doesn't
#            add babysitting).
#   - 2.13.1  (R2.7 wave 2.0.1 — VF-361 wizard close-pending UI +
#            force-finish escape + Codex pass-1/2 review-pass amendments).
#            Server adds POST /api/v2/projects/{slug}/onboard-state/force-finish
#            (operator escape hatch; rationale >=30 chars; stamps
#            first_close_complete with force_finished:true; one-way; 409 if
#            already stamped). UI adds close-pending banner to
#            /ui/test-wizard with elapsed-time timer counting from
#            completed_at + absorption-ticket link + "Need to skip this?"
#            disclosure containing the rationale textarea + force-finish
#            submit. Substep #6 (first_close_complete) renders force-finished
#            badge when stamped via the escape path. Telemetry honesty: the
#            force_finished flag persists on onboard_state.first_close_complete
#            for future analytics ("X% via close, Y% via force-finish").
#            Codex review pass-1 amendment: endpoints.schema_discovery.openapi
#            now carries an explicit allowed-workflow-vs-discovery distinction
#            so agents don't mistake "visible in OpenAPI" for "allowed to
#            use" (the openapi mirror exposes ~207 routes including admin/
#            auth/proxy that aren't in the agent workflow surface; /agentnotes
#            is the authoritative allowed-surface). Codex review pass-2
#            amendment: /api/v2/onboard/framing payload version bumped from 1
#            to "1.1" + new wave:"2.0" anchor (framing TEXT bytes changed in
#            wave 2.0 — substep-order section + .scratch + tooling-at-step-2
#            lite hints — and the explicit version makes that legible to
#            humans/debug; agents that hash framing already caught the diff).
#            Bidirectional cross-reference discipline preserved: framing
#            payload version bumps because TEXT bytes changed; SCAFFOLD_VERSION
#            stays 2.1.0 because scaffold artefact bytes still don't change
#            in wave 2.0.1. helper-not-babysitter check on force-finish:
#            PASSES — consequence real (operator's onboard sits stuck without
#            an escape; consequence loop genuine); operator-agency preserved
#            (rationale text floor surfaces "why" without prescribing template;
#            no babysitter UX prompts demanding more justification).
#   - 2.13.0  (R2.7 wave 2.0 — coordinated structural change to
#            onboard substep order + first_close_complete substep + IC-035
#            envelope on /onboard/* + openapi.json mirror + ceremonial
#            absorption ticket auto-create on /complete). Substep order
#            materially changed: tooling moves to step 2 (was step 4) so
#            doc-tree layout exists BEFORE planning thinking lands;
#            eliminates Codex's pass-1 tooling-before-documentation
#            inversion. New first_close_complete substep added as #6,
#            server-stamped when ANY task in the project transitions to
#            done; observability-only at v2.0 (no operational gating;
#            wave 2.0.1 will add the wizard close-pending UI + force-
#            finish escape on top of this). Auto-creates "Onboard
#            absorption workspace" ticket on first /complete (lands in
#            Triage with deliberate-Triage rationale; serves as
#            absorption workspace AND first human-closure ceremony
#            tutorial). IC-035: /onboard/* 401s now carry the standard
#            envelope mirroring /agentnotes' unauthenticated tier
#            (cross-vendor evidence: Codex pass-1 + Claude Desktop
#            idle-poll). openapi.json mirrored at /api/v2/openapi.json
#            (rewards Codex's correct schema-discovery self-recovery
#            reflex; was 404 on the API-prefix path). SCAFFOLD_VERSION
#            stays 2.1.0 because scaffold artefact bytes don't change in
#            wave 2.0 (server-side reorder only); customer agent's
#            mental model of WHEN tooling lands shifts via framing_text
#            + substep order, not via bundle content. Bidirectional
#            cross-reference discipline preserved.
#   - 2.12.3 (R2.7 wave 1.8.4 — IC-036 PHASE_REQUIRED_ON_CREATE
#            gate). Cross-vendor evidence (Claude Code's prior Flight
#            Tracker run + Codex's current run) showed agents skip Ticket
#            Discipline rule #3 ("set phase_id; do not leave new tasks in
#            Triage") under task pressure. Server now 422s on agent-driven
#            POST /api/v2/projects/{slug}/tasks when phase_id is missing OR
#            resolves to default Triage AND no transition_note (>=30 chars)
#            explaining deliberate Triage. Standard envelope (code +
#            detail + human_visible + agent_remedy listing available
#            phases). Same audit-quality enforcement family as
#            transition_note >=40, blocked_by_reason >=10, abandoned_note,
#            docs_state >=30. Helper-not-babysitter check passes
#            (consequence real and recurring; gate substitutes for missing
#            agent consequence-loop; deliberate-Triage escape hatch keeps
#            sketching workflow possible). Humans not gated. Codifies the
#            failure mode named explicitly by the OUR-block Specificity
#            Discipline meta-rule: "vague conversational rules in any
#            contract get optimised away under task pressure."
#   - 2.12.2 (R2.7 wave 1.8.3 — drop public/ from default scaffold
#            tree + always-fires NOTE on audience: public renders +
#            SCAFFOLD_VERSION bump 2.0.0 -> 2.1.0 to match the bundle
#            change; surfaces.scaffold.version updated in lockstep per the
#            bidirectional cross-reference discipline shipped in wave 1.8.2).
#            Upstream-fix change shaped by Claude Desktop's pass-2 + Claude
#            Code's audit thread. The "public" doc tier is mostly cargo-cult
#            from human-product-documentation eras: most projects don't have
#            a public technical readership, the agent surface (contract +
#            teachable 422 envelopes) IS the programmable public surface,
#            and pre-creating public/ invites default-of-fill labour without
#            proportional readership. Default scaffold tree is now
#            internal/ + proposed/; public/ created on demand only — when
#            an outside reader explicitly asks for documentation. The
#            moment the directory exists, the TOC auto-includes it AND
#            vf_render activates an always-fires [VF-RENDER NOTE] on every
#            audience: public render. The NOTE is the standing
#            responsibility-transfer caveat: bundled scan catches only a
#            narrow class of generic internal-jargon markers, NOT IP / PII
#            / trade-secrets / client-names / commercial-confidence; the
#            human is the IP gatekeeper. Fires unconditionally including
#            when the leak scan finds nothing (clean run is the most
#            dangerous moment for false confidence). Retires Claude Code's
#            two pass-3 concerns upstream: (1) PUBLIC_LEAK_PATTERNS
#            cognitive-ergonomics false-positive risk shrinks to near-zero
#            because the guard fires only at explicit graduation, not on
#            routine doc-writing; (2) audience-asymmetry concern dissolves
#            because public/ is no longer a default-populated tier
#            requiring symmetric inverse guarding. Codifies the meta-
#            principle: the cheapest gate is the one against the existence
#            of the surface you'd otherwise have to gate.
#   - 2.12.1 (R2.7 wave 1.8.2 — surface-version visibility + scaffold
#            bundle bump). Adds `surfaces` map to authenticated /agentnotes
#            response so agents can detect stale local caches across all
#            independently-versioned customer-facing surfaces in one GET
#            (currently `scaffold` only; framework lets us add more without
#            schema break). Bumps SCAFFOLD_VERSION 1.0.0 -> 2.0.0 — the wave
#            1.8 scaffold bundle materially differs from the prior one
#            (TOC location moved, three-class scan, audience=public WARN
#            scan, README "Doc classes" section). Surfaced by Claude
#            Desktop's pass-2 audit-from-local-cache write-up: agent
#            correctly identified that contract version is not a proxy for
#            scaffold version; without this map the desync is invisible
#            until artefact-byte-level comparison. Auto-reminder is the
#            presence of the surface-version field plus bidirectional
#            cross-reference comments at SCAFFOLD_VERSION (onboard.py) and
#            the surfaces map entry (contract.py) — bump together or the
#            audit signal lies. NOT a new guard; the structure itself is
#            the discipline.
#   - 2.12.0 (R2.7 wave 1.8 + 1.8.1 combined: scaffold doc-class
#            layout + docs_state assessment on needs_review). Wave 1.8 ships
#            a default three-class doc layout (internal/public/proposed
#            under 0-MD/0-Documentation/) with TOC at
#            0-MD/0-Documentation/TOC.md; vf_render.py adds a heuristic
#            audience=public leak scan; vf_toc.py refactored to scan the
#            three classes + exclude archived/ subdirs; scaffold README
#            gains a Doc classes section (layout + lifecycle + when-to-
#            propose anti-sycophancy brake) with explicit AGENT-NEEDS-TO-
#            ADAPT + ASK-HUMAN voice; OUR-block + inline gain a
#            cross-pointer paragraph under Folder Discipline. Wave 1.8.1
#            adds docs_state {needed|exists|updated|created|skipped} +
#            docs_note (>=30 chars) on TaskPatch; agents must include both
#            on transition to needs_review (422 with code=
#            DOCS_ASSESSMENT_REQUIRED + agent_remedy on failure); handler
#            auto-posts a structured TaskNote 'docs_state: <state> — <note>'
#            into the audit feed.
#
# R2.6 changes that justify the bump (additive):
#   (1) IC-020 + IC-022: wizard reset + canonical create_project unified via
#       _create_project_record helper. Wizard projects now get ProjectMember
#       (so needs_review works first onboard) + prefix (so short_id renders
#       on first task creation). One single source of truth for project
#       creation across HTTP + wizard + future internal callers.
#   (2) IC-024: onboard endpoints surfaced in contract `endpoints.onboard`
#       (framing / scaffold / state_get / state_reset / state_ack /
#       state_complete). Previously discoverable only via workflow text.
#   (3) Suggestion C: `next_step` hint on every /onboard-state/ack +
#       /complete + GET /onboard-state response. Resumable onboard from any
#       saved state without re-parsing workflow.
#   (4) Suggestion D: `question_category` field on drift-eval 422 responses
#       (situational | forward | reflective | posture). Transparency, not
#       new game-leak — question text already telegraphs the family.
#   (5) IC-025: drift-eval suppression extended by 30-min grace window post
#       /complete (was: cleared the moment /complete landed; agents
#       immediately hit drift-eval on first real mutation).
#   (6) IC-021 + IC-023: wizard prompt tightened — milestone field is `name`
#       not `label` (per contract canonical); tooling_hash spec is concat
#       of all 4 artefacts byte-for-byte (no "or just vf_render.py" alt).
#
# R2.6 IC-026 (point release on top of R2.6 wave 1):
#   (7) IC-026: discipline manifest written to vendor-native filename.
#       Wizard prompt step 7 + bootstrap step 3 + agents_md_template
#       header all updated. Mapping: Claude Code / Claude Desktop /
#       Anthropic Agent SDK -> CLAUDE.md; Codex / Cursor / Aider /
#       generic / unsure -> AGENTS.md. Caught from Claude Desktop run #3
#       — wrote AGENTS.md per old prompt; discipline still worked because
#       Claude Desktop reads both, but native is CLAUDE.md. Forward fix
#       so multi-vendor onboards land in each vendor's auto-pickup file.
#
# R2.7 wave 1 changes (post Claude Code Flight Tracker review IC-028 + IC-029):
#   (8) VF-356 / IC-028: phases mutability via new PATCH /api/v2/phases/{id}
#       endpoint (was: write-once; Gantt phase->milestone linkage structurally
#       unfixable post-creation). Allow-list: name + milestone_id + sort_order.
#       Required `reason` (>=10 chars) per audit-quality discipline. ActivityEvent
#       stamped with actor_user_id + changes dict + reason. Plus explicit
#       DELETE-rejection (HTTP 422 with code=PHASE_NOT_DELETABLE + agent_remedy
#       naming the alternatives) so the agent recovers from the response alone.
#   (9) VF-357 / IC-029: PATCH bodies are now strict allow-lists. All *Update
#       BaseModel classes in app/api/v2/projects.py + members.py + admin.py
#       carry model_config = ConfigDict(extra='forbid'). Sending an unknown
#       field returns 422 with code=FIELD_NOT_ALLOWED_ON_PATCH + agent_remedy
#       (translated by exception handler in app/main.py). Was: silent 200
#       no-op (Claude Code review §6.2 "worst kind of API gap").
#
# Both VF-356 + VF-357 are codified per the pinned 422-recoverable principle:
# every error response carries code + detail + agent_remedy + human_visible
# so the agent can recover from the response alone.
#
# R2.7 wave 1.5 (point release on top of wave 1):
#  (10) IC-034: vf_render.py emits stderr WARN/INFO for missing or incomplete
#       YAML frontmatter at render time (was: silently degraded — TOC just
#       showed '-' for the missing fields, agent only noticed via the visible
#       symptom). Same "errors are teaching surfaces" principle as VF-357.
#       OUR-block Render & TOC Discipline gains an explicit frontmatter
#       requirement line so agents know the rule from contract refresh,
#       not by empirical discovery via the TOC. Surfaced by the Claude Code
#       agent during R2.7 wave 1 recovery validation (2026-05-02): agent
#       self-observed "my docs don't have proper frontmatter — that's why
#       the TOC shows '-' for those columns." PK directive: ship now.
#
# R2.7 wave 1.6 (audience-separation fix on top of wave 1.5):
#  (11) Scaffold artefact sanitization. vf_render.py (a customer-facing
#       artefact shipped via /onboard/scaffold) was leaking internal-jargon
#       refs (memory keys, IC-XXX tags, R2.X round refs) in its docstring
#       AND in its stderr WARN/INFO output. Customer running the renderer
#       would see "[IC-034 WARN]" in their terminal with no idea what
#       IC-034 is — internal jargon as customer-visible UI. Fix: docstring
#       rewritten in self-supporting public form (principle stated plainly,
#       no internal refs); stderr tags repointed from [IC-034 WARN/INFO] to
#       [VF-RENDER WARN/INFO] (customer-meaningful). New
#       scripts/check_scaffold_clean.py guard prevents this class of leak
#       from regressing — run from CI / pre-commit / manually before
#       merging changes to app/onboard_scaffold/. Codifies the
#       audience-separation principle (memory feedback_agent_design_discipline)
#       at write time rather than write-once-leak-forever.
#
# R2.7 wave 1.7 (audit-scope extension on top of wave 1.6):
#  (12) The 1.6 audit scope was too narrow — only checked app/onboard_scaffold/
#       and missed two other equally customer-facing surfaces. This release
#       extends the fix:
#       - contract.py customer-facing strings: 7 leaks sanitised using the
#         "drop the lead-in, keep the content" pattern. Internal change-history
#         (which version added a rule, which ticket drove it) stripped from
#         agent_enforcement.rules + task_discipline.rules + endpoints.phases
#         + endpoints.onboard.state_get notes. Customer keeps every rule's
#         full meaning; the internal-ID provenance lives in commit history
#         + FINDINGS doc + memory entries (surfaces the customer doesn't see).
#       - OUR-block source + inline copy: 2 stale [IC-034 WARN] / [IC-034 INFO]
#         references repointed to [VF-RENDER WARN] / [VF-RENDER INFO]. Two-for-one:
#         fixes both the internal-jargon leak AND the staleness (the old tags
#         no longer exist after wave 1.6).
#       - check_scaffold_clean.py extended with --our-block (scans OUR-block
#         shipped slice between BEGIN/END markers + the OUR_BLOCK_TEXT_INLINE
#         constant in onboard.py) and --live <base_url> (fetches /agentnotes
#         JSON and walks all string values, eliminating string-vs-comment
#         ambiguity in contract.py by checking the actual customer payload).
#
# R2.7 wave 1.8 (doc-class layout — scaffold + OUR-block):
#  (13) Three-class doc layout shipped via /onboard/scaffold defaults:
#       - 0-MD/0-Documentation/{internal,public,proposed}/  (lowercase)
#       - 0-MD/0-Documentation/TOC.md (NEW location; was 0-MD/TOC.md)
#       - **/archived/ subdirs preserved on disk; out of TOC
#       - .scratch/ at project root, gitignored, never in TOC
#       vf_render.py: docstring extended with new layout + audience=public
#       heuristic leak scan that flags ticket-shape codes / memory-key
#       shapes in public-audience doc bodies. Voice throughout: "AGENT
#       NEEDS TO ADAPT + ASK THE HUMAN" — ServiceNow-style framework
#       provides primitives + defaults; agent + human exercise discipline
#       suiting their use case.
#       vf_toc.py: refactored to scan the three classes via SCAN_DIRS
#       constant; EXCLUDED_PATH_PARTS={"archived"} for live-TOC filtering;
#       writes TOC into the docs root rather than parent.
#       README.md scaffold: new "Doc classes" section with layout +
#       lifecycle (proposed → graduates to internal/public via audience
#       filter; proposed/archived/ shelves abandoned ideas) + anti-
#       sycophancy brake on what warrants a proposal ("if you can't name
#       what made this worth a proposal, it's not one") + optional
#       triggered_by frontmatter field.
#       OUR-block source + inline gain one paragraph under Folder
#       Discipline pointing at the README's Doc classes section + Render &
#       TOC Discipline updated with new TOC path.
#       NOT in this wave: dogfood migration of our own 0-MD/ tree into
#       the new layout (separate ready/critical ticket — wave 1.8 = scaffold
#       guidance + customer onboard defaults only).
#
# R2.7 wave 1.8.1 (docs_state assessment on needs_review):
#  (14) Lean PATCH-field shape: docs_state enum {needed | exists | updated
#       | created | skipped} + docs_note (>=30 chars REGARDLESS of state).
#       Agents only; required on transition to needs_review. 422 returns
#       standard envelope per the pinned 422-recoverable principle:
#       code=DOCS_ASSESSMENT_REQUIRED + detail + human_visible=true +
#       agent_remedy spelling out the five states with what each means
#       and what docs_note should contain. Handler auto-posts a structured
#       TaskNote ('docs_state: <state> — <note>') into the conversation
#       feed AND an ActivityEvent with action=docs_state_assessed so the
#       assessment is visible everywhere notes/audit are read.
#       Why this shape (not a hard gate): board volume is ~2-5 needs_review
#       transitions per day in mature projects; gate-style adds persistent
#       round-trip cost + mid-flow interruption with no signal-quality gain
#       once the field is in muscle memory. PATCH-field wins on volume +
#       cost. The 30-char floor is the anti-sycophancy brake against
#       reflexive 'n/a' on 'skipped' / 'exists'.
#       Escalation path (documented in 0-MD/proposed/DOCS-STATE-ASSESSMENT.md,
#       not built): if compliance is low (less-capable model classes drift
#       on the rule), escalate to gate-style with per-project toggle
#       (operator-tunable, like drift_window_seconds). Built later as a
#       separate ticket.
#
# Bumping causes contract_drift on existing agents' next mutation -> they
# re-read /agentnotes -> get the new rules. Desired behaviour.
CONTRACT_VERSION = "2.14.5"  # VF-385: StepAck.surfaced_verbatim docstring end-marker aligned — was "carrying it cheaper" (older retired range), now matches FRAMING_TEXT + OUR-block at "Start formal; your tone takes over as the agent learns your voice." (the canonical longer range including four-practices).

# Build label rendered under the top-left logo on both the board UI and the
# admin portal. Two cases:
#   - master / dev branches: empty string here -> startup hook falls back to
#     "Pre-RC · alembic <head>" (dynamic; useful for "what build am I on?").
#   - downstream release branches (e.g. 0.7.1-RC): set to a static
#     human-readable string like "0.7.1-RC (Pre-release)".
# Intent: a colleague/customer running a downstream branch sees a stable
# label they can quote back; an internal dev sees the live alembic head.
BUILD_TAG = "0.7.1-PRE-RC"


def _get_agent_from_token(request: Request, db: Session) -> Optional[Agent]:
    """Extract Bearer token from request, look up agent by hash.

    VF-341 §4.5: token TTL is now enforced. Tokens with expires_at in the
    past auth-fail (return None). Tokens with expires_at IS NULL keep their
    existing eternal behaviour until backfilled via
    scripts/migrate_token_ttl.py.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:].strip()
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    agent = db.query(Agent).filter(Agent.api_token_hash == token_hash, Agent.status == "active").first()
    if agent is None:
        return None
    if agent.expires_at is not None:
        if agent.expires_at <= datetime.now(timezone.utc):
            return None
    return agent


# ── Unauthenticated contract (minimal) ──

def _base_from_request(request: Request) -> str:
    """Derive base URL from the incoming request instead of static config.
    WHY: a hardcoded BASE_URL points at prod even when the agent is on dev/uat.
    Customer installs also need this to reflect their actual hostname."""
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    return f"{scheme}://{host}"


def _public_contract(request: Request):
    base = _base_from_request(request)
    return {
        "product": {
            "name": "VibeForge+",
            "version": "2.0.0",
            "mode": "Project tracker with human-agent collaboration.",
        },
        "contract_version": CONTRACT_VERSION,
        "base_url": base,
        "authentication": {
            "method": "Bearer token",
            "header": "Authorization: Bearer <your-token>",
            "how_to_get_token": "Ask your human administrator to issue a token from the Config page.",
            "config_url": f"{base}/ui/config",
            "note": "Pass your token to this endpoint to receive the full contract with endpoints, rules, and project access.",
        },
        "status": "unauthenticated",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }


# ── Bootstrap section ──

def _bootstrap_section(agent: Agent, request: Request, project: Optional[Project] = None):
    """Active setup steps for first-hit onboarding."""
    base = _base_from_request(request)
    slug_ref = project.slug if project else "{project_slug}"

    return {
        "description": "First-hit setup. Execute these steps before starting any work on a new project.",
        "prerequisites": {
            "required": ["board_url", "agent_token"],
            "board_url": base,
            "note": "You have these if you are reading this authenticated response.",
        },
        "steps": [
            {
                "step": 1,
                "action": "identify_project",
                "description": "Determine which project you are working on.",
                "method": (
                    f"Your project is: {project.name} ({project.slug})"
                    if project
                    else "Check available_projects in this response. If empty, follow the onboarding_partner flow in board_capabilities — help the human plan and set up their first project. If only one active project, use it. If multiple, ask the human which to work on. Then re-request GET /agentnotes/{slug} for project-specific context."
                ),
            },
            {
                "step": 2,
                "action": "get_token_securely",
                "description": "Your agent token must be provided via a secure file — never via chat or conversation.",
                "method": [
                    "If .agent-config already exists in the project root, source it and skip to step 3.",
                    "If .agent-config does NOT exist, tell the human: 'I need my agent token. Please create .agent-config in the project root with my token, or write the token to a temp file named exactly `agent-token.txt` that I will read and you will delete after.'",
                    "NEVER ask the human to paste the token in chat. NEVER display tokens in conversation.",
                    "If the human writes a temp file (agent-token.txt), read it, create .agent-config from the template below, then tell the human in one unambiguous line: 'I have copied your token into .agent-config. Please delete the file named `agent-token.txt` (the temp one). DO NOT delete `.agent-config` — that is the persistent config we will use for every API call.'",
                    "If you have file-delete capability in your environment, prefer to delete `agent-token.txt` yourself silently after reading it, then tell the human only: 'I have deleted agent-token.txt. Your .agent-config is in place.' This avoids the wrong-file-deletion risk entirely.",
                ],
                "template": f"VIBEFORGE_API={base}/api/v2\nVIBEFORGE_TOKEN=<read-from-file>\nVIBEFORGE_PROJECT={slug_ref}\n",
                "rules": [
                    "Source this file before any API call: source .agent-config",
                    "Reference token via $VIBEFORGE_TOKEN — never hardcode or display it.",
                    "Add .agent-config AND agent-token.txt to .gitignore immediately.",
                    "When telling the human to delete `agent-token.txt`, name the file explicitly. Do not say 'delete it' or 'delete the temp file' — past sessions have had humans delete `.agent-config` instead because the wording was ambiguous.",
                ],
            },
            {
                "step": 3,
                "action": "create_rules_file",
                "description": "Create your editor's NATIVE discipline-manifest file in your working directory root. Filename matters — your editor only auto-reads its native one on session start.",
                "content_key": "agents_md_template",
                "note": "The full template is in agents_md_template. **Write it to the filename your editor reads natively** — CLAUDE.md for Claude Code / Claude Desktop / Anthropic Agent SDK; AGENTS.md for Codex / Cursor / Aider / generic / unsure (industry-standard fallback). The discipline content is identical regardless of filename; the filename determines whether your editor picks it up automatically on session start. Multi-vendor projects may write both (or write the native + a one-line pointer in the other).",
                "vendor_native_filenames": {
                    "claude_code": "CLAUDE.md",
                    "claude_desktop": "CLAUDE.md",
                    "anthropic_agent_sdk": "CLAUDE.md",
                    "codex": "AGENTS.md",
                    "cursor": "AGENTS.md",
                    "aider": "AGENTS.md",
                    "generic_or_unsure": "AGENTS.md",
                },
            },
            {
                "step": 4,
                "action": "verify_identity",
                "description": "Verify your token and identity via authenticated /me.",
                "command": "source .agent-config && curl -sL -H \"Authorization: Bearer $VIBEFORGE_TOKEN\" \"$VIBEFORGE_API/me\"",
                "expected": "200 OK with agent identity, project scope, my_tasks counts, and reviewers list.",
            },
            {
                "step": 5,
                "action": "fetch_project_contract",
                "description": "Re-fetch the project-scoped contract for actionable details. The unauthenticated /agentnotes is only a stub — the project-scoped version has full endpoints, rules, and reviewers list.",
                "command": f"source .agent-config && curl -sL -H \"Authorization: Bearer $VIBEFORGE_TOKEN\" \"$VIBEFORGE_API/agentnotes/{slug_ref}\"",
                "expected": "200 OK with full contract including endpoints, board_capabilities, design_principles, agent_enforcement.",
                "note": "ALWAYS use the authenticated /agentnotes/{slug} variant. Public /agentnotes returns minimal stub.",
            },
            {
                "step": 6,
                "action": "verify_connectivity",
                "description": "Sanity check by fetching tasks.",
                "command": f"source .agent-config && curl -sL -H \"Authorization: Bearer $VIBEFORGE_TOKEN\" \"$VIBEFORGE_API/projects/{slug_ref}/tasks\"",
                "expected": "200 OK with JSON array of tasks.",
            },
            {
                "step": 7,
                "action": "check_in",
                "description": "Announce yourself on the board.",
                "method": "Read your assigned tasks (filter by your agent name). If any are 'ready', pick one up (move to in_progress with transition_note). If none assigned, ask the human what to work on.",
            },
            {
                "step": 8,
                "action": "internalise_live_check_rule",
                "description": "Commit to memory: any time the human asks you about board state, you fetch fresh before answering. Always.",
                "rule": "If the human asks 'is there anything for me?', 'what's the status of X?', 'who owns Y?', 'what's open?' — issue a fresh GET against the board (e.g. /me, /projects/{slug}/tasks, /tasks/{id}) BEFORE you reply. Do NOT answer from session memory of prior tool outputs, even if they are seconds old. The board is the live single source of truth shared with the human in real time. Cache-answers turn that contract into decoration.",
            },
        ],
        "post_setup": "You are now bootstrapped. Follow the rules in your CLAUDE.md. Use the workflows in this contract for checktasks (session start) and sync (session end). Default reflex on any state question from the human: GET first, answer second.",
    }


# ── Discipline manifest template (filename-agnostic) ──

def _claude_md_template(agent: Agent, request: Request, project: Optional[Project] = None):
    """Generate a minimal discipline-manifest pointer. Filename-agnostic —
    written to CLAUDE.md by Claude vendors, AGENTS.md by Codex / Cursor /
    generic. See bootstrap.steps[3].vendor_native_filenames for the mapping.
    Rules live in the contract, not in this template."""
    base = _base_from_request(request)
    slug = project.slug if project else "{project_slug}"
    name = project.name if project else "{project_name}"
    prefix = (project.prefix if project and project.prefix else slug.upper()[:3])

    return f"""# Discipline manifest — {name}
# Board: {base} | Project: {slug} ({prefix})
# Write this file as CLAUDE.md (Claude vendors) or AGENTS.md (Codex / Cursor / generic)
# per your editor's native filename. Content is identical either way.

## Security — MANDATORY

- Source credentials from `.agent-config` and reference via $VIBEFORGE_TOKEN.
- Never display tokens in shell commands, curl output, or chat.
- `.agent-config` MUST be in .gitignore.
- If credentials are needed and no config file exists, ask the human.

## Board

API: {base}/api/v2
Contract: GET /agentnotes/{slug}
Identity: GET /me

Read your project contract at the endpoint above for all API endpoints,
task rules, enforcement gates, workflows, and session protocol.

## Project Rules

Add project-specific rules below as you discover them during work.
This section is yours — the board provides the contract, you provide
the local context (tech stack, conventions, human preferences).
"""


# ── Workflows section ──

def _build_sync_workflow(base: str, slug: str, project: Optional[Project]):
    """End-of-session reconciliation workflow. The regenerate_contract_html step
    is dogfood-only — it only fires when an agent modifies app/api/v2/contract.py,
    which only happens in the vibeforge-plus repo. Customer projects don't carry
    contract.py, so the step is filtered out for them. Step numbers stay sequential
    per audience to avoid a confusing gap."""
    is_dogfood = bool(project and project.slug == "vibeforge-plus")

    base_steps = [
        {
            "name": "task_reconciliation",
            "action": "Fetch all open tasks. For each task you worked on this session:",
            "sub_steps": [
                "If you did work but didn't add notes → add a detailed note summarising what was done, decisions made, and reasoning.",
                "If status should have changed but didn't → update with transition_note.",
                "If task is in needs_review → ensure assigned to human, not agent.",
                "If task was completed by human → respect their status, only add missing notes.",
                "Notes are the shared memory across sessions — write them for the next agent or human who picks up this task.",
            ],
            "endpoint": f"GET {base}/api/v2/projects/{slug}/tasks/",
            "rules": [
                "Always include transition_note on status changes.",
                "Include @mention of reviewer on needs_review tasks.",
                "Never move to done — only human does that.",
                "Check audit trail if unsure who changed what last.",
            ],
        },
        {
            "name": "git_commit",
            "action": "Stage and commit changes in working directory.",
            "rules": [
                "Include descriptive commit message summarising session work.",
                "Include Co-Authored-By header for the agent.",
                "Skip if working tree is clean.",
            ],
        },
    ]
    if is_dogfood:
        base_steps.append({
            "name": "regenerate_contract_html",
            "action": "Regenerate the contract HTML reference if contract.py was modified this session.",
            "command": "source .agent-config && export VIBEFORGE_API VIBEFORGE_TOKEN && node scripts/generate_contract_html.js > 0-MD/0-Documentation/public/AGENT-CONTRACT.html",
            "rules": [
                "Only run if app/api/v2/contract.py was in the git diff for this session.",
                "Include the regenerated HTML in the git commit.",
            ],
        })
    base_steps.extend([
        {
            "name": "git_push",
            "action": "Push to remote. Report the commit hash. Never force push.",
        },
        {
            "name": "update_resume",
            "action": "PUT a concise project resume summarising current state.",
            "endpoint": f"PUT {base}/api/v2/projects/{slug}/resume",
        },
        {
            "name": "session_summary",
            "action": "Output a clean summary of tasks updated, commits pushed, and what's next.",
        },
    ])

    for i, step in enumerate(base_steps, start=1):
        step["step"] = i
        # Move 'step' key to the front for readability
        base_steps[i - 1] = {"step": i, **{k: v for k, v in step.items() if k != "step"}}

    n = len(base_steps)
    return {
        "name": "sync",
        "trigger": "Session end, or when asked to sync.",
        "description": "End-of-session reconciliation — update board, commit code, summarise.",
        "steps": base_steps,
        "priority_order": f"If context window is low, prioritise: tasks (1) → git (2-{n - 2}) → resume ({n - 1}) → summary ({n}).",
    }


def _workflows_section(agent: Agent, request: Request, project: Optional[Project] = None):
    """Embedded workflow definitions — replaces external skill files."""
    base = _base_from_request(request)
    slug = project.slug if project else "{project_slug}"
    agent_name = agent.name

    return {
        "checktasks": {
            "name": "checktasks",
            "trigger": "Session start, or when asked to check tasks.",
            "description": f"Fetch tasks assigned to you ({agent_name}) in project {slug}.",
            "steps": [
                "Source credentials from .agent-config (never display tokens).",
                f"GET {base}/api/v2/projects/{slug}/tasks/",
                f"Filter for tasks where assigned_to or owner_label contains '{agent_name}' or 'agent' (case-insensitive).",
                "Exclude terminal statuses (done, cancelled).",
                "Group results by status and display formatted summary.",
                "If no tasks assigned, inform the human and ask what to pick up.",
                "For whichever task you pick up, GET its notes BEFORE starting work — notes contain design decisions and context.",
            ],
            "display_format": "\n".join([
                "═══ MY TASKS ({agent_name}) ═══",
                "",
                "In Progress:",
                "  • {prefix}-{num}: {title}",
                "",
                "Ready:",
                "  • {prefix}-{num}: {title}",
                "",
                "Needs Review:",
                "  • {prefix}-{num}: {title}",
                "",
                "Backlog:",
                "  • {prefix}-{num}: {title}",
                "",
                "Total: {count} assigned",
            ]),
            "rules": [
                "Only show non-terminal tasks (exclude done, cancelled).",
                "Always fetch fresh from the API — never use cached data.",
                "Never display tokens in commands or output.",
            ],
        },
        "sync": _build_sync_workflow(base, slug, project),
    }


# ── Authenticated contract (full) ──

def _full_contract(agent: Agent, db: Session, request: Request, project_slug: str | None = None, slim: bool = False):
    base = _base_from_request(request)
    projects = (
        db.query(Project.id, Project.slug, Project.name, Project.status)
        .filter(Project.status == "active")
        .order_by(Project.name)
        .all()
    )

    # Resolve project if slug provided
    project = None
    if project_slug:
        project = db.query(Project).filter(Project.slug == project_slug).first()

    contract = {
        "product": {
            "name": "VibeForge+",
            "version": "2.0.0",
            "mode": "PostgreSQL-backed project tracker with MC theme",
            "intended_use": "Human + AI agent collaboration on projects.",
        },
        "contract_version": CONTRACT_VERSION,
        # Surface-version fingerprints. Each customer-facing surface that
        # ships independently of the contract has its own version; agents
        # use this map to detect stale local caches (e.g. scaffold artefacts
        # written to disk at one fetch, contract refreshed later — without
        # this map there is no signal the artefacts are now behind the
        # current bundle). On every audit the agent compares its locally-
        # cached surface version against the value here; mismatch -> re-fetch.
        # Each version MUST be kept in sync with its source-of-truth constant
        # (named per surface). Bidirectional comments at both ends are the
        # auto-reminder; bump here and at the source together or the audit
        # signal lies.
        "surfaces": {
            "scaffold": {
                # MUST match SCAFFOLD_VERSION in app/api/v2/onboard.py.
                "version": "2.4.0",
                "endpoint": "/api/v2/onboard/scaffold",
                "note": "Tool bundle (vf_render, vf_toc, template, README). Re-fetch when this version advances past the value you cached on disk. v2.4.0 (wave 2.0.8 R2): README handover-cycle section gains explicit placement-convention callout — handover docs live at `0-MD/progress/` (peer to `0-MD/0-Documentation/`, NOT under it) because they are session-continuity escape-hatch artefacts, not durable documentation. Doc-classes diagram updated to show `0-MD/progress/` with the categorical-separation note. Resolves PK contribution from Codex blind cross-vendor onboard findings about visual confusion when handovers sit under same `0-MD/` root. v2.3.0 (wave 2.0.7): README gains a `Handover → compact → absorb` section documenting the session-continuity cycle (the agent's source material for substep 4 compaction_practice — when human asks 'walk me through it', agent reads from there). v2.2.0 (wave 2.0.5): vf_toc.py orchestrates render-then-index; per-output dependency contracts (TOC.md→.md, TOC.html→.html); paths relative to each output's location; verify-and-warn on missing link targets; template gains index audience-pill + toc-helper styles; README ships with frontmatter.",
            },
        },
        "base_url": base,
        "status": "authenticated",

        "agent": {
            "id": agent.id,
            "name": agent.name,
            "slug": agent.slug,
            "model_type": agent.model_type,
            "model_name": agent.model_name,
            "project_id": agent.project_id,
            "created_by": agent.created_by,
        },

        "discovery": [
            "1. You are authenticated. This is your full contract.",
            "2. Read the bootstrap section and follow the setup steps.",
            "3. Use the workflows section for checktasks (session start) and sync (session end).",
            "4. The agents_md_template contains rules — write to the filename your editor reads natively (CLAUDE.md for Claude vendors; AGENTS.md for Codex / Cursor / generic). See bootstrap.steps[3] for the full vendor→filename mapping.",
            "5. All API endpoints are listed below. Use them with your Bearer token.",
            "6. Begin work. Update tasks as you go.",
        ],

        "status_enum": ["backlog", "ready", "in_progress", "needs_review", "blocked", "done", "cancelled"],
        "priority_enum": ["low", "medium", "high", "critical"],

        "priority_matrix": {
            "critical": {
                "sla": "Same session — drop everything",
                "criteria": "Data loss risk, app down, security breach, deploy broken",
                "action": "Fix immediately. Do not start other work until resolved.",
            },
            "high": {
                "sla": "Next session",
                "criteria": "Blocks human workflow or agent operations, core UX broken",
                "action": "Prioritise at start of next session.",
            },
            "medium": {
                "sla": "Within 3 sessions",
                "criteria": "Important but workaround exists, feature gaps, non-blocking improvements",
                "action": "Schedule when high-priority queue is clear.",
            },
            "low": {
                "sla": "Backlog",
                "criteria": "Nice to have, cosmetic, deferred by design, future planning",
                "action": "Pick up opportunistically or when relevant work is nearby.",
            },
        },

        "board_capabilities": {
            "summary": "VibeForge+ is a self-hosted project tracker designed for human-AI collaboration. Agents are first-class participants alongside humans.",
            "structure": {
                "project": {
                    "description": "Top-level container for all work. Has name, slug, description, status.",
                    "minimum_to_create": "name (slug auto-generated if omitted)",
                    "lifecycle": "active → completed (with celebration) → archived. Can be reopened.",
                    "created_by": "Human only. Agents cannot create projects — they guide humans through planning.",
                },
                "milestone": {
                    "description": "Major project checkpoint (e.g. 'Foundation', 'Auth & Access', 'Agent Platform').",
                    "purpose": "Grouping and filtering. Shows as filter chips on board, collapsible rows in Gantt.",
                    "optional": True,
                    "examples": ["A: Foundation", "B: Auth & Access", "C: Agent Platform"],
                },
                "phase": {
                    "description": "Work grouping within a milestone (e.g. 'Identity', 'Tokens', 'Layout').",
                    "purpose": "Shown as badge on task cards. Swimlane dividers in board columns.",
                    "belongs_to": "milestone",
                    "examples": ["Identity", "Tokens", "Permissions", "Verification"],
                },
                "task": {
                    "description": "Unit of work. Card on board, bar in Gantt.",
                    "fields": "title, short_description (card face), description (detail), status, priority, owner, task_type, phase, dates",
                    "types": "feature, bug, chore, spike, verification",
                    "lifecycle": "backlog → ready → in_progress → needs_review → done. Also: blocked, cancelled.",
                },
            },
            "collaboration_model": {
                "humans": "Create projects, make priority decisions, close tasks, post completion notes.",
                "agents": "Pick up tasks, write code, post structured notes, move to needs_review for human validation.",
                "notes": "Shared memory between humans and agents across sessions. Immutable — use supersede to correct.",
                "audit_trail": "Every field change logged with actor identity and timestamp.",
            },
            "planning_guidance": {
                "description": "When helping a human plan a new project, follow this approach:",
                "steps": [
                    "Read any existing docs, plans, or requirements the human provides.",
                    "Propose milestones for major project phases (3-6 typically).",
                    "Under each milestone, propose phases for work groupings.",
                    "Break phases into tasks with clear titles, types, and priorities.",
                    "Once the human creates the project on the board, re-discover via GET /agentnotes and create the structure via API.",
                    "Post the rationale as notes on key tasks for future context.",
                ],
            },
            "project_creation_guide": {
                "description": "How projects are created. Agents cannot create projects — they guide humans.",
                "minimum_fields": {
                    "name": "Project name (required). Human-readable, descriptive.",
                    "slug": "URL-safe identifier (optional — auto-generated from name if omitted).",
                    "description": "What the project is about (optional but recommended).",
                },
                "created_via": "Admin portal (SA) or board UI (SU/User). Not via API for agents.",
                "after_creation": "Agent re-requests GET /agentnotes to discover the new project in available_projects. Then GET /agentnotes/{slug} for project-specific context.",
            },
            "onboarding_partner": {
                "description": "When no projects exist or agent has no assigned projects, the agent becomes an onboarding partner.",
                "flow": [
                    "1. Agent authenticates → available_projects is empty.",
                    "2. Agent reads board_capabilities to understand what the board can do.",
                    "3. Agent explains to the human: what a project is, what milestones/phases/tasks are, how the board works.",
                    "4. Agent asks: 'Do you have a plan, requirements doc, or idea you want to start with?'",
                    "5. If human provides docs/plan → agent reads them and proposes milestone/phase/task structure.",
                    "6. If human has just an idea → agent helps break it down into milestones and tasks through conversation.",
                    "7. Agent tells human: 'Create the project on the board (via Admin or UI), then tell me the project name.'",
                    "8. Human creates project → agent re-requests GET /agentnotes → sees new project.",
                    "9. Agent creates milestones, phases, and tasks via API from the prepared plan.",
                    "10. Agent posts rationale notes on key tasks for future session context.",
                ],
                "key_principles": [
                    "Agent is a partner, not a gatekeeper. Help the human get started with minimum friction.",
                    "Don't overwhelm with structure — start simple, add milestones/phases as the project grows.",
                    "If the human just wants to start coding, a single milestone with a few tasks is fine.",
                    "The board adapts to the project, not the other way around.",
                ],
            },
        },

        "design_principles": {
            "summary": "Guiding principles for all work. These inform HOW to think, not just what to do.",
            "engineering": [
                {"name": "DRY", "rule": "One auth check, one validation, one source of truth. Don't duplicate logic."},
                {"name": "KISS", "rule": "Simplest solution that works. No speculative abstractions. Three similar lines beat a premature abstraction."},
                {"name": "Secure by Default", "rule": "New routes locked by default. Opt-in to public, not opt-in to secure."},
                {"name": "Least Privilege", "rule": "Request minimum access. Don't store what you don't need. Agents get scoped tokens, not global keys."},
                {"name": "Defence in Depth", "rule": "Contract rules + API gates + UI guards. Multiple layers. Don't rely on one check."},
                {"name": "Fail Secure", "rule": "Auth fails = deny. Parse fails = reject. Unknown state = stop and ask human."},
                {"name": "No Secrets in Output", "rule": "NEVER put credentials in notes, logs, API responses, chat, or shell history. Use [REDACTED] if referencing a password action."},
                {"name": "Least Knowledge", "rule": "Components only know what they need. Don't leak internal details across boundaries."},
                {"name": "Separation of Concerns", "rule": "Each role, endpoint, and component has one job. SA is not SU. Admin portal is not board."},
            ],
            "work": [
                {"name": "Document Decisions", "rule": "Code comments explain WHY, not what. Notes explain rationale. If you made a choice, say why."},
                {"name": "Verify Before Asserting", "rule": "Test what you built. Don't assume it works because the code looks right. Check the actual output."},
                {"name": "Complete the Handoff", "rule": "Every needs_review has: structured note, test steps, @mention, human assignment. No exceptions."},
                {"name": "Contract is Law", "rule": "Read your own contract. Follow it. If it's wrong, fix the contract first, then the code."},
                {"name": "Atomic Deploys", "rule": "Deploy all related changes together. Regenerate derived artefacts (HTML, docs). No half-deployed states."},
                {"name": "No Assumptions", "rule": "Fetch fresh data. Check the model. Read the error. Don't guess from memory or cached state."},
                {"name": "Audit Your Own Work", "rule": "Before marking complete, re-read what you changed. Check notes for secrets. Verify intent matches output."},
                {"name": "Flag Debt", "rule": "When taking a shortcut, create a backlog task explicitly. No silent tech debt. Human sees what is deferred."},
            ],
            "visibility": [
                {"name": "Explain Trade-offs", "rule": "When making a choice, explain what was gained AND what was sacrificed. Not just 'I did X' but 'I chose X over Y because Z'."},
                {"name": "Estimate Impact", "rule": "Before making a change, state what it touches: files, flows, DB changes. Human knows the blast radius."},
                {"name": "Seek Approval on Architecture", "rule": "Propose new patterns, human approves. Small fixes are fine. New patterns need sign-off."},
                {"name": "Plain English First", "rule": "Every completion note starts with a one-line plain English summary before technical detail. Human should not need to parse HTML."},
                {"name": "Revert Path", "rule": "For significant changes, document how to undo: backup reference, git commit to revert to, steps."},
                {"name": "No Silent Side Effects", "rule": "If fixing one thing changes another, say so explicitly. Don't hope nobody notices."},
                {"name": "Progress Visibility", "rule": "Human should never need to ask 'what are you doing'. Task status, notes, and transition_notes make it obvious."},
            ],
        },

        "code_commentary": {
            "summary": "Code should be self-documenting. Comments explain why, not what. Enables fresh AI to review and generate docs independently.",
            "conventions": [
                "# WHY: explains the business or security reason this code exists",
                "# RULE: references the contract rule being enforced (e.g. RULE: agent cannot move to done)",
                "# FLOW: describes the user/agent journey through this entry point",
                "# GATE: marks a permission or validation checkpoint",
            ],
            "documentation_versioning": "All generated documentation is versioned alongside the contract. Version resets at v1.0.0 RC.",
        },

        "task_fields": {
            "title": "Plain text only. HTML tags are rejected with 422. The WHAT in plain language.",
            "short_description": "Max 120 chars, plain text only. Shown on board card face. HTML rejected with 422.",
            "description": "Plain text only. HTML rejected with 422. Full detail in plain language - implementation notes, acceptance criteria, context. Captured reasoning, lists, sections, code, links go in a NOTE (POST /tasks/{{task_id}}/notes), not in description. Description answers 'what is this task'; notes answer 'what was discussed, decided, or done'.",
            "task_type": "One of: feature, bug, chore, spike, verification. Set on creation.",
        },

        "task_discipline": {
            "summary": "Keep tasks current without being prompted. Read before writing.",
            "rules": [
                "BEFORE creating a new task, GET /api/v2/projects/{{slug}}/tasks and filter the response client-side to OPEN tickets only (statuses: backlog, ready, in_progress, needs_review, blocked — NOT done or cancelled, those are history). Scan for related or duplicate work. For hard dependencies, set blocked_by_task_id (with blocked_by_reason >= 10 chars per VF-304). For soft relations, use POST /api/v2/tasks/{{task_id}}/related with other_task_id + reason (>= 10 chars) — this creates an audit-trailed, idempotent, queryable link visible in the UI relationship drawer (GET /api/v2/tasks/{{task_id}}/relationships). Inline prose like 'related: VF-123' in descriptions is unqueryable + unidirectional + drops the audit signal — avoid. If unsure whether a candidate is truly related, list the candidates and confirm with the human before linking. Don't accumulate orphan tickets.",
                "WHEN creating a new task, set phase_id (and milestone_label) appropriate to the work area. Do not leave new tasks in the default 'Triage' phase. Use GET /api/v2/projects/{{slug}}/phases to enumerate available phases and their milestones; pick the one that matches the work surface. If genuinely uncertain, ask the human or include a transition_note explaining why Triage is the deliberate choice. SERVER-ENFORCED on agent POST: missing phase_id or resolution to default Triage WITHOUT transition_note (>=30 chars) returns 422 with code=PHASE_REQUIRED_ON_CREATE + agent_remedy listing available phases + the deliberate-Triage escape-hatch. Humans not gated.",
                "BEFORE starting work on any task, GET its notes (GET /api/v2/tasks/{{task_id}}/notes). Notes contain design decisions, reasoning, prior context, and instructions from humans or other agents. Do not skip this.",
                "When a human approves a plan or direction, update the task before continuing.",
                "Move to in_progress when approved work begins. Include a transition_note describing what you will do.",
                "Every status transition MUST include a transition_note explaining why. No silent moves. For agent transitions, the API auto-posts it as a visible TaskNote in the notes feed. Human transitions log to activity timeline only.",
                "Note-fidelity gate (agents only): for needs_review and blocked, transition_note must be >= 40 chars, must not duplicate the previous transition note, and blocked must reference the blocker (set blocked_by_task_id or mention it in the note). 422 on failure with specific error.",
                "When setting, changing, or clearing blocked_by_task_id on a PATCH, MUST include blocked_by_reason (>= 10 chars) explaining the dependency. Required for cross-session traceability — without a captured reason, future agents inherit the dependency without knowing why. 422 on failure.",
                "Docs assessment on needs_review (agents only): when transitioning a task to needs_review, MUST set docs_state to one of {needed, exists, updated, created, skipped} AND docs_note (>= 30 chars) describing what was assessed. The 30-char floor applies regardless of state — anti-sycophancy brake against reflexive 'n/a'. 422 with code=DOCS_ASSESSMENT_REQUIRED + agent_remedy on failure. The handler auto-posts a structured TaskNote ('docs_state: <state> — <note>') so the assessment is visible in the conversation feed, not just the PATCH payload. AGENT NEEDS TO ADAPT the docs_note to your project: name doc paths, audience class, sections changed; for 'skipped' explain why no doc work belongs in this scope. ASK THE HUMAN if unsure whether a borderline change warrants 'updated' vs 'exists'.",
                "Update tasks immediately when state changes - do not batch.",
                "Move to needs_review when work needs human validation or sign-off. MUST include owner_label EXPLICITLY in this PATCH body using the format 'human:<Display Name>' — for example owner_label='human:Parvez Khan'. CONTRACT_VERSION 2.14.1: server no longer falls back to existing task.owner_label. Every needs_review transition is an active reassignment so the agent thinks about WHO is reviewing — passive ride-through of a stale owner from a prior assignment defeats the handoff intent. Bare 'human' (no colon), 'agent:<name>', or any value not matching 'human:<non-empty Name>' WILL be rejected with 422. At session start, resolve the human reviewer from GET /projects/{slug}/members (filter type=human, take .name) and prepend 'human:' yourself. If no human member exists, ask the human to have an admin add them as a project member before proceeding. Never hardcode reviewer names.",
                "Agents CANNOT move to done or cancelled — see agent_enforcement. Prefer needs_review when uncertain.",
                "cancelled requires non-empty abandoned_note.",
                "Add detailed notes to tasks documenting decisions, implementation details, and reasoning — not just status changes. Notes are the shared memory between agents and humans across sessions.",
                "When moving to needs_review, include @mention of the reviewer in the note.",
                "Agent API calls are identified by Bearer token — audit trail shows agent name, not human.",
                "Agents MUST NOT delete notes. Use supersede instead — POST /tasks/{{task_id}}/notes/{{note_id}}/supersede with a reason. Agents can only supersede their own notes.",
                "When writing notes, use structured HTML: <strong>Problem:</strong>, <strong>Fix:</strong>, <strong>Scope:</strong> sections with <ul><li> bullets. For needs_review notes, add <strong>Test steps:</strong> with <ol><li> numbered steps the human can follow to verify. Makes notes scannable across sessions.",
                "Every API write (POST, PATCH, PUT, DELETE) MUST verify the response. Check HTTP status AND response body. If not 2xx or body doesn't match what was sent, report the error immediately. Never assume success.",
            ],
        },

        "sync_expectations": {
            "on_task_change": "Update task status immediately via PATCH API.",
            "on_note_added": "POST to /api/v2/tasks/{{task_id}}/notes with author_type='agent'.",
            "on_session_end": "PUT resume summary via /api/v2/projects/{{slug}}/resume.",
            "periodic": "Every 5 significant actions, re-read project tasks to verify board matches reality.",
            "on_mention": "Check /api/v2/triggers/mention for pending mentions addressed to you.",
        },

        "board_reconciliation": {
            "summary": "The board is a collaboration tool. Human edits live. Always fetch fresh before writing.",
            "rules": [
                "Before any status update, GET /api/v2/projects/{{slug}}/tasks to read current state.",
                "Never rely on cached or remembered task states from earlier in the conversation.",
                "If a task has moved since your last read, the human change takes priority.",
                "Notes are additive — always safe to post regardless of status changes.",
                "Status changes are competitive — latest human action wins.",
                "If you did work but the human already moved the task further, post your note but skip the status change.",
                "If the human moved a task back (e.g. needs_review back to in_progress), respect their decision. Ask if unclear.",
                "Use activity_events to determine who changed what and when if reconciliation is ambiguous.",
                "If your unposted work means the task is further along than the human's status reflects, post the note AND advise the human that the status may need updating. Do not silently accept a stale human status when you have newer context.",
            ],
        },

        "hierarchy": {
            "structure": "Project > Milestone > Phase > Task",
            "milestone": "Filter/grouping concept. NOT on card face.",
            "phase": "Work context. Badge on card face.",
            "task": "Card on board, bar in Gantt.",
        },

        "endpoints": {
            "tasks": {
                "list":   {"method": "GET",   "path": "/api/v2/projects/{slug}/tasks"},
                "get":    {"method": "GET",   "path": "/api/v2/tasks/{task_id}"},
                "create": {"method": "POST",  "path": "/api/v2/projects/{slug}/tasks",
                           "note": "Agent POSTs are gated on phase_id (or transition_note >=30 chars if Triage is deliberate). Missing/Triage without rationale returns 422 with code=PHASE_REQUIRED_ON_CREATE + agent_remedy. Humans not gated.",
                           "body": "{title, short_description, description, status, priority, owner_label, task_type, phase_id, milestone_label, start_date, due_date, transition_note}"},
                "update": {"method": "PATCH", "path": "/api/v2/tasks/{task_id}",
                           "note": "Status changes require transition_note. needs_review must set human owner AND docs_state {needed|exists|updated|created|skipped} + docs_note (>=30 chars) — agents only; auto-posts as a structured TaskNote. Agent cannot move to done. Mutating blocked_by_task_id (set/change/clear) requires blocked_by_reason >= 10 chars.",
                           "body": "{status, transition_note, owner_label, priority, title, short_description, description, task_type, phase_id, phase_change_reason, start_date, due_date, blocked_by_task_id, blocked_by_reason, docs_state, docs_note, abandoned_note}"},
                "audit":  {"method": "GET",   "path": "/api/v2/tasks/{task_id}/audit"},
            },
            "artefacts": {
                "get":           {"method": "GET",  "path": "/api/v2/projects/{slug}/artefacts/{type}",
                                  "note": "Wave 2.0.8 R3: KISS read-only artefact fetch over already-persisted onboard_state. Type-routed: plan + agent_md return content + hash from onboard_state; contract is a 308 redirect to /agentnotes/{slug}; handover returns 404 with FS pointer (not server-captured — see proposal VF-372). Unknown types return 404 with supported_types list. Use this for cross-vendor cold-start when filesystem access isn't available; hash is authoritative for drift detection.",
                                  "supported_types": ["plan", "agent_md", "contract", "handover"]},
            },
            "relationships": {
                "list":           {"method": "GET",  "path": "/api/v2/tasks/{task_id}/relationships",
                                   "note": "Returns all linked tasks (related + blocked_by + blocking). Powers the UI relationship drawer. Use this to discover existing links before adding new ones."},
                "related_create": {"method": "POST", "path": "/api/v2/tasks/{task_id}/related",
                                   "note": "Soft (non-blocking) relation between two tickets. Stored canonically (lower-UUID side); POST from either task creates the same edge. Idempotent: duplicate target returns RELATED_ALREADY_LINKED. Use this instead of inline 'related: VF-XXX' prose in descriptions — structured links are queryable, bidirectional, and surface in the UI drawer; inline prose drops the audit signal.",
                                   "body": "{other_task_id, reason}  (reason >= 10 chars; 422 RELATED_REASON_REQUIRED on missing/short)"},
                "blocks_create":  {"method": "POST", "path": "/api/v2/tasks/{task_id}/blocks",
                                   "note": "Reverse-blocked-by — sets the TARGET task's blocked_by_task_id = this task. Effectively says 'this task blocks the target'. Rejects if target already has a blocker (1:1 invariant; offer Related as alternative). For the more common 'this task is blocked' direction, set blocked_by_task_id directly via PATCH /tasks/{task_id} (with blocked_by_reason >= 10).",
                                   "body": "{target_task_id, reason}  (reason >= 10 chars)"},
            },
            "notes": {
                "list":      {"method": "GET",  "path": "/api/v2/tasks/{task_id}/notes"},
                "create":    {"method": "POST", "path": "/api/v2/tasks/{task_id}/notes",
                              "note": "Agent identity enforced: author_type/name forced. is_completion_note forced false. is_internal is human-only — agents 422 if true is sent; agent reads receive a filtered view excluding internal notes.",
                              "body": "{body, author_type, author_name, is_completion_note, is_internal}"},
                "supersede": {"method": "POST", "path": "/api/v2/tasks/{task_id}/notes/{note_id}/supersede",
                              "note": "Agents can only supersede own notes.",
                              "body": "{reason}"},
                "revert":    {"method": "POST", "path": "/api/v2/tasks/{task_id}/notes/{note_id}/revert",
                              "body": "{reason}"},
            },
            "milestones": {
                "list":   {"method": "GET",  "path": "/api/v2/projects/{slug}/milestones"},
                "create": {"method": "POST", "path": "/api/v2/projects/{slug}/milestones",
                           "note": "Field is 'name', not 'title'.",
                           "body": "{name, sort_order}"},
                "close":  {"method": "POST", "path": "/api/v2/milestones/{milestone_id}/close"},
                "reopen": {"method": "POST", "path": "/api/v2/milestones/{milestone_id}/reopen"},
            },
            "phases": {
                "list":   {"method": "GET",  "path": "/api/v2/projects/{slug}/phases"},
                "create": {"method": "POST", "path": "/api/v2/projects/{slug}/phases",
                           "body": "{name, milestone_id, sort_order}"},
                "update": {"method": "PATCH", "path": "/api/v2/phases/{phase_id}",
                           "note": "Phase mutability via PATCH. Body is strict allow-list (extra fields rejected with 422 + agent_remedy). Required `reason` (>=10 chars) — captured in the audit trail. ActivityEvent stamped per change. To clear milestone link (unlink phase from milestone), send milestone_id=\"\" (empty string).",
                           "body": "{name?, milestone_id?, sort_order?, reason}"},
                "delete": {"method": "DELETE", "path": "/api/v2/phases/{phase_id}",
                           "note": "Explicit rejection. Phases are append-only; DELETE returns 422 with code=PHASE_NOT_DELETABLE + agent_remedy naming the alternatives (rename to mark deprecated, reassign tasks, future archive). Do not call DELETE; use PATCH to mutate or rename."},
            },
            "members": {
                "list":        {"method": "GET",  "path": "/api/v2/projects/{slug}/members",
                                "note": "Resolve human reviewer for needs_review here."},
                "mentionables": {"method": "GET",  "path": "/api/v2/projects/{slug}/mentionables"},
                "add":         {"method": "POST", "path": "/api/v2/projects/{slug}/members"},
            },
            "project": {
                "resume":    {"method": "PUT", "path": "/api/v2/projects/{slug}/resume",
                              "body": "{resume_summary}"},
                "dashboard": {"method": "GET", "path": "/api/v2/projects/{slug}/dashboard"},
            },
            "triggers": {
                "mention": {"method": "POST", "path": "/api/v2/triggers/mention",
                            "note": "Placeholder — not yet implemented."},
            },
            "onboard": {
                "framing":         {"method": "GET",  "path": "/api/v2/onboard/framing",
                                     "note": "Returns framing_text + our_block_text. Both used by the first-onboard sequence. Wave 2.0: framing_text now describes the substep order including .scratch/ + tooling-at-step-2 lite hints. Wave 2.0.1 (Codex pass-2): payload version bumped from 1 -> '1.1' + new wave:'2.0' anchor field; framing TEXT bytes changed in wave 2.0 and the explicit version makes that legible to humans/debug (agents that hash framing already caught the diff). 401 returns the standard envelope mirroring /agentnotes' unauthenticated tier (probe /agentnotes to recover)."},
                "scaffold":        {"method": "GET",  "path": "/api/v2/onboard/scaffold",
                                     "note": "Returns the 4 default tool artefacts (vf_render.py, vf_toc.py, template.html, README.md) the agent materialises at 0-MD/.tools/ during onboard step 2 (was step 4 pre-wave-2.0). 401 returns the standard envelope."},
                "state_get":       {"method": "GET",  "path": "/api/v2/projects/{slug}/onboard-state",
                                     "note": "Returns current onboard_state JSONB + derived flags: `complete` (gate-cleared = agent_md_hash + completed_at) AND `fully_complete` (also requires first_close_complete substep). Wave 2.0: next_step now returns first_close_complete after agent_md_hash if not yet stamped."},
                "state_reset":     {"method": "POST", "path": "/api/v2/projects/{slug}/onboard-state/reset",
                                     "note": "Clears onboard_state to {}. Test-loop reset; backward escape for the wizard close-pending state."},
                "state_ack":       {"method": "POST", "path": "/api/v2/projects/{slug}/onboard-state/ack",
                                     "note": "Register one onboard step (framing_acknowledged | tooling_hash | doc_complexity | compaction_practice | plan_hash). Substep order (wave 2.0.7): framing → tooling → doc_complexity → compaction_practice → plan → manifest → first_close (7 substeps). UX rhythm: heavy → silent → light → medium → planning. Specificity Discipline forcing functions: framing_acknowledged requires surfaced_verbatim=true + surfaced_summary >=150 chars + human_ack >=8 chars (3-field gate; agent must ASK + WAIT, fabricating defeats it); compaction_practice (NEW wave 2.0.7) requires compaction_practice_ack >=4 chars (`skip` is shortest valid escape — preserves operator agency, agent surfaces practice later when it matters); tooling_hash requires defaults_summary >=80 chars + defaults_applied (non-empty list). Response includes `next_step` hint.",
                                     "body": "{step, value, surfaced_summary?, surfaced_verbatim?, human_ack?, compaction_practice_ack?, defaults_summary?, defaults_applied?}"},
                "state_complete":  {"method": "POST", "path": "/api/v2/projects/{slug}/onboard-state/complete",
                                     "note": "Register agent_md_hash + clear the onboard write-gate. Wave 2.0: response now includes `fully_complete` (false until first_close_complete substep stamps via task->done) + `absorption_ticket_id` (auto-created ceremonial ticket for the customer's first close-ceremony). agent_md_content optional (<=64KB).",
                                     "body": "{agent_md_hash, agent_md_content?}"},
                "force_finish":    {"method": "POST", "path": "/api/v2/projects/{slug}/onboard-state/force-finish",
                                     "note": "Wave 2.0.1 (VF-361): operator escape hatch for the first_close_complete substep. Stamps the substep with force_finished:true and a rationale (>=30 chars). One-way operation (409 if first_close_complete already stamped, whether via natural close or prior force-finish). Requires operationally-complete onboard (agent_md_hash + completed_at) — substep 6 only opens after substep 5 lands. Reset endpoint provides backward escape; force-finish + reset compose to two-way wizard movement. Telemetry: force_finished flag preserved on onboard_state.first_close_complete for future analytics. Audit-quality required-field family alongside docs_state and abandoned_note (30-char floor surfaces 'why' to future review without prescribing template).",
                                     "body": "{rationale}"},
            },
            "schema_discovery": {
                "openapi":         {"method": "GET",  "path": "/api/v2/openapi.json",
                                     "note": "Wave 2.0: FastAPI's OpenAPI spec mirrored at the API-prefix path so agent self-recovery probes (when stuck on a URL guess) succeed where they look. The default FastAPI path /openapi.json (root) also continues to work. Wave 2.0.1 (Codex pass-1): VISIBLE-VS-ALLOWED DISTINCTION — OpenAPI exposes ALL FastAPI routes (~200+ paths including admin/auth/proxy/bootstrap surfaces NOT intended for agent workflow use). The authoritative agent allowed-workflow surface is /agentnotes/{slug} which enumerates only the endpoints in the agent contract. Use openapi.json for schema-discovery and self-recovery (parameter shapes, response envelopes), not for workflow expansion. Visible-in-schema is NOT permission-to-use; permission is governed by agent role + endpoint discipline declared in /agentnotes."},
                "agentnotes":      {"method": "GET",  "path": "/api/v2/agentnotes/{slug}",
                                     "note": "AUTHORITATIVE allowed-workflow surface for agents. Enumerates the endpoints + rules + onboard sequence the agent is expected to operate inside. Pair with /openapi.json for schema-discovery: /agentnotes is WHAT to use; /openapi.json is HOW (parameter shapes)."},
            },
        },

        "note_fields": {
            "body": "HTML content. Sanitized server-side (allowed: p, br, strong, em, b, i, ul, ol, li, span).",
            "author_type": "human, agent, or system. Agent tokens are forced to 'agent' — cannot impersonate humans.",
            "author_name": "Display name. Agent tokens are forced to their registered name — cannot forge identity.",
            "is_completion_note": "Boolean. Only humans can set true. Agent tokens are forced to false. Required before human can move to done.",
            "superseded_at": "ISO datetime if superseded, null if active.",
            "superseded_by": "Who superseded it.",
            "superseded_reason": "Why it was superseded.",
            "supersede_history": "JSON array of {action, by, reason, at} entries tracking full supersede/revert lifecycle.",
        },

        "agent_enforcement": {
            "summary": "API enforces agent boundaries via Bearer token detection. Silent correction, not rejection where possible.",
            "rules": [
                "Agent CANNOT move tasks to done or cancelled — 422. Only humans close or cancel. To recommend cancellation, move to needs_review with a note explaining why.",
                "Agent CANNOT move to needs_review without setting owner_label to a human — 422.",
                "Agent status transitions: transition_note auto-posted as visible TaskNote in feed. Human transitions log to activity timeline only (no TaskNote — avoids editor boilerplate noise).",
                "Agent note-fidelity gate: needs_review and blocked require transition_note >= 40 chars, no duplicate of previous, blocked must reference blocker. 422 with specific error on failure. Humans are not gated.",
                "Agent posting notes: author_type forced to 'agent', author_name forced to registered name, is_completion_note forced to false.",
                "Agent can only supersede/revert their own notes (author_type='agent') — 422 if attempting on human notes.",
                "Supersede and revert blocked on done tasks — reopen first.",
                "Completion notes required before human closes: must have at least one non-superseded completion note in current cycle.",
                "Reopen from done auto-supersedes all active completion notes — fresh note required for next close.",
                "Reverted notes lose their completion flag — closure authority requires a fresh completion note.",
                "Context Drift Refresh: the board tracks when each agent last read the contract (GET /agentnotes). If more than 1 hour has passed since the last read, the next agent mutation returns 422 with code=BOARD_GATE_TRIGGERED and gate_reason=contract_drift. The agent must re-read CLAUDE.md/AGENTS.md, then GET /agentnotes to refresh, then retry. See CLAUDE.md board-gate rule for recovery steps. Interval is per-project tunable (default: 1 hour).",
                "X-Refresh-Nonce header: every GET /agentnotes returns a refresh_nonce in the response body. On the next mutation, echo it back in the X-Refresh-Nonce HTTP header. Mismatch returns 422 with gate_reason=stale_nonce. Recovery: GET /agentnotes again to obtain a fresh refresh_nonce, save it, retry the mutation with the new nonce in X-Refresh-Nonce. The nonce proves the agent actually parsed the latest contract response, not just kept a stale copy.",
                "Drift-eval self-check (v4): after a contract refresh, the next mutation returns 422 with gate_reason=drift_eval_required and a short session-state question. The question itself names the answer shape (typically 'one short sentence'). Answer truthfully via the X-Drift-Response header. If the gate returns 422 again with the same gate_reason, answer the NEW question — do not re-send the same response text. Drift-eval uses undisclosed evaluation criteria. Answer truthfully; non-compliance triggers human review and a write-freeze until cleared.",
                "Drift-eval freeze (v4): if your writes return 403 with code=BOARD_GATE_FROZEN and gate_reason=drift_eval_stuck, your session has been paused pending human review. Stop writing. Wait for a human to clear the drift flag on the triggering ticket. On clear, a visible note on that ticket will contain re-alignment instructions — follow them before resuming.",
                "Board state — never cache-answer: the board is the live single source of truth shared with the human in real time. Whenever the human asks about board state — 'is there anything for me?', 'what's the status of X?', 'who owns Y?', 'is Z still open?' — you MUST issue a fresh GET against the board (e.g. /me, /projects/{slug}/tasks, /tasks/{id}) BEFORE answering. Do not answer from session memory of prior tool outputs, even if those outputs are seconds old. Cache-answers negate the board's purpose: they turn a live contract into decoration, and they hide your own optimise-past reflex behind plausible-sounding recall. The cost of a fresh GET is ~50ms; the cost of a confidently-wrong cache-answer is broken trust. If you find yourself about to say 'yes/no/N tickets' about board state without a fresh fetch in the last few seconds, stop and fetch first.",
            ],
        },

        "project_crud": {
            "list": "GET /api/v2/projects",
            "create": "POST /api/v2/projects  (body: {name, slug, description, root_path, docs_path})",
            "update": "PATCH /api/v2/projects/{slug}",
            "resume": "PUT /api/v2/projects/{slug}/resume  (body: {resume_summary})",
            "complete": "POST /api/v2/projects/{slug}/complete",
            "archive": "POST /api/v2/projects/{slug}/archive",
            "reopen": "POST /api/v2/projects/{slug}/reopen",
            "archive_summary": "GET /api/v2/projects/{slug}/archive-summary",
        },

        "audit_trail": {
            "tracked_changes": [
                "status_changed", "phase_changed", "title_changed",
                "priority_changed", "owner_changed", "description_changed",
                "blocked_by_changed", "start_date_changed", "due_date_changed",
                "note_deleted", "note_superseded", "note_revert_supersede",
            ],
            "actor_types": ["human", "agent", "system"],
        },

        # ── New: Bootstrap, CLAUDE.md template, Workflows ──

        "bootstrap": _bootstrap_section(agent, request, project),
        "workflows": _workflows_section(agent, request, project),

        # RULE: Agent only sees its scoped project. No information leak.
        "available_projects": [
            {"slug": p.slug, "name": p.name, "status": p.status}
            for p in projects
            if not agent.project_id or p.id == agent.project_id
        ],

        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }

    # Project-specific context + CLAUDE.md template
    if project:
        _stale = _is_resume_stale(project, db)
        contract["context"] = {
            "project_slug": project.slug,
            "project_name": project.name,
            "project_prefix": project.prefix or project.slug.upper()[:3],
            "root_path": project.root_path or "",
            "docs_path": project.docs_path or "",
            "resume": "" if _stale else (project.resume_summary or ""),
            "note": "Use the endpoints above with this slug. Query the API for live state.",
        }
        if _stale:
            contract["context"]["resume_stale"] = True
            contract["context"]["resume_stale_note"] = (
                "The stored resume_summary describes content that no longer exists "
                "on this project (zero tasks, phases, milestones). Treat the project "
                "as fresh; do not anchor on the prior resume."
            )
        contract["agents_md_template"] = _claude_md_template(agent, request, project)

        # Self-referential: keep documentation section for vibeforge-plus only
        if project.slug == "vibeforge-plus":
            contract["documentation"] = {
                "start_here": "0-MD/0-Documentation/TOC.md — master index, directory map, doc index, open-source prep markers",
                "readme": "README.md — project overview, quick start, links to TOC",
                "session_handoff": "0-MD/progress/SESSION-HANDOFF.md — session state, git hashes, ready queue [PRIVATE]",
                "deploy_guide": "0-MD/0-Documentation/internal/deploy.md — SSH, docker stack, deploy workflow [PRIVATE]",
                "board_hierarchy": "0-MD/0-Documentation/public/board-hierarchy.md — milestone=filter, phase=badge [PUBLIC]",
                "product_vision": "0-MD/0-Documentation/public/product-vision.md — product goals, milestone roadmap [PUBLIC]",
                "note": "Read 0-MD/0-Documentation/TOC.md first. All docs tracked in git. Paths relative to repo root.",
            }
    else:
        # WHY: If agent is project-scoped, resolve template from its project even without slug param
        if agent.project_id:
            scoped_project = db.query(Project).filter(Project.id == agent.project_id).first()
            if scoped_project:
                contract["agents_md_template"] = _claude_md_template(agent, request, scoped_project)
                _stale = _is_resume_stale(scoped_project, db)
                contract["context"] = {
                    "project_slug": scoped_project.slug,
                    "project_name": scoped_project.name,
                    "project_prefix": scoped_project.prefix or scoped_project.slug.upper()[:3],
                    "root_path": scoped_project.root_path or "",
                    "docs_path": scoped_project.docs_path or "",
                    "resume": "" if _stale else (scoped_project.resume_summary or ""),
                    "note": "Auto-resolved from agent project scope.",
                }
                if _stale:
                    contract["context"]["resume_stale"] = True
                    contract["context"]["resume_stale_note"] = (
                        "The stored resume_summary describes content that no longer exists "
                        "on this project (zero tasks, phases, milestones). Treat the project "
                        "as fresh; do not anchor on the prior resume."
                    )
            else:
                contract["agents_md_template"] = _claude_md_template(agent, request)
        else:
            contract["agents_md_template"] = _claude_md_template(agent, request)

    # ── Documentation contract: base template + project-specific if it exists ──
    doc_contract_body = None
    if project:
        from app.models.artefact import ProjectArtefact
        artefact = (db.query(ProjectArtefact)
            .filter(ProjectArtefact.project_id == project.id, ProjectArtefact.name == "contract")
            .order_by(ProjectArtefact.version.desc())
            .first())
        if artefact:
            doc_contract_body = artefact.body

    contract["doc_contract"] = {
        "status": "project_specific" if doc_contract_body else "base_template",
        "version": "1.0",
        "note": "Read-only in v1. Agent write access (PUT /artefacts/contract) is planned for a future release." if not doc_contract_body else "Project-specific documentation contract. Managed via PUT /artefacts/contract.",

        "minimum": {
            "readme": "Every project must have a README.md.",
            "style": "TL;DR at the top of every doc (manager stops here). Full detail below (engineer keeps reading). One doc, two audiences.",
            "toc": "If more than 3 docs exist, create and maintain a TOC.md. Rebuild after every doc change.",
        },

        "on_planning": "During onboarding, ask the human: 'Is this project closer to a copy-paste app or a CRM?' Then calibrate both structure and detail density accordingly.",

        "spectrum": {
            "copy_paste_app": {
                "description": "Simple, short-lived, weekend project",
                "structure": "README.md + inline code comments. TOC only if 3+ docs accumulate.",
                "density": "README covers what it does, how to run it, and any non-obvious decisions. A few paragraphs, not pages. Docs are a convenience, not a contract.",
            },
            "crm": {
                "description": "Complex, long-lived, multi-surface, multi-session",
                "structure": "README + architecture docs per surface (auth, API, database, etc) + proposals folder + progress/handover folder + TOC (mandatory) + render pipeline if available.",
                "density": "Each surface doc covers: what it does, why it is shaped this way, what the agent needs to know to work on it safely, what the gotchas are. TL;DR at top, full detail below. Docs are the persistence layer — they survive session boundaries.",
            },
            "in_between": "Human describes, agent calibrates. Most projects land here. README + 1-3 architecture docs for the main surfaces + TOC. No audience folders or render pipeline unless the human asks.",
        },

        "on_refresh": "If a TOC already exists in the project's doc path, it represents the agreed documentation structure. Follow it. Do not re-run the planning prompt or overwrite the existing structure.",

        "project_body": doc_contract_body,
    }

    # GATE: Slim response on refresh reads (not first read).
    # WHY: First read gets everything — agent needs full contract to bootstrap.
    # Subsequent reads drop heavy sections the agent already has on disk or
    # in context. Cuts response size on long-session refreshes.
    #
    # FIX (Round 6 feedback): bootstrap/agents_md_template/board_capabilities
    # were previously stripped on slim, but a normal bootstrap involves TWO
    # authenticated /agentnotes calls (no-slug first, then scoped) — the
    # second call is flagged slim=True because last_contract_read_at was set
    # by the first call. Stripping bootstrap fields on that second call meant
    # the agent's FIRST scoped read lost exactly the fields bootstrap tells
    # it to look at. Now keeping these in slim responses too — they're small
    # (~5KB combined) and bootstrap-critical. Only design_principles +
    # workflows (the genuinely heavy sections) get stripped on refresh.
    if slim:
        for key in ("design_principles", "workflows"):
            contract.pop(key, None)
        contract["response_mode"] = "slim"
    else:
        contract["response_mode"] = "full"

    return contract


# ── Stale-resume detection ──

def _is_resume_stale(project, db) -> bool:
    """True if `resume_summary` describes content that no longer exists.

    Heuristic: resume has substantive text AND the project has zero tasks +
    zero phases + zero milestones. Caught in Round 6 — when an admin wipes
    a project's content via SQL (smoke-test reset) without also clearing
    `resume_summary`, the next agent contact sees a resume describing past
    state that's gone, contradicting `/projects/{slug}/tasks` returning [].

    Round 6 fix: detect this and surface `context.resume_stale=True` while
    blanking the resume, so the agent doesn't anchor on a fiction.
    """
    if not project.resume_summary or not project.resume_summary.strip():
        return False
    from app.models.task import Task
    from app.models.phase import Phase
    from app.models.milestone import Milestone
    if db.query(Task.id).filter(Task.project_id == project.id).first():
        return False
    if db.query(Phase.id).filter(Phase.project_id == project.id).first():
        return False
    if db.query(Milestone.id).filter(Milestone.project_id == project.id).first():
        return False
    return True


# ── Routes ──

def _refresh_agent_drift(agent, db):
    """Reset drift timer + rotate nonce on contract read.
    Returns (nonce, is_first_read) — is_first_read is True if the agent
    has never read the contract before (last_contract_read_at was NULL)."""
    from datetime import datetime, timezone as _tz, timedelta
    import secrets
    from app.api.v2.drift_gate import DRIFT_EVAL_PASS_GRACE_SECONDS
    is_first_read = agent.last_contract_read_at is None
    agent.last_contract_read_at = datetime.now(_tz.utc)
    agent.refresh_nonce = secrets.token_hex(4)  # 4 bytes = 8 hex chars
    # Wave 2.0.8 R4 (B): grace window. Reset per-cycle eval pivot count
    # always (a refresh is the natural reset point for in-flight pivots),
    # but only nuke drift_eval_passed_at if it's stale beyond the grace
    # window. Codex blind cross-vendor batch operations surfaced that the
    # original "always reset on refresh" behaviour cycled drift_eval prompts
    # on every mutation when the agent re-fetched /agentnotes defensively
    # between mutations. Now: pass once, batch mutations within 30 min stay
    # quiet even across refreshes; stale passes still re-arm. Hash history
    # (drift_eval_hashes, lifetime) is intentionally NOT reset — it persists
    # across cycles so cached responses from earlier cycles are still caught
    # during the grace window. Escalation freeze (drift_escalated_at) is also
    # not reset here — only the human clear handler can lift it.
    agent.drift_eval_count = 0
    if agent.drift_eval_passed_at is not None:
        age = (datetime.now(_tz.utc) - agent.drift_eval_passed_at).total_seconds()
        if age > DRIFT_EVAL_PASS_GRACE_SECONDS:
            agent.drift_eval_passed_at = None
        # else: keep the recent pass intact across this refresh (grace window)
    # If passed_at was already None, leave it None (first read or post-escalation clear).
    db.commit()
    return agent.refresh_nonce, is_first_read


@router.get("/agentnotes")
def agent_contract(request: Request, db: Session = Depends(get_db)):
    agent = _get_agent_from_token(request, db)
    if agent:
        nonce, is_first_read = _refresh_agent_drift(agent, db)
        result = _full_contract(agent, db, request, slim=not is_first_read)
        result["refresh_nonce"] = nonce
        return result
    return _public_contract(request)


@router.get("/agentnotes/{slug}")
@router.get("/api/v2/agentnotes/{slug}")
def agent_contract_project(slug: str, request: Request, db: Session = Depends(get_db)):
    """Project-scoped agentnotes. Both /agentnotes/{slug} (root) and
    /api/v2/agentnotes/{slug} (alias) work — IC-003 fix per VF-353.

    Why both: agents that compose URLs from VIBEFORGE_API (which carries the
    /api/v2 base) get a working route; agents reading the contract literally
    use the root form. Eliminates the "6-attempt hunt" failure mode observed
    in run #1.
    """
    agent = _get_agent_from_token(request, db)
    if agent:
        nonce, is_first_read = _refresh_agent_drift(agent, db)
        result = _full_contract(agent, db, request, project_slug=slug, slim=not is_first_read)
        result["refresh_nonce"] = nonce
        return result
    return _public_contract(request)
