# Proposal: Persist the rotating Aim Lab session cookie (avoid forced re-login) — v4

> **Rev 4 (2026-06-17).** Supersedes v3
> ([`AUTH_SESSION_ROTATION_PROPOSAL_v3.md`](AUTH_SESSION_ROTATION_PROPOSAL_v3.md)). This file is the
> current draft; older drafts are kept for the review diff and move to Git history at finalization.
>
> - **Rev 4 — P2 (`login` reset race) + a focused concurrency self-review.** `login`'s state-file
>   reset must run under the **same `data/session.lock`** (else a concurrent sync re-creates the
>   state file with the old chain *after* login's delete, shadowing the fresh login). Specified the
>   lock ordering (**delete-before-write**, lock only the brief commit not the 300 s browser window)
>   and coupled it to §3's re-resolve-inside-the-lock. The self-review additionally hardened §3:
>   **atomic** stale-lock reclaim (naive "dead PID → delete+recreate" double-acquires → forks),
>   PID-reuse-safe liveness, same-dir temp + `chmod 0600` before `os.replace`, failure-tolerant
>   reads, and surfacing persist-after-mint failures. Also fixed stale "rev 2" header wording (P3).
> - **Rev 3** — P2: `--session-file` independence contract (one source = one login) + minting-path
>   warning; kept `--session-file` read-only/non-persisted.
> - **Rev 2** — P1 (lock wraps *resolve* → mint → persist; re-resolve inside the lock; route every
>   minting caller through it, incl. `cli.py:162-166`), P2a (`--session-file` not rotation-managed),
>   P2b (persist **only** after a successful mint — never the collapsed dead cookie).

**Status:** Proposal — **rev 4; ready to implement; no open questions.** The run-history pipeline
build is complete (M1–M6b merged), the go/no-go is resolved (see **Decisions**), the blocking
empirical gate — the `--no-follow-rotation` control run — **passed 2026-06-17** (evidence:
`_monitor_session_control.log`), and the design is fully settled (see **Decisions** +
**Implementation spec**). This doc is the complete spec for Codex; the remaining work is the build
itself (one PR off `main`, per the CLAUDE.md workflow).
**Investigated:** 2026-06-07 (Claude Code). **Re-baselined:** 2026-06-17 onto the productized
auth layer (`aimlabs_auth.py`) after M6b retired the PoC scripts. **Revisions:** rev 2–rev 4
2026-06-17 (review findings + concurrency self-review above).
**Related:** PR #8 (error-message disambiguation — the *first half* of this, **merged to `main`**,
now landed as `ReloginRequiredError`); `aimlabs_auth.py` (`fetch_session_json` /
`resolve_session_cookie` / `write_env_var` / `extract_session_cookie`);
`docs/RUN_HISTORY_ARCHITECTURE.md` §4 / §8.4 / §17 auth assumptions.

---

## TL;DR

`AIMLAB_SESSION` does **not** reliably last "~30 days" the way the script's docstring and the
design assume. The upstream OAuth **refresh tokens are single-use / rotating**: every
access-token refresh consumes the current refresh token and mints a new one, returned inside a
**new session cookie** via the response's `Set-Cookie` header. The script reads a *static*
cookie from `.env` and never persists the rotated one, so after the **first** refresh the
stored cookie is stale. The next refresh re-presents the spent token, the provider's
reuse-detection rejects it (`accessTokenError: "RefreshAccessTokenError"`), and the user is
forced to re-login — typically within ~1–2 hours of capture, not ~30 days.

**Fix:** after each `/api/auth/session` call **that returns a fresh access token**, **persist the
returned `Set-Cookie` session-token over the stored value** (atomic write, single-flight lock), so
the stored credential always holds the current link in the rotation chain. A *failed* refresh is
**never** persisted (it returns a collapsed, dead cookie — rule 1 / Implementation spec §1–2). That
lets the rolling ~30-day session keep minting hour-long bearers with no re-login.

---

## Decisions (resolved 2026-06-17, with the user)

- **(A) Go: build it.** `sync` is **unattended/scheduled by design** — it never opens a login
  window (design §4 + decision 23: no `--no-login`, no interactive detection). A refresh token
  that silently dies ~1 h in therefore breaks *every* scheduled run until a human re-logs in —
  exactly the failure this fix removes. (For a purely interactive tool the cheaper "re-login
  when it breaks" might win; that's not this tool.)
- **(D) Storage: a JSON token-state file owned by the shared auth layer** (see *Where it should
  live*), not a per-run `.env` rewrite.
- **Blocking gate — CLEARED (2026-06-17):** the `--no-follow-rotation` control run passed
  (Investigation §4). The design is now fully settled in **Implementation spec** — no open
  questions remain; what's left is the build.

---

## Background: there are two token layers

1. **The aimlabs session** — the `__Secure-next-auth.session-token` cookie (a NextAuth
   encrypted JWE). This is the long-lived credential `AIMLAB_SESSION` stores. Session
   `expires` is ~30 days out.
2. **The upstream OAuth tokens** that aimlabs' backend holds *inside* that session JWT: a
   short-lived **access token** (~1 h) plus a **refresh token**.

`GET https://aimlabs.com/api/auth/session` returns the current access token, transparently
refreshing it server-side (via the stored refresh token) when it has expired. The auth layer
(`aimlabs_auth.get_bearer_from_session`) exchanges `AIMLAB_SESSION` for a fresh bearer this way
on every run.

Two behaviors of that endpoint matter here:

- **It re-issues the cookie on *every* call** (rolling re-encryption): the `Set-Cookie`
  session-token value changes each time even when nothing material changed.
- **When it actually refreshes** (access token expired), it **rotates the refresh token**
  (RT0 → RT1) and bakes RT1 into that new cookie. RT0 becomes single-use/spent.

---

## Symptom

The script reported **"your Aim Lab session has expired"** even though the session was valid
(cookie good for ~30 days, user authenticated). The real response was HTTP 200 with
`accessTokenError: "RefreshAccessTokenError"` and **no** `accessToken`.

**Already addressed by PR #8** (error-message disambiguation), now productized as
`ReloginRequiredError` (raised whenever `/api/auth/session` returns an `accessTokenError`): it
distinguishes "the session is fine but the access-token refresh failed — re-login" from a
genuinely missing/expired session and from transient network errors. PR #8 does **not** fix the
underlying cause — that's this proposal.

> **PR #8 wording caveat — RESOLVED.** PR #8's original message blamed the refresh token being
> "revoked (commonly by logging in again elsewhere)," which the investigation below shows is
> usually wrong (the typical cause is rotation/staleness, no second login). Productization
> already neutralized this: the current `ReloginRequiredError` message just says aimlabs
> "accepted the session but could not refresh an access token; run `voltmeter login`" — no
> "revoked elsewhere" claim remains, so no separate wording follow-up is owed.

---

## Investigation & evidence

**1. Live diagnosis of the failing cookie.** `GET /api/auth/session` with the stored cookie:
```
HTTP 200
keys = ['accessTokenError', 'expires', 'user']
expires           = 2026-07-07   (session valid ~30 more days — NOT expired)
accessTokenError  = 'RefreshAccessTokenError'
accessToken       = <absent>
```
Re-calling with the freshly-rotated `Set-Cookie` also failed — the session was already in a
terminal error state (its JWT had been stripped from 1377 → 340 chars, i.e. tokens removed).

**2. User experiment (the timing tell).** Captured a fresh cookie at ~01:00; it worked for a
while, then failed by ~02:35 (~95 min later) — **with no browser open** on aimlabs.com. Too
fast for idle/inactivity expiry (days), and it lands right after the ~1 h access-token
lifetime, i.e. at the first moment a *refresh* is actually required.

**3. Decay monitor (the decisive test).** A throwaway monitor (`_monitor_session.py`) polled
`/api/auth/session` every 60 s from a fresh login, as the **sole consumer (no browser)**, and
**followed the rotated `Set-Cookie`** each poll. Key transition:
```
03:46:38  token=Y  ATE=03:48:00 (in +1.4m)    ← original access token, about to expire
03:47:38  token=Y  ATE=04:47:39 (in +60.0m)   ← REFRESH SUCCEEDED: expiry jumped +1h
...                                            ← still healthy 40+ min later
```
Following the rotated cookie, the refresh **succeeded** and the access token was renewed for
another hour. (Access-token lifetime ≈ 60 min.)

**4. Control run — `--no-follow-rotation` (2026-06-17, closes the A/B).** Same sole-consumer
conditions but *not* following the rotated cookie. Key transition:
```
14:32:50  #59  token=Y  ATE=14:34:16 (in +1.4m)         ← original access token, about to expire
14:33:50  #60  token=Y  ATE=15:33:50 (in +60.0m)        ← FIRST refresh SUCCEEDED (RT0's one use)
14:34:51  #61  token=N  err=RefreshAccessTokenError     ← reuse of spent RT0 → family revoked (cookie 1377→340)
```
The refresh works **exactly once**, then the next reuse of the static cookie dies — the
single-use-rotation fingerprint. Versus run 3 (followed rotation → survived the same boundary),
the lone variable is whether the rotated cookie is followed, so the discarded `Set-Cookie` is
the confirmed cause. Evidence: `_monitor_session_control.log`. (Note the failure response at #61
still carries a `Set-Cookie` — a *collapsed, dead* 340-char cookie; this is why persistence must
be gated on a successful mint, P2b / rule 1.)

**Conclusion:** refresh is **not** fundamentally broken — there is no hard 1-hour cap. The
session can be sustained **iff** the caller follows the rotated cookie. The script fails only
because it keeps reusing the static `.env` cookie, whose refresh token is consumed on the first
refresh; the second refresh re-presents the spent token → reuse-detected → `RefreshAccessTokenError`.
This exactly matches the user's 01:00 → 02:35 failure.

> Operational aside: the monitor's first run was killed at ~03:08 when the machine slept at
> ~3 AM (idle). Relevant for any future unattended/headless companion — background runs must
> survive (or prevent) system sleep.

---

## Root cause

Single-use / rotating upstream refresh tokens, combined with the script treating
`AIMLAB_SESSION` as a static credential and **discarding the rotated cookie** the server
returns. The stored cookie therefore goes stale one refresh after capture.

---

## Proposed fix

After a `/api/auth/session` call **that returns a fresh access token**, capture the response's
`Set-Cookie` session-token and **persist it** before using the bearer for anything else. The
stored value then always holds the newest link in the chain (C0 → C1 → C2 …), so the rolling
~30-day session keeps minting bearers with no re-login. A response carrying `accessTokenError`/no
token is **not** persisted (rule 1, Implementation spec §1–2).

Walked through the user's "run at 1:00, run again at 3:00" example:

| Time | What happens | Stored cookie after |
| --- | --- | --- |
| **1:00 login** | Capture **C0** (RT0 + access token AT0, ~1 h). | C0 |
| **1:00 run** | AT0 still fresh → no refresh → use AT0. | C0 (unchanged) |
| **3:00 run** | AT0 expired → server refreshes with RT0 → mints AT1, **rotates RT0→RT1**, returns **C1** via `Set-Cookie`. Use AT1. | **persist C1** ✅ |
| next run | Sends C1 → refreshes with RT1 → C2 … chain continues. | C2 … |

Without the persist step: the 3:00 run still works (RT0's first use), but the **next** run
re-sends spent RT0 → `RefreshAccessTokenError` → forced re-login. With it: indefinite within
the session window.

---

## Design rules / gotchas (must-haves, not optional)

1. **Persist immediately and atomically — but only on a successful mint.** On a response that
   returns a fresh access token, write the rotated cookie the moment it returns (before the heavy
   API work), via temp-file + `os.replace` (atomic rename). The dangerous window is "refresh
   happened (RT0 now dead) but the new cookie isn't saved yet" — a crash there bricks the stored
   credential. **Never persist a failed-refresh response (P2b):** it returns a *collapsed, dead*
   cookie (1377 → 340 chars in the evidence), and writing it over good state would manufacture the
   very lockout we're preventing. On failure, leave the state file untouched and raise
   `ReloginRequiredError`.
2. **Be the sole consumer / single-flight it.** Anything *else* hitting `/api/auth/session`
   — a browser tab on aimlabs.com, a second concurrent run — forks the chain and trips
   reuse-detection, which can revoke the whole token family. The single-flight lock must cover
   **resolve → mint → persist as one unit** (Implementation spec §3), and document "don't keep
   aimlabs.com open in a browser while the tool is the credential holder."
3. **Only re-login on a genuine `ReloginRequiredError`.** Distinguish "refresh truly failed →
   must re-login" from a transient network/HTTP blip, so a still-good cookie isn't discarded.
   (PR #8 introduced exactly this distinction — build on it.)
4. **Handle chunked `Set-Cookie` (`.0`/`.1`/…).** If the token grows past ~4 KB, NextAuth splits
   it across cookies. Reuse `extract_session_cookie` (`aimlabs_auth.py`) — the `login` capture
   path's helper, which **already joins chunked cookies** — rather than assuming a single cookie.
   (This means rule 4 is effectively already implemented for the read side.)
5. **Rolling vs. absolute expiry — confirm in production, do NOT block on it.** NextAuth defaults
   to a *rolling* session (each use pushes `expires` out ~30 days), which is what makes "never
   re-login" true. If aimlabs set an *absolute* cap, a re-login is still required at that hard
   limit regardless of persistence — but the fix is a large win either way (it turns "dies at
   the first refresh ~1 h in" into "survives the whole session window unattended"). Confirming
   rolling needs an `expires` comparison across a **>24 h** horizon (NextAuth only rolls after
   `updateAge`, default 24 h), so a few-hour run can't answer it — let it accrue in production
   rather than gating implementation. `ARCHITECTURE.md` already *assumes* rolling; treat that as
   provisional.

---

## Where it should live

**Decided (D): a small JSON token-state file owned by the shared auth layer (`aimlabs_auth.py`),
atomic write + run lock in one place** — *not* a per-run `.env` rewrite. Rationale: the
credential is now read by **both** `voltmeter sync` and `aimlab_scores` (both go through
`aimlabs_auth.resolve_session_cookie`), and `.env` is a hand-maintained file, so rewriting it on
every run is the riskier path. Centralize rotate-and-persist in `aimlabs_auth` so both consumers
benefit and only one place owns the write.

Re-baselined onto the current code (M6b retired the PoC `aimlab_history.py`):

- **The discard is in `fetch_session_json` (`aimlabs_auth.py`):** it reads the response *body*
  but never reads the `Set-Cookie` *header*. Capture the rotated session-token there (reuse
  `extract_session_cookie`'s chunk-join) and persist it from the **locked mint path (§3), on a
  successful mint only (§1/§2)**.
- **Building blocks already exist (renamed/public):** `write_env_var`, and `extract_session_cookie`
  — which already handles the chunked `.0`/`.1` case (rule 4), so that part is done.
- **Atomicity is NOT yet there:** `write_env_var` does a plain `write_text`, not temp-file +
  `os.replace`. The new state writer must be atomic (rule 1); don't reuse `write_env_var` as-is.
- **Design reconciliation:** `docs/RUN_HISTORY_ARCHITECTURE.md` **§4 / §8.4 / §17** currently
  classify `RefreshAccessTokenError` as **terminal** ("the only fix is a fresh `login`") and call
  the cause "not understood." Both are now disproven (Investigation §4). Reconcile them in the
  **same PR** as the code — see **Implementation spec §4**.

---

## Implementation spec (settled 2026-06-17; refined through rev 4)

The last open design questions, now decided so this doc is the complete spec. Simplicity-first:
these pin only what Codex shouldn't have to guess; everything else is Codex's call.

### 1. Token-state file — path, schema, precedence, persist gating, login reset

- **Path:** `data/session.json`, written/managed by `aimlabs_auth`. Use the tool-owned, already
  gitignored `data/` dir (home of the run-history DB) — deliberately **not** the hand-maintained
  `.env` (that separation is the whole point of decision D). Create it `0600` on POSIX (mirror
  `write_env_var`), and add an explicit Secrets-section `.gitignore` line as belt-and-suspenders
  (it holds a "logged-in-as-you" credential).
- **Schema (minimal):**
  ```json
  {
    "version": 1,
    "session_cookie": "<rotated value, or chunked 'n0=v0; n1=v1' string>",
    "expires": "<session `expires` from /api/auth/session>",
    "updated_at": "<ISO-8601 UTC when this link was persisted>"
  }
  ```
- **Resolution precedence** (`resolve_session_cookie`, extended): `--session-file` (explicit
  override) > **`data/session.json`** > `$AIMLAB_SESSION` > `.env`. The state file wins over
  env/`.env` because those hold the *original* capture (C0), which dies one refresh after capture,
  whereas the state file holds the current chain link. A corrupt/unparseable state file is treated
  as **absent** (warn, fall through) — never a hard failure. `scores` and `sync` both read through
  this one function, so both pick up the current link.
- **`--session-file` is a read-only, *non-rotation-managed* override (P2a).** It still wins for
  *reads* ("use exactly this"), but its rotations are **not** persisted to the shared state file —
  a one-off override must not silently become the default credential for later runs. Consequence:
  because the pointed-at file is never written back, repeated `--session-file` runs are **not**
  rotation-protected (they'd die after the first refresh, like the pre-fix bug). That's acceptable
  for a one-off/debug override; **for unattended/scheduled use, use the default `.env` → state-file
  channel**, which is the protected path.
- **Independence contract — one credential source = one login (P2 v3, load-bearing).**
  `--session-file` must point at an **independent session** (its own refresh-token family), never a
  cookie derived from the same login that seeds `.env`/`data/session.json`. Reuse-detection is
  server-side and *per family*, so a `--session-file` sync that triggers a refresh consumes that
  family's current refresh token; if it's the **same** family as the managed chain — *even a
  different cookie value from the same login* — the next default sync re-presents a now-spent token
  and the whole family (both stores) is revoked. No client-side scheme can make two stores over one
  server-side family safe, so this is a documented contract, not a feature. (Persisting rotation
  back to the override source would **not** fix it — the *other* store still strands/revokes the
  family; it only "works" if you use `--session-file` exclusively, which is just this rule.)
- **Guard (simple, P2 v3).** When `--session-file` is used on the **minting path** (`sync` / bearer
  mint), emit a loud one-line warning to the warning stream — e.g. *"using --session-file: this run
  will not persist rotation; the file must be an independent login or it can revoke your managed
  credential."* A stricter `--allow-unmanaged-session` opt-in gate was considered and **deferred**
  as unneeded config for a single-account tool (simplicity-first); revisit only if a hard block is
  wanted.
- **Persist (P2b + P2a):** writes `data/session.json` **atomically** (temp-file + `os.replace`)
  **only after a successful mint** (a response with a fresh `accessToken`; never on
  `accessTokenError` — that returns a dead cookie, rule 1), and **only for the default channels**
  (state file / `$AIMLAB_SESSION` / `.env`) — *not* for a `--session-file` run. A successful mint
  with no refresh (rolling re-encrypt only) is still persisted; it's harmless and keeps state
  current. Flow: fresh login → `.env` C0 → first refresh persists C1 → later runs read C1 (state
  file wins) → C2 … .
- **`login` resets rotation — under the lock (critical, P2 v4).** A fresh login starts a new chain;
  since the state file *wins* over `.env`, a leftover dead last-link would otherwise shadow the
  fresh capture and fail immediately. So on a successful `voltmeter login`, **under the same
  `data/session.lock` (§3)** and in this order: **(1) delete `data/session.json` first, (2) write
  the fresh cookie to `.env`,** (3) the existing verify step then re-seeds the chain (below).
  - **Why the lock:** without it, a concurrent sync mid-`resolve→mint→persist` on the old chain can
    re-create `data/session.json` with a stale link *after* login's delete, shadowing the fresh
    login (and wasting it — people usually `login` *because* the chain broke). The lock serializes
    them; combined with §3's *re-resolve-inside-the-lock*, a post-login sync reads the fresh `.env`.
  - **Why delete-before-write:** it's crash-safe — a crash between the steps leaves *no state file +
    old `.env`* (at worst a re-login), never a fresh `.env` shadowed by stale state.
  - **Hold the lock only for the commit, not the browser window:** `login` can wait up to
    `DEFAULT_LOGIN_TIMEOUT_SECONDS` (300 s) for the user; capture/auth happen lock-free, and the
    lock is taken only for steps 1–2 + the verify-mint.
- **The login verify-mint seeds the new chain.** `login` already verifies the captured cookie
  (`_verify_and_report_identity` → `fetch_session_json` in `aimlabs_auth.py`). Under this design
  that verify *is* a successful default-channel mint, so it persists the first rotated link (D1),
  also under the lock. Net: after login the state is cleared then re-seeded from the fresh login,
  with no window for the old chain to leak back in.

### 2. Scope — only the bearer path rotates; `scores` only reads

Refresh/rotation happens **only at `GET /api/auth/session`**, i.e. only in `fetch_session_json`
(via `get_bearer_from_session` / `resolve_bearer`, used by `sync` and login-verify). `aimlab_scores`
sends the cookie to its own endpoint and **never calls `fetch_session_json`**, so it does **not**
rotate. Therefore:

- **Persist-on-rotation lives in exactly one place** — the bearer-mint path, inside the
  single-flight locked function (§3). `fetch_session_json` must read the response's `Set-Cookie`
  (reuse `extract_session_cookie`'s chunk-join), and the locked mint path persists the rotated link
  **after a successful mint only** (never on `accessTokenError`; on failure, leave the state file
  untouched, release the lock, raise `ReloginRequiredError`). The `SessionFetcher` seam must carry
  the rotated cookie out so the **mocked regression test can exercise persistence** (today it
  returns only the JSON body).
- **`scores` benefits passively** — no scores-side change beyond reading through the updated
  `resolve_session_cookie`.

### 3. Single-flight lock — one locked function owns resolve → mint → persist (P1)

The lock is worthless if a caller resolves the cookie *before* acquiring it: process A reads C1,
blocks on the lock, B rotates C1→C2 and persists, A wakes and mints with the stale C1 → forks the
chain (the exact thing the lock exists to prevent). Today `cli.py:162-166` resolves the session and
*then* mints — outside any lock — so this is a live shape, not a hypothetical.

**Contract — a single auth-layer function owns the whole critical section, in this order:**

1. acquire the exclusive lock;
2. **(re-)resolve the credential as the first step *inside* the lock** — re-run the full precedence
   (state file → `$AIMLAB_SESSION` → `.env`); never trust a cookie resolved *before* the lock was
   held (don't cache a pre-lock value across the wait);
3. call `/api/auth/session` (mint);
4. on success, persist the rotated cookie atomically (§1); on `accessTokenError`, persist nothing;
5. release the lock (in a `finally`).

All bearer-minting callers route through this one function — including the split `cli.py:162-166`
path (refactor it to call this rather than resolve-then-mint) **and any mid-run re-mint on a 401**
(design §8.4). `login`'s reset (§1) takes the **same** lock — the lock guards *every* writer of
`data/session.json`, not just the mint. **Scope:** hold the lock only for resolve → mint → persist;
release it **before** the heavy data fetch (never hold it across the whole sync).

**Concurrency & crash-safety (auth is sensitive — required, not nice-to-have):**

- **Lockfile = advisory, existence-based.** `data/session.lock`, created via
  `os.open(..., O_CREAT | O_EXCL)` (atomic create-if-absent; works on Windows *and* POSIX). Write
  `{pid, start_time}` into it and **close the fd** — represent the lock by the file's *existence*,
  not a held handle, so cleanup/reclaim works on Windows (a held handle there can block deletion).
  Release = `unlink`.
- **Stale-lock reclaim must be atomic** (else it re-introduces the fork). "Check dead PID, then
  delete + recreate" is itself racy: two contenders both see the stale lock, both reclaim, both
  proceed → concurrent mint → forked chain. Reclaim via an **atomic steal** — rename the stale
  lockfile to a unique name (`os.replace` of that inode succeeds for exactly one contender); the
  winner confirms the dead `{pid, start_time}`, removes it, and re-acquires. Losers retry.
- **Survive PID reuse.** Liveness = recorded `pid` is alive **and** its `start_time` matches (a dead
  PID can be recycled by an unrelated process), or treat a lock older than a generous lease as
  stale. Never reclaim on bare "PID is alive."
- **Atomic write, done right.** The temp file must live in the **same dir** (`data/`) as
  `session.json` so `os.replace` is genuinely atomic (it isn't across filesystems), and be
  `chmod 0600` **before** the replace so the credential is never briefly world-readable.
- **Reads are failure-tolerant.** Any failure to read state — missing, deleted mid-read (login can
  `unlink` it), partial, unparseable, unknown `version` — is treated as **absent** (warn, fall
  through to `$AIMLAB_SESSION`/`.env`), never a crash.
- **Persist failure after a successful mint must be surfaced, not swallowed.** If the mint succeeded
  (RT already rotated server-side) but the atomic write fails (disk full, perms), the chain has
  advanced without being saved → the next run will need re-login. Raise/log a loud, specific error;
  don't silently continue as if the state were saved.
- **Who skips the lock:** `--session-file` runs (they mint but never persist, and are an independent
  family by contract, §1) and `scores` (read-only, non-rotating). A lock-free `scores` reader sees
  old-or-new via atomic `os.replace` (never torn), and a vanished file simply falls through.

### 4. Design-doc reconciliation — same PR

Update `docs/RUN_HISTORY_ARCHITECTURE.md` **§4 / §8.4 / §17** in the **same PR** as the code (the
code is what makes "recoverable" true; a separate docs PR would leave `main` self-contradictory).
Flip the framing: `RefreshAccessTokenError` is **normally recoverable** — the rotated cookie is
persisted and reused, so the unattended session sustains itself — and **terminal only** when the
session itself is gone, the chain was forked (a browser tab / second run), or the state file was
lost. Keep `ReloginRequiredError` as the surfaced error **for that residual terminal case**, so the
existing "run `voltmeter login`" path stays correct as the fallback rather than the expected ~1 h
outcome.

---

## Post-ship verification (non-blocking)

Nothing here blocks implementation — these accrue *after* the fix ships.

- **Control run — CLEARED (2026-06-17).** Was the blocking gate; result + evidence in
  Investigation §4 (`_monitor_session_control.log`). Its success-then-die-on-reuse sequence is the
  regression test (§2 / Acceptance criteria). Reproduce recipe below, retained for future use.
- **Rolling vs. absolute session lifetime** — see rule 5; needs a >24 h `expires` comparison, so
  let it accrue in production rather than gate the build.
- **Second-cycle confirmation** — fold into a long follow-rotation run (`--max-min 150` crosses the
  second ~60-min refresh boundary, ~04:47 in the captured log); hardening only.

**Reproduce the control run** — sole consumer (no `aimlabs.com` tab, no concurrent `sync`/`scores`),
disable system sleep, run from the repo root (where `.env` lives):
```
voltmeter login        # fresh cookie (unspent refresh token)
python proof-of-concepts/auth-session-rotation/_monitor_session.py \
    --no-follow-rotation --interval 60 --max-min 90
```
If it fails in the first poll or two (well before 60 min), the "fresh" cookie wasn't fresh —
re-login and retry.

---

## Acceptance criteria (when implemented)

- After a refresh **that succeeds**, `data/session.json` reflects the rotated cookie; a subsequent
  run (`sync` or `scores`) reuses it without re-login.
- A **failed** refresh (`accessTokenError`) does **not** overwrite the state file with the dead
  cookie — it leaves the last-good state intact and raises `ReloginRequiredError`.
- A `--session-file` run does **not** write `data/session.json` (explicit overrides are not
  rotation-managed), and a `--session-file` run on the minting path emits the warning that it
  won't persist rotation and must be an independent login.
- `resolve_session_cookie` prefers `data/session.json` over `$AIMLAB_SESSION`/`.env`; a corrupt
  state file is ignored with a warning, not a crash.
- `voltmeter login` deletes `data/session.json` then writes `.env` (delete-before-write), **under
  `data/session.lock`**, so a stale last-link cannot shadow the freshly captured cookie.
- A `login` concurrent with an in-flight `sync` cannot resurrect the old chain: the lock serializes
  them and the post-login `sync` re-resolves the fresh `.env` (no stale state shadows the login).
- Survives ≥2 consecutive refresh cycles unattended (sole consumer).
- A `--no-follow-rotation`-style path still fails after the first refresh (proves the fix is
  what's responsible) — encoded as a regression test with a mocked session endpoint that rotates
  `Set-Cookie`.
- Crash between refresh and persist does not corrupt the state file (atomic temp-file in the same
  dir + `chmod 0600` before `os.replace`); a half-write is impossible. A persist failure *after* a
  successful mint is surfaced loudly (not swallowed).
- A read that races a delete/replace (missing, partial, deleted mid-read) falls through to
  `$AIMLAB_SESSION`/`.env`, never crashing.
- Two concurrent rotating runs cannot fork the chain: resolve → mint → persist runs inside one
  `data/session.lock`, with state **re-resolved inside the lock**; reclaiming a stale lock is
  **atomic** (no two contenders both acquire) and survives PID reuse.
- A genuine `ReloginRequiredError` (not a network blip) is the only thing that prompts re-login.

---

## Appendix: monitor methodology

`_monitor_session.py` (in this directory): polls `/api/auth/session` on an interval as the sole
consumer, **follows** the rotated `Set-Cookie` session-token across polls, masks all token
values, and logs — relative to the first observed `accessTokenExpiresAt` — exactly when
`accessToken` disappears vs. when it advances. Flags: `--interval`, `--max-min`,
`--no-follow-rotation`. It reads `AIMLAB_SESSION` from `.env` in the current working directory
and appends to `_monitor_session.log` there too, so run it from the **repo root** (where `.env`
lives, after a fresh `voltmeter login`); the committed `_monitor_session.log` in this directory
is a captured run kept as evidence. When this work is picked up, promote the monitor (or its
essence) into a proper dev/diagnostic tool or test fixture rather than re-deriving it.
Healthy cookie ≈ 1377 chars; a terminal `RefreshAccessTokenError` cookie collapses to ≈ 340
chars (tokens stripped) — a quick at-a-glance health signal.
