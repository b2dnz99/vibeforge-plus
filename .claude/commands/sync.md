Execute the full end-of-session sync workflow. Every step is mandatory — do not skip any.

## Prerequisites

- Read token from `0-Code/.agent-config` (never display it)
- API base: use $VIBEFORGE_API from .agent-config
- Always fetch fresh board state before making any changes

## Step 1: Task Reconciliation

Fetch ALL open tasks from the board API:
```
GET /api/v2/projects/vibeforge-plus/tasks
```

For each task you worked on this session:
- **If you did work but didn't add notes** → add a note summarising what was done
- **If status should have changed but didn't** → update status with proper `transition_note`
- **If task is in `needs_review`** → ensure it's assigned to `human:Parvez Khan`, not agent
- **If task was completed by human** → respect their status, only add missing notes
- **Show what you updated** in your output

Rules:
- Always include `transition_note` on status changes
- Include `@Parvez Khan` mention on needs_review tasks
- Never move a task to `done` — only human does that
- Check activity log if unsure who changed what last

## Step 2: Local Git Commit

```bash
cd /c/0-APP/ViveForge+/0-Code
git add -A
git status --short
```

- If there are changes, commit with a descriptive message summarising session work
- Include `Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>`
- If no changes, skip and note "Working tree clean"

## Step 3: GitHub Push

```bash
git push origin master
```

- Report the commit hash
- If push fails, report the error — do not force push

## Step 4: Session Handoff Update

Update `0-MD/SESSION-HANDOFF.md`:
- Increment version number
- Update `last_updated` date and git hash
- Add a new session section with:
  - What was built/fixed this session (bullet points)
  - Tickets completed/moved
  - Any bugs found
- Update "Ready for Next Session" with current ready queue
- Update "Needs Review" with current review queue
- Update "Agent Resume" with current state summary
- Commit and push this update too

## Step 5: Check Uncommitted Planning/Design Work

Scan the conversation for:
- Architecture decisions discussed but not written to MD
- Design mockups approved but not saved
- Contract changes discussed but not codified
- Task descriptions or notes composed but not posted to API

For each found:
- If significant → write to appropriate MD file or post to API
- If draft/incomplete → write to `0-MD/DRAFTS/` with clear filename
- Report what was saved

## Step 6: Update Project Resume

```
PUT /api/v2/projects/vibeforge-plus/resume
```

Write a concise resume summarising the current project state — what's built, what's in review, what's next.

## Step 7: Session Summary

Output a clean summary:

```
═══ SESSION SYNC COMPLETE ═══

Git: {hash} → pushed
Handoff: v{version}

Tasks Updated:
  • VF-XX: {what changed}
  • VF-YY: {what changed}

Needs Your Review:
  • VF-XX: {title}

Ready for Next Session:
  • VF-XX: {title}

MDs Written/Updated:
  • {filename}: {what}

Session Highlights:
  • {bullet point summary of session work}
```

## Important

- This is the LAST thing done before session ends
- Do NOT skip steps — the handoff is how the next session picks up context
- If context window is critically low, prioritise: Step 1 (tasks) → Step 2+3 (git) → Step 6 (resume) → Step 4 (handoff)
