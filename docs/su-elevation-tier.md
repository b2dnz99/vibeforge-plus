---
title: VibeForge+ SU Elevation Tier — Phase 2 of the SA/SU identity split
audience: public
status: 0.7.1-PRE-RC
version: 1.1.0
supersedes: 0-MD/proposed/SU-ELEVATION-TIER-PROPOSAL.md (graduated on PROD ship)
last_updated: 2026-05-09
authors: Parvez Khan + Claude (AI co-author)
ip: informative
style: technical
ip_first_dated: 2026-04-20
ip_authors: [pkhan, claude]
ip_disclosure_path: published
ip_summary: |
 Closes bug 5.6 from identity-roles.md. SUs today borrow the SA password to perform
 admin-panel actions; audits attribute to SA regardless of which human acted.
 Phase 2 introduces an SU admin context: SU re-confirms their OWN password via a
 new /admin/elevate endpoint, receives a short-lived `elevated_until` stamp on
 their existing `vf_session`, and performs admin writes as themselves. SA stays
 as pure break-glass for recovery paths (SU lockout, host-side rescue). The
 `_require_sa` gate is rebuilt to accept either (a) the legitimate `vf_sa_session`
 cookie OR (b) an elevated `vf_session`. Return value is the acting user, so
 existing `_audit` callsites automatically attribute correctly. Single-commit
 ship with one schema migration.
---

# SU Elevation Tier

> **Status:** Active as of 2026-04-20. Phase 2 of the identity-roles.md plan — the SU admin-context elevation tier. Shipped across all environments in a single commit (an internal release). Sibling to [identity-roles.md](identity-roles.md) which now flips bug 5.6 from "deferred" to "fixed via this doc."

---

## 1. Thesis

Today, when a Super User needs to perform an admin-panel action:

1. They click "Admin" from the board.
2. They land on `/admin/login`, which asks for the **SA** password.
3. If they know it (likely — they ARE the SA in dogfood, or they share the password in team mode), they type it.
4. Server issues a `vf_sa_session` cookie bound to **the SA user account**.
5. `_require_sa` returns the **SA user** object. All admin writes are audited as the SA's `display_name`.

The operator who actually clicked the button is invisible to the audit trail. In single-the maintainer dogfood this is merely confusing; in any team deployment it is an **auditability hole** — the SA becomes a shared shell for privileged actions and accountability dissolves.

**Phase 2 fix:**

1. SU stays logged in as themselves (`vf_session`).
2. Before performing admin actions, they **re-confirm their own password** at a new `/admin/elevate` endpoint.
3. Server stamps `sessions.elevated_until = now + 30 min` on their existing session row.
4. `_require_sa` gate is rebuilt to accept either:
 - a valid `vf_sa_session` cookie (break-glass path, unchanged), **or**
 - a `vf_session` belonging to an active SU with `elevated_until > now`.
5. Return value is the *acting* user — SA in the first path, SU in the second.
6. `_audit` callsites use that return value; attributions become correct automatically. An additional `elevated: true` is written into details when the caller is an elevated SU, for explicit auditability.

**SA stays as pure break-glass:** the `vf_sa_session` path is unchanged for this ticket. It's the rescue tier for SU lockout, lost credentials, DB-patching scenarios. A future hardening ticket can restrict SA to recovery-only paths once operators are comfortable relying on SU elevation for routine admin.

> **Update — an internal release (2026-04-25):** the single `_require_sa` gate described above has been **split into three tiered helpers** — `_require_portal_read`, `_require_portal_user_write`, and `_require_portal_system_write`. Tier-S system writes (certs, SSO, SMTP, backup, branding) now reject elevated SU; only an actual SA cookie passes. Tier-U user writes (users, agents, sessions, memberships) keep the prior semantics. The elevation mechanism this doc describes is unchanged — what changed is what an elevation grants you. Full spec at [admin-portal-perm-tiers.md](../internal/admin-portal-perm-tiers.md) (internal). The original `_require_sa` symbol is retained as an alias for `_require_portal_user_write` for back-compat.

---

## 2. Design decisions (D-series — pick before code)

Each decision has options; my lean is last. Confirm D1–D9 or push back.

### D1 — Elevation credential **→ A (locked)**

How does the SU prove intent?

- **A.** **SU re-enters their own password (sudo pattern). ← SELECTED**
- **B.** SU re-enters email + password (fresh credential proof).
- **C.** TOTP (requires new infra).
- **D.** Separate "admin password" per SU (second credential).

### D2 — Elevation state storage **→ A (locked)**

Where does the "elevated" flag live?

- **A.** **New column `elevated_until: DateTime \| None` on the `sessions` table. ← SELECTED**
- **B.** Two columns: `elevated: bool` + `elevated_at: DateTime`.
- **C.** New separate `elevated_sessions` table.
- **D.** In-memory cache (Redis-style).

### D3 — Elevation timeout **→ 15 min (locked)**

How long is elevation valid?

- **A.** 5 min (tight; high friction).
- **B.** **15 min. ← SELECTED**
- **C.** 30 min (matches existing `SA_SESSION_MINUTES`).
- **D.** Until logout (no timeout).

Rationale: tighter than the SA break-glass session (30 min) since SU routine admin shouldn't linger. Rolling extension (D4) mitigates friction during active work.

### D4 — Re-elevation trigger **→ C, rolling window (locked)**

After initial elevation, when does SU need to re-confirm?

- **A.** Once per elevation window → all admin actions pass until timeout.
- **B.** Fresh re-elevation for each destructive action (soft-delete, role change, transfer).
- **C.** **Rolling timeout — auto-extend on each admin action. ← SELECTED**

Rationale: no friction during active admin work; still kicks on 15-min idle. Implementation: each successful `_require_sa` hit via the SU path sets `elevated_until = now + 15 min`.

### D5 — SA cookie path going forward **→ A (locked)**

- **A.** **SA keeps `vf_sa_session` cookie unchanged. SU elevation uses `vf_session` with `elevated_until`. ← SELECTED**
- **B.** Retire SA cookie entirely. (Contradicts identity model.)
- **C.** SA cookie stays but restricted to nuclear ops only (future hardening).

### D6 — SA usage going forward **→ A (locked)**

- **A.** **SA remains usable for all admin ops as today (fallback for any case). ← SELECTED**
- **B.** SA restricted to recovery ops only.
- **C.** SA deprecated; only host-script-induced emergency.

Phase 2 is purely additive — zero regression risk on SA break-glass.

### D7 — UI flow **→ B (locked)**

- **A.** Separate `/admin/elevate` page for SUs + existing `/admin/login` page for SA.
- **B.** **Single `/admin/login` page, dual-mode. Server detects SU session and renders the correct form. ← SELECTED**
- **C.** Modal overlay on admin pages when elevation needed.

### D8 — Audit attribution **→ C (locked)**

- **A.** `_audit` uses whoever `_require_sa` returned.
- **B.** Add explicit `elevated: true` to details dict.
- **C.** **Both. ← SELECTED**

### D9 — Phase split / ship shape **→ A (locked)**

- **A.** **Single commit, single ship. ← SELECTED**
- **B.** Split infra from callsite wiring.
- **C.** Split infra from SA restriction.

Helper refactor is atomic — no callsite edits needed.

---

## 3. Code surface (assuming defaults above)

Five concrete changes. One schema migration.

### 3.1 Schema migration

```sql
ALTER TABLE sessions ADD COLUMN elevated_until TIMESTAMP WITH TIME ZONE;
```

Column is nullable. Default NULL means "not elevated." No backfill needed.

### 3.2 New endpoint: `POST /admin/elevate`

```
Body: { password: str }
Gate: must have a valid vf_session belonging to an active super_user.
Action: if bcrypt.check(password, su.password_hash): set sessions.elevated_until = now() + 30 min.
Audit: su_elevation_granted (success) or su_elevation_failed (bad password).
Response: { ok: true, elevated_until: ISO8601 }
```

### 3.3 Rebuilt helper: `_require_sa`

```python
def _require_sa(request, db) -> User | None:
    # Path A: traditional SA cookie
    user = _get_sa_user(request, db)
    if user:
        return user

    # Path B (new): elevated SU session
    session_id = request.cookies.get("vf_session")
    if not session_id:
        return None
    sess = db.query(UserSession).filter(
        UserSession.id == session_id,
        UserSession.session_type == "user",
        UserSession.expires_at > now(),
        UserSession.elevated_until != None,
        UserSession.elevated_until > now(),
    ).first()
    if not sess:
        return None
    user = db.query(User).filter(
        User.id == sess.user_id,
        User.role == "super_user",
        User.status == "active",
    ).first()
    return user
```

Return contract is unchanged (returns acting user or None). All 29+ callsites continue to work unmodified.

### 3.4 `/admin/login` dual-mode

Template + view detect whether the incoming request has a valid SU `vf_session`:

- **Has SU session:** render "Confirm your password to enter admin mode." Form POSTs to `/admin/elevate`.
- **No SU session (or non-SU user):** render existing SA login form. Form POSTs to `/admin/login` (unchanged).

Copy reflects the two states. For SU path the tone is "you are elevating yourself, briefly"; for SA path the tone is "this is break-glass."

### 3.5 `_audit` callsite extension

Audit helper picks up an optional `elevated: bool` flag. When `_require_sa` returned the SU path, callers pass `elevated=True`. Details dict ends up with `{"actor": "<name>", "elevated": true}`.

Or — simpler — the helper computes the elevated flag itself from the returned user's role: if role is `super_user`, they're elevated by definition (since regular SU access doesn't reach `_require_sa` paths). Record as `elevated: True` in that case.

---

## 4. Acceptance criteria

- [ ] `POST /admin/elevate` with correct SU password + active SU session → 200, `elevated_until` set, audit `su_elevation_granted` written.
- [ ] `POST /admin/elevate` with wrong password → 401, audit `su_elevation_failed`, `elevated_until` unchanged.
- [ ] `POST /admin/elevate` while logged in as non-SU user → 403.
- [ ] `POST /admin/elevate` while not logged in → 403.
- [ ] After elevation, SU can call any `_require_sa`-gated endpoint → 200, audit attributes to SU `display_name` with `elevated: true`.
- [ ] After elevation timeout, same endpoint → 401 "SA elevation required."
- [ ] Existing SA flow (`vf_sa_session` cookie) still works unchanged.
- [ ] `/admin/login` GET detects session state and renders the correct form.
- [ ] Self-test on DEV covers all six SU-elevation paths + regression on SA path.
- [ ] Migration runs cleanly; rollback migration provided.

---

## 5. Out of scope (future tickets)

- **TOTP as elevation credential.** D1 picks password re-confirm for v1; TOTP is future hardening.
- **SA restriction to recovery-only paths.** D6 picks "SA keeps all powers." Future hardening.
- **Rolling elevation timeout.** D4 picks fixed timeout. Extending on activity is nice-to-have.
- **Elevated session revocation UI.** SU may want to "un-elevate" explicitly; today they'd wait for timeout or logout. Future nice-to-have.
- **Admin panel "remaining elevation" indicator.** Nice-to-have chrome.

---

## 6. Ship plan

Single commit, single ship. Per D9.A:

1. Alembic migration: add `sessions.elevated_until`.
2. New endpoint: `POST /admin/elevate`.
3. Rebuild `_require_sa`.
4. Update `/admin/login` template + view for dual-mode.
5. Audit helper records `elevated: true` when acting via SU path.
6. Self-test on DEV covers all six SU-elevation paths + SA regression.
7. Deploy DEV → UAT → PROD.
8. Graduate this doc (move to public tree; update identity-roles.md cross-refs).

---

## 7. Graduation plan

On PROD ship:

- Move `0-MD/proposed/SU-ELEVATION-TIER-PROPOSAL.md` → `0-MD/0-Documentation/public/su-elevation-tier.md` (or fold into `identity-roles.md` as §11 — the maintainer to decide at graduation; default: separate doc).
- Update `identity-roles.md` §5.6 to flip from "bug, deferred" → "fixed via SU elevation tier; see su-elevation-tier.md."
- Update `identity-roles.md` §8 to remove an internal release from the non-scope list.

---

## 8. Sign-off checklist

- [x] the maintainer confirms D1 (elevation credential: password re-confirm). *2026-04-20*
- [x] the maintainer confirms D2 (storage: `elevated_until` column on sessions). *2026-04-20*
- [x] the maintainer confirms D3 (timeout: **15 min**). *2026-04-20 — tightened from initial lean.*
- [x] the maintainer confirms D4 (**rolling window: auto-extend on each admin action**). *2026-04-20 — changed from initial lean.*
- [x] the maintainer confirms D5 (SA cookie unchanged; login page dual-mode). *2026-04-20*
- [x] the maintainer confirms D6 (SA keeps all powers for now). *2026-04-20*
- [x] the maintainer confirms D7 (UI: single login page, dual-mode). *2026-04-20*
- [x] the maintainer confirms D8 (audit: both automatic attribution + explicit `elevated: true`). *2026-04-20*
- [x] the maintainer confirms D9 (ship: single commit). *2026-04-20*

**All ticked 2026-04-20. Proposal is load-bearing for an internal release Phase 2 implementation.** Any code deviation updates this doc in the same commit.
