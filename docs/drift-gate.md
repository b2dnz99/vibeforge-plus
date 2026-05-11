---
title: "The drift gate — what it does, why it exists, how to control it"
audience: public
status: 0.7.1-PRE-RC
version: 1.1.0
last_updated: 2026-05-09
authors: Parvez Khan + Claude (AI co-author)
---

# The drift gate

A mechanism in the API layer that periodically forces an AI agent to re-read its contract before letting it write to the board. This page describes what it does, the lever to turn it down or off, and what an operator typically sees.

## What it addresses

After a long agent session, two things drift apart:

1. The agent's working memory of what the rules are (the agent contract — endpoints, response shapes, gates, recovery patterns)
2. The actual rules on the server

The agent's recall of the rules decays over hours of use, particularly when the conversation gets compressed (Claude `/compact`, model context-window rotation, new session inheriting paraphrased state). A long session without a re-read often produces "the agent confidently does the wrong thing because its rules went stale." The drift gate is the server-side enforcement that the agent re-reads on a timer it cannot skip.

## How it works

```
                 every API mutation by an agent (POST/PATCH/etc.)
                                    │
                                    ▼
              ┌────────────────────────────────────────┐
              │  Has this agent refreshed its contract │
              │  in the last <window> seconds?         │
              │  (default: 1 hour, project-tunable)    │
              └────────────────┬───────────────────────┘
                               │
                  ┌────────────┴────────────┐
                  │                         │
                  ▼                         ▼
              YES — pass               NO — return 422
              (write proceeds)         BOARD_GATE_TRIGGERED
                                       refresh required
                                              │
                                              ▼
                                      Agent reads the contract:
                                      GET /agentnotes/<slug>
                                              │
                                              ▼
                                      Server now asks a short
                                      session-state question via
                                      the next 422 (X-Drift-Response
                                      header is the answer channel)
                                              │
                                              ▼
                                      Agent answers truthfully
                                      (length floor + dedup applied
                                      across all past answers)
                                              │
                                              ▼
                                      Pass: write proceeds
                                      Fail: agent gets a different
                                      question; up to 4 rounds; then
                                      escalates to "stuck" — human
                                      operator must intervene
```

The "session-state question" is a short prompt like *"What's the next concrete step you'll take?"* or *"What surprised you in the last few minutes?"*. The agent answers in one short sentence. The server checks the answer is long enough and not a verbatim repeat of any past answer (lifetime hash dedup). The grading is content-blind — the act of producing fresh output is what re-engages the agent's working memory; the server doesn't try to score for correctness.

## What an operator sees

Most of the time: nothing. Within the freshness window (default 1 hour from the agent's last contract-read), the gate is invisible.

When it fires (typical: a few times per long session):

- The agent's tool-call output shows a 422 response with code `BOARD_GATE_TRIGGERED`
- The agent does the recovery automatically (re-read + answer the question)
- An extra agent turn or two; no operator action needed

When it escalates (rare — typically only if the agent is genuinely stuck or there's a real misalignment):

- After 4 question-answer rounds without passing, the gate flips the agent's state to `drift_eval_stuck`
- All further writes from that agent return 403 `BOARD_GATE_FROZEN` until cleared
- The admin portal surfaces a "⚠ Clear Drift Gate" button on the affected task
- An operator with admin access clicks Clear, optionally types a short re-alignment note, and the agent unfreezes

## How to control it

Three levers:

### Lever 1 — System-wide enable / observation mode

In the admin portal at `/admin/portal/administration/agent-telemetry-and-drift`, there is a toggle:

- **Enabled** (default): full enforcement. Gate fires + can escalate to freeze.
- **Disabled**: observation mode. Eval prompts still get sent, attempts still log, escalation rows still create — the freeze step is suppressed. Use this to see what would have happened without disrupting work.

The toggle is system-wide, persisted in the database, survives restarts.

### Lever 2 — Per-project freshness window

The default 1-hour window is project-tunable. Set per project via the admin portal session-policy page. Useful when:

- Agents are very chatty → shorten the window to 30 minutes (more frequent re-grounding)
- Agents do long compute-bound runs → lengthen to 2 hours (fewer mid-task interruptions)

The minimum supported window is 60 seconds (testing only).

### Lever 3 — Manual clear when escalated

If an agent is in `drift_eval_stuck`, an admin operator clears the flag from the task drawer (or via `POST /api/v2/tasks/<id>/clear-drift` with an SA cookie). The clear unfreezes the agent and writes an audit row noting who cleared and when.

## Should it be on or off

Default is **on**. Reasons to keep it on: long agent sessions accumulate enough rule-decay that re-reads catch real failures.

Consider observation mode if:

- Testing a new agent integration — see how often the gate would fire without disrupting flow
- Doing a one-off short task and the gate fires more than expected
- Debugging an agent in escalation — see the next 422 question without it actually freezing

Consider full disable if:

- Agents in this project don't need persistent memory across sessions (one-shot scripts)
- VibeForge+ is being used as a pure task-board with humans only

## What it is not

- Not a security gate. It addresses agent cognition, not malice. A malicious agent could still write garbage tasks; the drift gate doesn't stop that.
- Not a rate limiter. Bursts are fine within the freshness window. The gate is about freshness, not pace.
- Not a quality grader. It doesn't judge whether the agent's work is good — only whether the agent has recently re-grounded its rules.
- Not unbreakable. A sufficiently capable agent could in principle game it (cache an answer, vary minimally to defeat dedup). The audit trail of eval attempts is the secondary value — humans can spot replay or pattern-match in the history that server-side logic can't catch.

## What the agent does on its end

The agent contract describes the recovery protocol verbatim. The agent's only job:

1. See the 422 with `code: BOARD_GATE_TRIGGERED`
2. Read the `gate_reason` field
3. If `contract_drift` or `stale_nonce`: GET `/agentnotes/<slug>`, then retry the original action with the `X-Refresh-Nonce` header set to the value from the response
4. If `drift_eval_required`: read the `detail` field (the question), answer truthfully in the `X-Drift-Response` header, retry
5. If anything else: read the `agent_remedy` field — it names the specific recovery

Agents don't need to know the algorithm; they follow the recovery instructions in each 422 response. Every gate firing is fully recoverable from the response payload alone.

## Where it surfaces in the UI

| Where | What you see |
|---|---|
| Admin portal `/admin/portal/administration/agent-telemetry-and-drift` | Per-agent: API call count, last-contract-read timestamp, eval pass/fail history. System-wide: the enable toggle. |
| Task drawer (when escalated) | "⚠ Clear Drift Gate" button + a re-alignment note input. Visible only on tasks where an agent is currently stuck. |
| Task card list | Tasks with a stuck agent get a small DRIFT chip and float to the top of the agent's task list. |
| Audit log | Every gate firing writes an audit event (`drift_eval_fired`, `drift_eval_passed`, `drift_eval_escalated`, `drift_cleared`). |

## TL;DR

Periodically (default once an hour) the agent must re-read its contract and answer a short session-state question before its next write lands. Most fires cost ~5-10 seconds of agent time, no operator action. Tunable per project. Can be set to observation mode or fully disabled via the admin portal.
