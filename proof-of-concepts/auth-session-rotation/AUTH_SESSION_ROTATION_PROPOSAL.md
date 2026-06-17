# Proposal: Persist the rotating Aim Lab session cookie (avoid forced re-login)

**Status:** Proposal — **ready to implement; no open questions.** The run-history pipeline build
is complete (M1–M6b merged), the go/no-go is resolved (see **Decisions**), the blocking empirical
gate — the `--no-follow-rotation` control run — **passed 2026-06-17** (evidence:
`_monitor_session_control.log`), and the design is fully settled (see **Decisions** +
**Implementation spec**). This doc is the complete spec for Codex; the remaining work is the build
itself (one PR off `main`, per the CLAUDE.md workflow).
**Investigated:** 2026-06-07 (Claude Code). **Re-baselined:** 2026-06-17 onto the productized
auth layer (`aimlabs_auth.py`) after M6b retired the PoC scripts.
**Scope when picked up:** a self-contained change to the shared auth layer (`aimlabs_auth.py`),
plus reconciling the credential/auth design. Mirrors the one-PR-off-`main` workflow.
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

**Fix:** after each `/api/auth/session` call, **persist the returned `Set-Cookie`
session-token back over the stored value**, wrapped in an atomic write and a single-run lock,
so the stored credential always holds the current link in the rotation chain. That lets the
rolling ~30-day session keep minting hour-long bearers with no re-login.

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
the confirmed cause. Evidence: `_monitor_session_control.log`.

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

After every `/api/auth/session` call, capture the response's `Set-Cookie` session-token and
**persist it over the stored `AIMLAB_SESSION`** before doing anything else. The stored value
then always holds the newest link in the chain (C0 → C1 → C2 …), so the rolling ~30-day session
keeps minting bearers with no re-login.

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

1. **Persist immediately and atomically — before the heavy API work.** The dangerous window is
   "refresh happened (RT0 now dead) but the new cookie isn't saved yet." A crash there bricks
   the stored credential = lockout. Write the rotated cookie the moment the session response
   returns, via temp-file + `os.replace` (atomic rename).
2. **Be the sole consumer / single-flight it.** Anything *else* hitting `/api/auth/session`
   — a browser tab on aimlabs.com, a second concurrent run — forks the chain and trips
   reuse-detection, which can revoke the whole token family. Take a run lock (e.g. a lockfile)
   so two runs can't race, and document "don't keep aimlabs.com open in a browser while the
   tool is the credential holder."
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
  but never reads the `Set-Cookie` *header*. That's the line to change — capture the rotated
  session-token there (or in `get_bearer_from_session`) and hand it to the state writer.
- **Building blocks already exist (renamed/public):** `write_env_var`, and `extract_session_cookie`
  — which already handles the chunked `.0`/`.1` case (rule 4), so that part is done.
- **Atomicity is NOT yet there:** `write_env_var` does a plain `write_text`, not temp-file +
  `os.replace`. The new state writer must be atomic (rule 1); don't reuse `write_env_var` as-is.
- **Design reconciliation:** `docs/RUN_HISTORY_ARCHITECTURE.md` **§4 / §8.4 / §17** currently
  classify `RefreshAccessTokenError` as **terminal** ("the only fix is a fresh `login`") and call
  the cause "not understood." Both are now disproven (Investigation §4). Reconcile them in the
  **same PR** as the code — see **Implementation spec §4**.

---

## Implementation spec (settled 2026-06-17)

The last open design questions, now decided so this doc is the complete spec. Simplicity-first:
these pin only what Codex shouldn't have to guess; everything else is Codex's call.

### 1. Token-state file — path, schema, precedence, login reset

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
  override, unchanged, read-only) > **`data/session.json`** > `$AIMLAB_SESSION` > `.env`. The
  state file wins over env/`.env` because those hold the *original* capture (C0), which dies one
  refresh after capture, whereas the state file holds the current chain link. A corrupt/
  unparseable state file is treated as **absent** (warn, fall through) — never a hard failure.
  `scores` and `sync` both read through this one function, so both pick up the current link.
- **Persist** writes `data/session.json` **atomically** (temp-file + `os.replace`) on the refresh
  path (§2), regardless of which channel the cookie was read from: fresh login → `.env` C0 → first
  refresh persists C1 → later runs read C1 (state file wins) → C2 … .
- **`login` resets rotation (critical correctness point):** on a successful `voltmeter login`,
  **delete `data/session.json`** when writing the fresh cookie to `.env`. A fresh login starts a
  new chain; since the state file *wins* over `.env`, a leftover dead last-link would otherwise
  shadow the fresh capture and fail immediately. Next sync rebuilds the state file from the new
  `.env` cookie.

### 2. Scope — only the bearer path rotates; `scores` only reads

Refresh/rotation happens **only at `GET /api/auth/session`**, i.e. only in `fetch_session_json`
(via `get_bearer_from_session` / `resolve_bearer`, used by `sync` and login-verify). `aimlab_scores`
sends the cookie to its own endpoint and **never calls `fetch_session_json`**, so it does **not**
rotate. Therefore:

- **Persist-on-rotation lives in exactly one place** — the bearer-mint path. `fetch_session_json`
  must read the response's `Set-Cookie` (reuse `extract_session_cookie`'s chunk-join), and the
  mint path writes the rotated link after a successful mint. The `SessionFetcher` seam must carry
  the rotated cookie out so the **mocked regression test can exercise persistence** (today it
  returns only the JSON body).
- **`scores` benefits passively** — no scores-side change beyond reading through the updated
  `resolve_session_cookie`.

### 3. Single-flight lock

- An **exclusive lockfile** `data/session.lock` guards the read→refresh→persist critical section
  on the rotating path, so two concurrent `sync`/login runs can't both refresh and fork the chain.
  Acquire via `os.open(..., O_CREAT | O_EXCL)` (works on Windows *and* POSIX); release (unlink) in
  a `finally`. Handle a **stale lock**: if the file exists but its recorded PID is not alive,
  reclaim it — a crashed run must not wedge the tool forever. (`os.replace` is atomic on Windows,
  so the state write itself needs no extra OS lock.)
- `scores` does **not** take the lock (read-only, non-rotating).

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

- After a refresh, `data/session.json` reflects the rotated cookie; a subsequent run
  (`sync` or `scores`) reuses it without re-login.
- `resolve_session_cookie` prefers `data/session.json` over `$AIMLAB_SESSION`/`.env`; a corrupt
  state file is ignored with a warning, not a crash.
- `voltmeter login` deletes `data/session.json` (rotation reset), so a stale last-link cannot
  shadow the freshly captured cookie.
- Survives ≥2 consecutive refresh cycles unattended (sole consumer).
- A `--no-follow-rotation`-style path still fails after the first refresh (proves the fix is
  what's responsible) — encoded as a regression test with a mocked session endpoint that rotates
  `Set-Cookie`.
- Crash between refresh and persist does not corrupt the state file (atomic temp-file +
  `os.replace`); a half-write is impossible.
- Concurrent rotating runs are serialized by `data/session.lock`; they do not fork the chain, and
  a stale lock (dead PID) is reclaimed rather than wedging the tool.
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
