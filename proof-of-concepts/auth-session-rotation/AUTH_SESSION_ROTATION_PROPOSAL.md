# Proposal: Persist the rotating Aim Lab session cookie (avoid forced re-login)

**Status:** Proposal — **ready to implement.** The run-history pipeline build is complete
(M1–M6b merged) and the go/no-go is resolved (see **Decisions**); the one remaining empirical
gate is the `--no-follow-rotation` control run (see **Open questions**). Until then the
workaround is "re-login when it breaks."
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
- **Blocking gate remaining:** the `--no-follow-rotation` control run (see *Open questions*).
  Everything else is design reconciliation + build.

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
- **Design reconciliation owed:** `docs/RUN_HISTORY_ARCHITECTURE.md` **§4 / §8.4 / §17** currently
  classify `RefreshAccessTokenError` as **terminal** ("the only fix is a fresh `login`") and call
  the cause "not understood." This proposal overturns both — they must be rewritten to
  "recoverable by persisting the rotated cookie." Do this *after* the control run (Open questions)
  confirms the mechanism.

---

## Open questions / verification still owed

- **`--no-follow-rotation` control run — the one blocking gate.** The captured log proves the
  *positive* direction (following rotation sustains the session); the control proves the bug
  directly and doubles as the regression test. **Procedure** — sole consumer (no `aimlabs.com`
  browser tab, no concurrent `sync`/`scores`), disable system sleep, run from the **repo root**
  (where `.env` lives):
  ```
  voltmeter login        # fresh cookie (unspent refresh token)
  python proof-of-concepts/auth-session-rotation/_monitor_session.py \
      --no-follow-rotation --interval 60 --max-min 90
  ```
  **Expected:** `token=Y` for ~0–60 min → first refresh at ~60 min still `token=Y` (RT0's one
  valid use) → next poll (~61 min) re-sends spent RT0 → `RefreshAccessTokenError`, `token=N`,
  monitor exits. Dying ~1 poll after the ~60-min mark (vs. the treatment run sailing past it)
  *is* the proof, and is what the regression test encodes (mock the session endpoint: first
  refresh returns a rotated cookie + token; re-presenting the old cookie returns
  `RefreshAccessTokenError`). If it fails in the first poll or two (well before 60 min), the
  "fresh" cookie wasn't fresh — re-login and retry.
- **Rolling vs. absolute session lifetime** — deferred, non-blocking (see rule 5).
- **Second-cycle confirmation** — fold into a long follow-rotation run (`--max-min 150` crosses
  the second ~60-min refresh boundary, ~04:47 in the captured log); hardening only, not
  load-bearing.

---

## Acceptance criteria (when implemented)

- After a refresh, the **token-state file** reflects the rotated cookie; a subsequent run
  (`sync` or `scores`) reuses it without re-login.
- Survives ≥2 consecutive refresh cycles unattended (sole consumer).
- A `--no-follow-rotation`-style path still fails after the first refresh (proves the fix is
  what's responsible) — encoded as a regression test with a mocked session endpoint.
- Crash between refresh and persist does not corrupt the state file (atomic temp-file +
  `os.replace`); a half-write is impossible.
- Concurrent runs are serialized by the lock; they do not fork the chain.
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
