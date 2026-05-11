---
title: "Board hierarchy — phase + milestone display rules"
audience: public
status: 0.7.1-PRE-RC
version: 1.1.0
last_updated: 2026-05-09
authors: Parvez Khan + Claude (AI co-author)
ip: informative
style: technical
---

# CRITICAL — Board Hierarchy Display Rules

**Status**: Must fix before v1.5 becomes source of truth
**v1 task**: #76
**Created**: 2026-03-29

---

## The Hierarchy

```
Project > Milestone > Phase > Task
```

Each level has a specific display role. They must NOT be conflated.

---

## Display Rules by Context

### Board View

| Element | Role | Display |
|---|---|---|
| **Milestone** | Filter | Chip bar at top of board. Click to filter all columns. NOT on card face. |
| **Phase** | Section divider | Swimlane header within each column. Groups tasks visually. |
| **Task** | Card | Draggable card in column. |

### Card Face Shows

- Title
- Priority pill
- Owner badge
- Phase badge
- Status pill

### Card Face Does NOT Show

- Milestone label — **redundant** when phase already implies it
- Raw label tags — legacy v1 pattern, not a display element

### Gantt Chart

| Element | Display |
|---|---|
| Milestone | Timeline bar spanning all its phases |
| Phase | Grouped rows under milestone |
| Task | Bar within phase row |

### Filters

- Milestone chips: top of board, filter all columns + show heading
- Phase: secondary filter within milestone context
- "x All" chip clears milestone filter

---

## Why This Matters

The v1 board used flat label tags (`milestone-b`, `ui`, `board`) as pseudo-categories on cards. v2 introduced proper milestones and phases in the DB but continued showing milestone badges on cards alongside phase badges. This creates visual noise and breaks the hierarchy:

- If a task is in the "Identity" phase, it belongs to "Auth & Access" milestone — showing both is redundant
- Milestone is a **filter/grouping** concept, not a card-level attribute
- Phase is the **work context** that matters on the card face

---

## Action Required

1. Remove milestone badge from board cards (v2 board.html)
2. Remove milestone badge from v1.5 board cards
3. Ensure phase swimlanes render as section dividers in columns
4. Milestone chips filter the board (already implemented as client-side filtering in v2)
5. Apply same rules consistently to v1.5 when board is upgraded
6. Update BOARD-STRUCTURE.json if milestone/phase rendering rules change

---

## Pre-agreed Milestone Names (v1 → v1.5)

| v1 Label | v1.5 Name | Status |
|---|---|---|
| milestone-a | Foundation | done |
| milestone-b | Auth & Access | done |
| milestone-b5 | Board & UI | done |
| milestone-c | Agent Platform | active |
| milestone-c+ | AI Features | active |
| milestone-d | Hardening | active |
| milestone-e | VS Code | active |
| milestone-f | Multi-Agent | active |

## Pre-agreed Phase Names

| Milestone | Phases |
|---|---|
| Foundation | Infrastructure |
| Auth & Access | Identity, Tokens, Permissions, Verification |
| Board & UI | Layout, Interactions, Editor, Theme, Verification |
| Agent Platform | Notes, Attribution, Sessions, Verification |
| AI Features | Provider Keys, AI-Powered |
| Hardening | Observability, Backups, Security |
| VS Code | Core, Presence |
| Multi-Agent | Decisions, Integrations |
| *(always present)* | Triage |

**These names are final.** Do not rename without human approval.
