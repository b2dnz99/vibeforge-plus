---
title: VibeForge+ Identity & Role Model — SA break-glass, SU day-to-day, User, Viewer
audience: public
status: 0.7.1-PRE-RC
version: 1.1.0
supersedes: 0-MD/proposed/IDENTITY-ROLES-PROPOSAL.md (graduated on PROD ship of an internal release + an internal release + an internal release)
last_updated: 2026-05-09
authors: Parvez Khan + Claude (AI co-author)
ip: informative
style: technical
ip_first_dated: 2026-04-20
ip_authors: [pkhan, claude]
ip_disclosure_path: published
ip_summary: |
 Codifies the four-role identity model that the board has always described in its UI matrix
 but never enforced in code: Super Admin (SA, break-glass only, never a board participant),
 Super User (SU, day-to-day admin with full board + admin panel), User (standard board
 member), Viewer (read-only). Fixes the current bug where SA can log into the board and is
 treated as SU across all project-level access paths. Establishes a bootstrap invariant
 (install_open stays true until BOTH SA and SU exist) and splits the auth cookie surface so
 SA only ever holds the short-lived `vf_sa_session` cookie on path=/admin/, never a `vf_session`
 board cookie. Forward-pointer to an internal release Phase 2 (SU admin context elevation tier) and
 an internal release (first-boot wizard UI, which will subsume the "install incomplete" banner). Scoped
 as Phase 1A (an internal release) + Phase 1B (an internal release); Phase 2 deferred.
---

# Identity & Role Model

> **Status:** Active as of 2026-04-20. Signed off by the maintainer on an internal release (doc-approval anchor) after Phase 1 landed in PROD. Covers human identity and authorization tiers. Companion doc [user-agent-model.md](user-agent-model.md) covers agent lifecycle. Phase 2 (an internal release SU elevation tier) retains separate scope.

---

## 1. Thesis

VibeForge+ has always advertised a four-role identity model — the Role Reference table at the top of `/admin/` shows it plainly:

| Role | Scope | Projects | Admin |
|---|---|---|---|
| **Super Admin (SA)** | **Admin portal only** | Audit only — **not a participant** | Full system access |
| **Super User (SU)** | Board + Admin | All projects | Manages users + projects |
| **User** | Board only | Own + assigned | None |
| **Viewer** | Board (read-only) | Assigned only | None |

The code, however, treats SA as a super-powered board user. A `super_admin` with valid credentials can log into `/ui/login`, gets a normal board session, and is indistinguishable from an SU across every project-level access check. This doc defines the intended model, fixes the three discovered bugs, and establishes the invariants that keep the model correct going forward.

The model is deliberately **simple**. No elevation key, no sudo-mode, no TOTP, no elevation audit flags — those are Phase 2 (see §8). Phase 1 just makes the matrix match reality.

---

## 2. The four roles

### SA — Super Admin (break-glass)

- **Purpose:** system recovery, SU lockout rescue, emergency password resets, nuclear administrative actions (user soft-delete, role changes, activity log inspection).
- **Cookie:** `vf_sa_session`, path=`/admin/`, 30-minute expiry. Short-lived by design.
- **Board access:** *none*. SA cannot log into `/ui/` at all. Attempts are rejected with a redirect hint to `/admin/`.
- **Project access:** *none implicit*. SA does not appear in any project's member list. SA cannot read, write, or own projects via the normal API surface.
- **Creation:** exactly one SA at bootstrap via `POST /api/v2/bootstrap/create-sa`. Cannot be created via the admin UI. Cannot be deleted. Cannot have its role changed.
- **Recovery:** host-side `scripts/reset_sa_password.py` for lost-credentials cases. Documented in [recovery-procedures](../0-Documentation/internal/recovery-procedures.md).
- **Audit:** all SA actions recorded with actor_type=`user` and the SA's `display_name`.
- **Password-change audit invariant (new — see bug 5.7):** every password change on the SA account MUST write an explicit, distinguishable ActivityEvent tagged against the SA. Two event types:
 - `sa_password_self_change` — SA changed own password via the normal UI.
 - `sa_password_force_reset` — password changed via the host-side rescue script OR via any future admin-forced reset path. Captures the source (script path, invoking OS user if available, timestamp).

 Both events surface prominently in the SA's own account activity view, so on next SA login the SA sees "your password was changed via `reset_sa_password.py` on 2026-XX-XX — was this you?" If it wasn't, the SA knows the box was touched at host level. This is the break-glass audit trail; without it, host-side password resets are invisible to the SA and SA compromise is undetectable.

### SU — Super User (day-to-day admin)

- **Purpose:** the primary working identity for the human the maintainer (or delegates). Normal board use plus cross-project admin (create users, manage memberships, reset passwords, soft-delete User/Viewer accounts).
- **Cookie:** `vf_session`, path=`/`, 12-hour expiry (same as any user).
- **Board access:** full. Sees all projects, can write everywhere.
- **Project access:** implicit admin on every project (no ProjectMember row required).
- **Creation:** provisioned by SA from the admin panel post-bootstrap. First SU must exist before `install_open` closes (see §6).
- **Cannot self-demote:** the "last active SU" guard in `admin.py` (already enforced on soft-delete and change-role) prevents stranding admin functions.
- **Audit:** actions recorded with actor_type=`user` and the SU's `display_name`.

> **Phase 1 scope note — SU admin panel access today.** In Phase 1 (an internal release + an internal release), an SU who needs the admin panel still authenticates via the **SA's** password at `/admin/login`. The resulting `vf_sa_session` session is bound to the SA user account, so the admin panel shows `SA: <sa display_name>` and every admin write is audited as the SA, not as the SU who actually performed the action. This is a known paradigm mismatch, not a bug we're fixing in Phase 1 — it becomes **bug 5.6** below, scope for **an internal release Phase 2** (the SU admin context / elevation-tier refactor). Phase 1 clarifies the role definitions and closes the board-login hole; Phase 2 closes the attribution hole.

### User

- **Purpose:** regular board participant. Creates projects, manages their own agents, works on assigned work.
- **Cookie:** `vf_session`.
- **Board access:** full, scoped to projects they own or are members of.
- **Project access:** explicit per `ProjectMember.role` (owner / admin / write / read at the per-project level).
- **Creation:** by SU (an internal release delegates this) or by SA.

### Viewer

- **Purpose:** read-only board participant. Cannot write.
- **Cookie:** `vf_session`.
- **Board access:** read-only.
- **Project access:** explicit membership; writes are rejected by `_require_write`.

### Account status (applies to all roles above)

The `users.status` column carries three values: `active`, `suspended`, `deleted`. Login rejects any status other than `active` with 403 — the status check runs **before** any password verification, so a disabled user who still remembers their credentials cannot authenticate.

- **`active`** — the default. All role semantics above apply normally.
- **`suspended`** — admin-toggled "disabled" state. Covered by **an internal release**: the admin panel adds a toggle on `/admin/users/{id}` to flip `active` ↔ `suspended`. Login returns `403` with the copy *"Your account is disabled. Contact your admin."* Audit events `user_suspended` / `user_unsuspended` record the action with the acting SA. SA cannot be suspended; the last active SU cannot be suspended (same invariant family as soft-delete).
- **`deleted`** — soft-delete, covered by existing `admin.py:844-902`. Restorable by SA.

The suspend/unsuspend flow is explicitly a reversible enable/disable toggle, not a destructive action. Compared with soft-delete, `suspended` preserves the user's ProjectMember rows, agents, and activity history — only the login gate flips.

---

## 3. Cookie model — two separate auth surfaces

There are exactly **two session cookies**, scoped to two non-overlapping URL paths:

| Cookie | Path | Expiry | Who holds it | Who issues it |
|---|---|---|---|---|
| `vf_session` | `/` | 12 h | SU, User, Viewer | `POST /ui/login` |
| `vf_sa_session` | `/admin/` | 30 min | SA only | `POST /admin/login` |

**Key constraints:**

- A single browser CAN simultaneously hold both cookies (useful when an SU also needs the admin panel, but that's not applicable to SA since SA doesn't have a `vf_session`).
- SA NEVER holds `vf_session`. Any code path that might issue a `vf_session` to a `super_admin` user is a bug (see §5).
- The `/ui/` routes check `vf_session` and reject SA outright. The `/admin/` routes check `vf_sa_session` and require it regardless of any `vf_session` present.

The admin panel provides a "Logout SA" link that clears `vf_sa_session`; this does NOT affect any `vf_session` (which SA wouldn't have anyway).

---

## 4. Bootstrap invariant

`install_open` is the gate that governs whether the first-run setup flow is still active. Today it checks: *"does at least one SA exist?"* That's insufficient — a fresh install can have SA but no SU, leaving the board unusable (no one can log in to `/ui/`).

**Revised invariant:** `install_open == True` UNTIL both of the following are true:

1. At least one active `super_admin` user exists.
2. At least one active `super_user` user exists.

Until both are true, `/ui/login` rejects all logins with a message directing the SA to create the first SU from the admin panel. The admin panel displays a banner at the top when `install_open` is true, with a CTA: "Create the first Super User."

**Stop-gap note:** this banner is explicitly a stop-gap. an internal release (First-boot setup wizard UI) will replace it with a proper wizard step. Code markers (`# TODO(an internal release): replace with wizard step`) mark the banner's temporary home so it doesn't drift into permanence.

---

## 5. Bugs this doc fixes

### Bug 5.1 — `/ui/login` accepts SA (an internal release / Ticket A)

`ui.py:358-391` does not check `user.role`. A `super_admin` user with valid credentials is issued a `vf_session` cookie and lands on `/ui/`.

**Fix:** after the password check, if `user.role == "super_admin"`, return `403` with a message directing them to `/admin/`. Audit as `login_blocked_sa`.

### Bug 5.2 — Bootstrap gives SA a board session (an internal release)

`bootstrap.py:234-254` creates the SA and immediately issues a `session_type="user"` cookie on path `/`. This is the root of "SA acts like a board user" — they literally are one, from minute zero.

**Fix:** after `create-sa`, set the `vf_sa_session` cookie (path=`/admin/`, 30-min) instead, and redirect to `/admin/`. The SA arrives at the admin panel already authenticated to it.

### Bug 5.3 — SA is implicit SU across project-level paths (an internal release / Ticket B)

The tuple `("super_user", "super_admin")` appears ~15 times across `projects.py`, `ui.py`, `members.py`. SA is treated as an implicit project admin/owner everywhere, which contradicts the matrix.

**Fix:** mechanical substitution — collapse all occurrences to `("super_user",)`. SA only retains explicit `_require_sa` paths in `admin.py`.

### Bug 5.4 — Legacy SA board sessions survive deploy (an internal release)

Any `vf_session` cookie currently issued to an SA is valid until its 12-hour expiry. A deploy that blocks new SA logins does not revoke these.

**Fix:** on app startup, delete `UserSession` rows where `session_type='user'` AND `user_id` is a `super_admin`. One-shot cleanup; idempotent on subsequent starts.

### Bug 5.5 — Stale model comment (an internal release)

`user.py:18` comment lists role values as `(super_admin, admin, user)`. Actual values are `(super_admin, super_user, user, viewer)`. The `admin` value is allowed by `admin.py:952` change-role but is not in the matrix.

**Fix:** correct the comment; remove `admin` from the allowed change-role values.

### Bug 5.6 — Admin action attribution collapses to SA regardless of acting human (**FIXED 2026-04-20 via an internal release Phase 2**)

**Status:** FIXED. See [su-elevation-tier.md](su-elevation-tier.md) for the full design + implementation. This section preserved as historical record.

**Was:** SUs borrowed the SA password at `/admin/login`; `_require_sa` returned the SA `User`; every admin write audited as `SA: <sa display_name>` regardless of which human clicked. Attribution hole.

**Fix (shipped an internal release Phase 2):** SUs re-confirm their OWN password at `POST /admin/elevate`. Server stamps `sessions.elevated_until = now + 15 min` on their existing `vf_session` (rolling window — extends on each admin action). `_require_sa` rebuilt to accept either (a) the legit SA cookie or (b) an elevated SU session, and returns the *acting* user — so `_audit` attributions self-correct. Per-request contextvars carry the elevation flag so `_audit` also tags `elevated: true` in details. Zero callsite edits needed.

**SA path preserved unchanged:** `vf_sa_session` cookie + `/admin/login` still work for the break-glass case. The dual-mode `/admin/login` page detects whether the current session is an SU and renders the elevate form; non-SU / anonymous gets the classic SA login form.

### Bug 5.7 — SA password-change events are not explicitly audited

Today, the host-side rescue script (`scripts/reset_sa_password.py`) rewrites the SA's password hash directly in the database. No ActivityEvent is recorded. If there's an SA self-change-password endpoint in `admin.py`, its audit (if any) uses a generic user action type, not an SA-specific one. Result: an SA password reset — whether routine or hostile — leaves no trace visible to the SA on next login.

**Why this matters:** SA is the break-glass identity. A host-side password reset is either a legitimate recovery event or a sign that someone with host access is trying to take over the SA account. The SA must be able to distinguish these on next login. Without an explicit audit event, the host-side rescue path is a silent backdoor.

**Fix (two event types):**

- `sa_password_self_change` — written by the admin.py change-password endpoint when the acting user is the SA. Includes IP, user-agent, timestamp.
- `sa_password_force_reset` — written by `scripts/reset_sa_password.py` when it completes the hash rewrite. Includes the script path, the OS user invoking the script (from `os.getenv("USER")` or `os.getlogin`), the hostname, and timestamp.

Both events must surface in the SA's own account activity view — not only in the global audit log — so on next SA login the SA is greeted with a top-of-page event ("your password was changed via reset_sa_password.py on 2026-XX-XX by `viveroot` on `<your-host>` — was this you?"). If unrecognised, the SA knows the box was touched at host level and can invoke an internal release Phase 2 procedures to investigate.

**Scope:** small. Three code changes — an audit write in `scripts/reset_sa_password.py`, an audit write (or event-type upgrade) in `admin.py`'s SA change-password endpoint, and a query in the SA account activity view to filter-highlight SA password events. **Scheduled as an internal release** — cut as its own ticket so the security-audit invariant gets a clean review rather than riding in on a cleanup commit.

---

## 6. Ship plan

### Phase 1A — an internal release (login block + bootstrap + banner)

1. `/ui/login` rejects `super_admin` with 403 + audit event.
2. Bootstrap `create-sa` issues `vf_sa_session`, not `vf_session`; redirects to `/admin/`.
3. `install_open` returns `True` until both SA and SU exist.
4. Admin panel shows "install not complete" banner while `install_open` is `True`.
5. Self-test on DEV: attempt SA board login → 403; verify fresh-install redirects to admin panel; verify banner shows until SU created.

### Phase 1B — an internal release (systemic demotion + session cleanup)

Blocked_by: an internal release.

1. Mechanical substitution: `("super_user", "super_admin")` → `("super_user",)` across `projects.py`, `ui.py`, `members.py`.
2. Startup hook: delete SA-held board sessions.
3. Fix `user.py:18` stale comment.
4. Remove `admin` from `admin.py:952` allowed change-role values.
5. Self-test on DEV: SA via `/admin/` still works; SA rejected on every project API call; legacy SA session revoked on restart.

### Phase 1C — an internal release (SA password-change audit)

Independent of an internal release and an internal release ordering-wise, but conceptually part of Phase 1 (closes bug 5.7). Three small code changes:

1. `scripts/reset_sa_password.py` writes `sa_password_force_reset` ActivityEvent on completion (script path, OS user, hostname, timestamp).
2. `admin.py` SA self-change-password endpoint writes `sa_password_self_change` instead of generic user event.
3. SA account activity view surfaces both event types with a prominent highlight on next SA login.

Can ship before, alongside, or after an internal release. Does not touch code paths in an internal release or an internal release scope, so no dependency ordering required.

### Phase 2 — an internal release (deferred, retained scope)

Introduces the SU admin context / elevation key / `elevated:true` flag on activity events. Not scheduled in this batch. Captured reasoning from 2026-04-11 remains on an internal release's notes.

---

## 7. Schema expectations (no migration required)

This doc does NOT require schema changes. Specifically:

- `users.role` enum-like column stays with the four advertised values: `super_admin`, `super_user`, `user`, `viewer`. (The legacy `admin` value gets removed from allowed writes but the column accepts it if it ever appears in existing data — a cleanup migration is scoped out and can happen opportunistically.)
- `user_sessions.session_type` stays with its existing values (`user`, `sa`). No new types.
- No new columns. No renames. No deletions.

---

## 8. What is explicitly NOT in this doc

To keep scope honest:

- ~~**SU admin context elevation (Phase 2 / an internal release).**~~ **SHIPPED 2026-04-20.** See [su-elevation-tier.md](su-elevation-tier.md). Bug 5.6 is now fixed — SUs elevate via their own password, audits self-attribute. TOTP remains future hardening; SA-restriction-to-recovery-only is still a future ticket.
- **Solo-mode UX hiding (an internal release).** Orthogonal; solo-mode hides UI elements but does not change identity semantics.
- **Owner transfer endpoint (an internal release).** Separate ticket; the "transfer a project to a new owner" flow is a project-level operation, not an identity-tier change.
- **User soft-delete cascade on owned projects (also an internal release).** Same ticket as ownership transfer.
- **Reset script damage (an internal release).** Separate bug track for the `scripts/` layer.
- **SU delegation of user creation (an internal release).** Depends on Phase 2's admin context tier.
- **First-boot wizard UI (an internal release).** Banner is the stop-gap; an internal release subsumes it.

---

## 9. Graduation record

**Graduated 2026-04-20** after an internal release + an internal release + an internal release all shipped to PROD and closed.

- Moved from: `0-MD/proposed/IDENTITY-ROLES-PROPOSAL.md` (deleted)
- Moved to: `0-MD/0-Documentation/public/identity-roles.md` (this file)
- Frontmatter: `status: Proposed` → `status: Active — signed off 2026-04-20`; version `0.1.0` → `1.0.0`.
- Cross-references added to: `user-agent-model.md`, `agent-contract.md`.
- Admin panel role-reference table link to this doc: TODO (trivial template edit; slot in on next admin UI touch).

---

## 10. Sign-off checklist

- [x] the maintainer signs off on §2 (four-role definitions). *Approved on an internal release, 2026-04-20.*
- [x] the maintainer signs off on §3 (two-cookie model). *Approved.*
- [x] the maintainer signs off on §4 (bootstrap invariant + banner as stop-gap). *Approved.*
- [x] the maintainer signs off on §6 (ship plan and phase split). *Approved; all three phases landed.*
- [x] the maintainer confirms D2 graduation destination (`identity-roles.md` in public tree). *This file.*

**All items ticked. Proposal is now load-bearing live documentation.** Any code deviation updates this doc in the same commit going forward.

---

## 11. Admin portal permission tiers — pointer to internal spec (added an internal release)

The four-role model in §2 says *who* has admin-portal access (SA always, SU on elevation). It doesn't say *what they can do once they're inside* — historically `_require_sa` was a single binary gate covering every admin write. As of **an internal release (2026-04-25)** that gate is split into three tiers:

- **Tier R (Read)** — view any admin-portal config. SA *or* SU-elevated.
- **Tier U (User-admin write)** — mutate users, agents, sessions, memberships, roles. SA *or* SU-elevated.
- **Tier S (System write)** — mutate certs, SSO, SMTP, backup, branding, feature flags, session policy. **SA only** — elevated SU is rejected.

Crossing into Tier S from SU-elevated is a deliberate UI step (popup confirmation → redirect to `/admin/login?as=sa` → return to origin). Audit attribution refines too: stacked SA still attributes to the SU human, only pure break-glass attributes as `actor: "SA"` with `break_glass: true`.

Full spec (workspace-level tier matrix, code surface, audit fields, visual states, viewer perms, etc.): **[admin-portal-perm-tiers.md](../internal/admin-portal-perm-tiers.md)** (internal).
