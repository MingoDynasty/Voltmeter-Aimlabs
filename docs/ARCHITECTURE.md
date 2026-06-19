# `aimlab_history.py` — architecture & handoff

> **Status (M6b):** the `aimlab_history.py` proof-of-concept this document was written against has
> been **retired**. Its behavior now lives in the package — login/session capture in
> `aimlabs_auth.py` (`login_and_capture`), history pagination/parsing in `aimlabs_history.py` +
> `history_sync.py`, and the `plays_agg` contamination check in
> `aimlabs_history.fetch_practice_contamination_count`. This remains useful background for the
> session→bearer model and embedded-browser capture, but the rotating-session persistence design in
> [`AUTH_SESSION_ROTATION.md`](AUTH_SESSION_ROTATION.md) supersedes the old
> `RefreshAccessTokenError` terminal-state framing.

> Self-contained handoff for an agent (or developer) with **no prior context**.
> Explains what the script does, how data flows through it, and — in depth — the
> authentication problem, why it exists, and how the script solves it. Assumes
> you will **build on this POC**, so it also flags the load-bearing assumptions
> and the obvious next steps.

---

## 1. What this is and why it exists

`aimlab_history.py` pulls the **full per-play history** for a single
Voltaic *VALORANT × Aimlabs* benchmark scenario from the **Aimlabs GraphQL API**,
paginates the entire history, and summarizes the score distribution
(trimmed mean, rolling median/max, PB, cold-run ordering).

**Why per-play history rather than just a personal best (PB):** a PB is a single
peak and a poor estimate of *current ability*. Per-play history yields honest,
reproducible signal — a rolling median of recent runs, a trimmed mean that
ignores outliers, and the cold-run trend — which is what you actually want when
tracking progress against benchmark thresholds.

**The larger goal it's a step toward:** an **evxl-style rank-progress tracker for
Voltaic's VALORANT × Aimlabs benchmarks**. [evxl.app](https://evxl.app) does this
for *KovaaKs*; nothing comparable exists for *Aimlabs*. This script is the
data-fetching layer for that gap.

**Design constraint: local-only.** This runs on the user's own machine against
the user's own account. It deliberately does **not** collect or store anyone
else's credentials. (See §8 for why that constraint matters and how a future
hosted version would preserve it.)

---

## 2. The end-to-end flow (the one-screen mental model)

```
                       ┌─────────────────────────────────────────────┐
                       │            ONE-TIME / OCCASIONAL             │
                       │                                              │
  user logs in   ─────▶│  aimlabs.com login (real browser UI:         │
  (real Aim Lab        │  handles MFA / captcha natively)             │
   credentials)        │            │                                 │
                       │            ▼                                 │
                       │  __Secure-next-auth.session-token  cookie    │
                       │  (~30-day reusable credential) ──▶ .env       │
                       └─────────────────────────────────────────────┘
                                    │  AIMLAB_SESSION
                                    ▼
        ┌───────────────────────── PER RUN ─────────────────────────┐
        │                                                            │
        │  GET aimlabs.com/api/auth/session   (cookie-authenticated) │
        │            │                                               │
        │            ▼                                               │
        │  { accessToken: "eyJ…" }   ← fresh ~1h bearer, minted by   │
        │            │                 aimlabs.com's backend          │
        │            ▼                                               │
        │  POST api.aimlab.gg/graphql   (Authorization: Bearer …)    │
        │  query taskHistory → Relay-paginated plays                 │
        │            │                                               │
        │            ▼                                               │
        │  parse nodes → compute stats → print report / --json       │
        └────────────────────────────────────────────────────────────┘
```

Two credentials, two lifetimes: the **session cookie** is the durable, reusable
one (~30 days); the **bearer token** is ephemeral (~1 hour) and re-minted on
every run. The script never asks the user to manage the bearer — it derives a
fresh one from the cookie each time.

---

## 3. The authentication problem (why auth is needed at all)

The target query, `taskHistory` (`aimlabProfile.plays`), is **account-scoped and
protected**. Called anonymously it returns:

```json
{"errors":[{"message":"Client must be authenticated to perform this action.",
  "extensions":{"code":"UNAUTHENTICATED","statusCode":401}}],
 "data":{"aimlabProfile":null}}
```

Established findings that constrain the solution (don't re-derive these):

1. **PBs are public, history is not.** The `LeaderboardEntry` query (one PB per
   scenario) works **anonymously** — validated against the Voltaic app exactly.
   Only the per-play history needs auth.
2. **The Voltaic app can't help.** app.voltaic.gg calls `api.aimlab.gg`
   **cross-site with no `Authorization` and no `Cookie`** — so it is *itself*
   401'd on history (which is why it never shows per-play history). There is no
   token to copy from Voltaic.
3. **No public/developer Aimlabs API.** Auth is centralized at `auth.aimlab.gg`,
   shared across aimlabs.com, the PC/mobile apps, and the VALORANT StatsCenter.
   No API program, no documented token endpoint to call.
4. **A raw HTTP client is not a browser.** SameSite cookie rules don't apply to
   a Python `requests`/`urllib` client, so if the credential is a cookie, the
   script can send it cross-origin and Aimlabs will honor it.

**Conclusion:** the only place a working credential exists is the **user's own
logged-in first-party Aimlabs session**.

---

## 4. The authentication solution (how it works)

### 4.1 The architecture insight

aimlabs.com is a **Next.js app using NextAuth**, authenticating via a standard
**OAuth 2.0 Authorization Code flow (OIDC)** against `auth.aimlab.gg`:

```
aimlabs.com  ──▶  auth.aimlab.gg/oauth/auth   (issues a one-time ?code=…)
            ◀──  redirect to aimlabs.com/api/auth/callback?code=…
aimlabs.com backend  ──▶  token endpoint   (code → access + refresh token)
```

The crucial detail: the **`code → token` exchange happens server-side on
aimlabs.com's backend**, not in the browser. That exchange uses a **client
secret** held on their server, so it **cannot be replayed** by us — the secret
isn't on the user's machine, and the authorization `code` is single-use and
already spent by the time you'd see it. So we cannot mint our own tokens by
imitating the OAuth client.

### 4.2 What we exploit instead

aimlabs.com holds the OAuth tokens **server-side**, then re-exposes the current
access token to **its own frontend** through a NextAuth route:

```
GET https://aimlabs.com/api/auth/session      (authenticated by the session cookie)

{ "user": { "email": "…", "id": "…" },
  "expires": "2026-07-06T…",                  ← session lifetime (~30 days)
  "accessToken": "eyJ…",                       ← fresh ~1h bearer
  "accessTokenExpiresAt": 1780736527 }
```

So the reusable credential is the **NextAuth session cookie**
(`__Secure-next-auth.session-token`), not a bearer we mint ourselves. With that
cookie we replay `/api/auth/session` to get a **fresh bearer on demand**: when
the bearer nears expiry, aimlabs.com's backend silently refreshes it (using the
`offline_access` refresh token it holds) and returns a new one. We never touch
the token endpoint, the client secret, the refresh token, or the password.

**Why this is the least-invasive own-account path** (not a hack): we use the site
exactly as its own frontend does — for the user's own account, with no password
scripting (which would break on CSRF, device checks, MFA), no client secret we
don't own, no credential theft, no third-party data access, and **no captcha**
(the only real login happens in a real browser, where Aim Lab handles MFA/captcha
itself). It does, however, rely on **undocumented frontend behavior**
(`/api/auth/session` exposing `accessToken`) that may change — see §9.1.

### 4.3 Token vs. cookie — lifetimes

| Credential | Where it lives | Lifetime | Role |
|---|---|---|---|
| Access token (bearer) | minted per run | **~1 hour** | sent to `api.aimlab.gg/graphql` |
| Session cookie | `.env` (`AIMLAB_SESSION`) | **~30 days** (rolling) | mints fresh bearers via `/api/auth/session` |

The session cookie is therefore the **most valuable secret** in the system — it
is effectively "logged in as the user" for a month. Treat it like a password
(see §8).

---

## 5. Credential resolution (precedence)

On a normal run the script resolves auth in this order (see
`resolve_authorization`):

1. **Explicit header** — `--header "Authorization: Bearer …"` always wins and
   bypasses everything below. (Manual override / debugging.)
2. **`AIMLAB_SESSION`** (preferred) — a session cookie. The script calls
   `/api/auth/session` (`fetch_bearer_from_session`) to mint a fresh bearer.
3. **`AIMLAB_TOKEN`** (fallback) — a raw bearer pasted in directly. Lasts ~1h,
   then 401s. Used only if the session route fails or no session is set.

> **Relationship to the production pipeline** (`RUN_HISTORY_ARCHITECTURE.md`, which
> depends on this doc): production `sync` uses **only the session-cookie path (2)** —
> it must re-mint bearers across long backfills, which a raw bearer can't do. The
> **raw-bearer options (1, 3) are POC/debug conveniences, not part of production
> `sync`**, and the production CLI never accepts a literal secret on the command line
> (it uses `$AIMLAB_SESSION` / `.env` / `--session-file`).

`resolve_authorization` returns a `(authorization, reason)` pair where `reason`
is `ok` / `expired` (a session cookie exists but the route rejected it) /
`absent` (no credential at all). The `reason` drives the login UX below.

Credentials are read from the environment, optionally seeded from a **`.env`** file by a
tiny zero-dependency loader (`load_dotenv`) that never overrides an already-exported
variable. (`.env` is gitignored — see §8.)

---

## 6. The login UX (removing the manual cookie hunt)

Capturing a cookie by hand (DevTools → Cookies → copy) works but is awkward,
especially for non-technical users. Two mechanisms remove that friction.

### 6.1 `--login` (guided capture)

`python aimlab_history.py --login` opens an **embedded browser window**
(`pywebview`, an optional dependency) pointed at a protected aimlabs.com page,
which forces the **real Aim Lab login**. After the user logs in normally
(MFA/captcha handled natively by Aim Lab), a background poller reads the
`__Secure-next-auth.session-token` cookie from the **webview's native cookie
store** — which includes **`httpOnly`** cookies, unlike `document.cookie` or a
bookmarklet — writes it to `.env` as `AIMLAB_SESSION`, closes the window, and
validates it (printing "verified login as `<email>`").

Implementation notes that bit us / matter:
- **Cookie shape varies by backend.** pywebview's `get_cookies()` returns
  `http.cookies.SimpleCookie` objects on Windows (WebView2/EdgeChromium), but
  `http.cookiejar.Cookie` on others. `_iter_cookie_pairs` normalizes all shapes
  (SimpleCookie, Morsel, cookiejar.Cookie, plain dict). **This was the cause of
  an early "logged in but nothing captured" bug.**
- **Chunked / custom-named cookies** are handled: if NextAuth splits the token
  across `…session-token.0/.1`, they're joined into one cookie string; custom
  names are stored as `name=value` and sent verbatim.
- The credential **never leaves the user's machine**.

### 6.2 Auto-login (so the user can't "forget")

> **Pipeline divergence (M6b).** This auto-login UX — and the `--no-login` flag below —
> describes the **retired `aimlab_history.py` PoC**, not the shipped `voltmeter` CLI. The
> productized pipeline made the **opposite** choice: `voltmeter sync` never opens a login window
> and has **no `--no-login` flag** — unattended/scheduled is the *default* (see
> `RUN_HISTORY_ARCHITECTURE.md` §4 and decision 23). Don't apply the `--no-login` guidance here to
> `voltmeter`.

A normal run with a **missing or expired** credential will **auto-open the login
window** — the user never needs to know `--login` exists. Guards:
- Fires only when a **desktop GUI is likely available** (`_gui_likely_available`:
  always on Windows/macOS; on Linux requires `DISPLAY`/`WAYLAND_DISPLAY`). This
  was switched **away from `stdin.isatty()`**, which is false under `uv run` /
  IDE consoles and wrongly suppressed it.
- Suppressed by **`--no-login`** or a headless environment: prints how to fix it
  and exits non-zero instead of popping a window. **Use `--no-login` for cron /
  scheduled / unattended runs** so a GUI never tries to open on a server.

---

## 7. The fetch / parse / summarize pipeline

Once an `Authorization` header is in hand:

- **Query** (`QUERY`): `taskHistory($filter, $first, $anthicId, $username,
  $after)` → `aimlabProfile.plays` (a Relay connection).
- **Pagination** (`fetch_history`): follows the Relay cursor —
  `pageInfo.hasNextPage` + `endCursor` → next `after` — until `max_plays` or the
  end. Prefers `requests` if installed, falls back to stdlib `urllib`.
- **Parse** (`_node_to_play`): maps each GraphQL node to a flat dict
  (`score`, `ended_at`, `performance_scores`, etc.).
- **Summarize** (`summarize`): `pb` (max), 10% `trimmed_mean`, `mean`/`median`,
  `stdev`, and rolling windows over the most-recent plays by `ended_at` —
  `roll_median_last_10` and `roll_max_last_25` (window sizes `ROLL_MEDIAN_N` /
  `ROLL_MAX_N`). Cold-run signal comes from the chronological ordering.
- **Output**: a human-readable report (`format_report`) or `--json` / `--out`
  for downstream tooling (e.g. a dashboard).

### Output enrichment
- The report header shows **scenario name + difficulty**, resolved by
  `resolve_task_info`: `--name`/`--difficulty` override → `TASK_INFO` table →
  fallback (name derived from the taskId's 3rd segment, difficulty `unknown`).
  **Difficulty is not encoded in the taskId**, so it must live in `TASK_INFO` or
  be passed explicitly.
- The per-play table shows the `PERF_COLUMNS` metrics
  (`accTotal`, `hitsTotal`, `shotsTotal`) pulled from each play's
  `performanceScores` (`_perf_scores` handles dict or JSON-string forms). If
  those keys aren't present, the table prints the **actual available keys** so a
  mismatch is self-diagnosing.

---

## 8. Security model (load-bearing — this is headed for a public repo)

- **Local-only by design.** The script runs on the user's machine against their
  own account. The session cookie and bearer **never leave the machine**.
- **Never commit secrets.** They live in `.env`, and `_write_env_var` `chmod 0600`s the
  file on POSIX. The repo-root `.gitignore` **already blocks** `.env`, `.env.*`, `*.token`,
  `*.cookie`, `*_history.json`, `config.toml`, and the local data store (`data/`, `*.db`,
  `*.sqlite*`) — done when the POC was committed. Still: never commit a real populated
  `.env`, token/cookie, database, or history dump.
- **The session cookie is the crown jewel** — a ~30-day "logged in as you"
  credential. If it leaks, the remedy is to log out of aimlabs.com (invalidates
  the session) and re-capture.
- **Why a *hosted, multi-user* version is a liability** (and intentionally out of
  scope for this POC): it would mean collecting **other people's** live 30-day
  session credentials on your server. The clean OAuth alternative is closed (no
  developer program / client registration). The right future architecture is a
  **local companion**: the capture + fetch run on each user's machine, and only
  **derived stats** (rolling median, etc.) are sent to a server — the credential
  stays local. Nothing in this POC is throwaway for that path.
- **Terms of service:** automating your *own* account is the benign case; a
  multi-user product amplifies the ToS question — review Aimlabs' ToS before
  going there.

---

## 9. Known fragilities & assumptions (read before building on this)

1. **`/api/auth/session` exposing `accessToken` is an undocumented frontend
   implementation detail**, not a public API contract. It works today. If
   aimlabs.com reconfigures NextAuth to stop surfacing the token, the script
   **fails loud** (clear "no accessToken … keys seen: […]" error) and you fall
   back to manual bearer capture.
   - **Observed live (2026-06-06, refined 2026-06-17):** the route can return HTTP 200 with
     `{ accessTokenError: "RefreshAccessTokenError", user, expires }` and **no**
     `accessToken` while the session cookie itself is still valid (`expires` a month out).
     The package now persists the rotated `Set-Cookie` session link after successful mints, so
     normal bearer refresh is recoverable. A remaining `RefreshAccessTokenError` means the chain
     is already gone/forked/corrupt and should surface "run `voltmeter login`," not retry-loop.
2. **pywebview `httpOnly` cookie reads are backend-dependent.** Confirmed to work
   on Windows (WebView2). Verify on any new platform; if a backend hides the
   `httpOnly` cookie, `--login` says so explicitly and manual capture remains the
   fallback.
3. **Token lifetime is ~1h, session ~30 days — but the two are decoupled.** The
   cookie's *identity* lifetime (`expires`, ~30d) and its *token-minting* ability
   (the `offline_access` refresh token) are independent, and the refresh token can
   die first (see §9.1 above). So "session valid" (cookie not past `expires`) does
   **not** guarantee "can mint a bearer." For periodic use a fresh bearer per run is
   plenty; the package sustains this by persisting the rotated session cookie returned by
   successful mints. Re-login remains the fallback for true expiry or residual
   `RefreshAccessTokenError`.
4. **Sandbox/CI cannot reach `api.aimlab.gg` or `aimlabs.com`.** All live
   validation is local. Logic is covered by mock tests (see §11).
5. **`TASK_INFO` is seeded with one scenario only** (Adjustshot Intermediate).
   Other scenarios fall back to a truncated name + `unknown` difficulty until the
   table is filled in.

---

## 10. Identifiers & query specifics (quick reference)

- **Endpoint:** `POST https://api.aimlab.gg/graphql`
- **Session route:** `GET https://aimlabs.com/api/auth/session`
- **`anthicId`** (the tracked account, semi-public — appears in the anonymous
  leaderboard query, not a secret): `A32D4D127BA6094E`
- **`mode` / `taskMode`:** `42`
- **Example `taskId`** (Adjustshot, Intermediate):
  `CsLevel.Lowgravity56.VT Adjus.RTUQMP`
- **Default cookie name:** `__Secure-next-auth.session-token`
- **`taskHistory` node fields fetched:** `id`, `appId`, `endedAt`, `score`,
  `manifest { playDuration pauseDuration }`, `performanceScores`,
  `gridshieldStatus`.

---

## 11. Code map (where to look)

| Area | Functions |
|---|---|
| Secrets / `.env` | `load_dotenv`, `_write_env_var` |
| Session → bearer | `_session_cookie_header`, `fetch_session_json`, `fetch_bearer_from_session` |
| Login capture | `_iter_cookie_pairs`, `_cookie_names`, `_extract_session_cookie`, `login_and_capture`, `LOGIN_START_URL` |
| Auth resolution | `resolve_authorization` (returns `(auth, reason)`), `_gui_likely_available`, `resolve_auth_or_login` |
| Fetch / paginate | `_post_json`, `_build_payload`, `_parse_page`, `fetch_history` |
| Stats | `_trimmed_mean`, `summarize` |
| Output | `resolve_task_info`, `_perf_scores`, `_fmt_metric`, `format_report`, `TASK_INFO`, `PERF_COLUMNS` |
| CLI | `main` |

**Test suites (mock-only, no network):**
- `test_auth.py` — session-route exchange, `.env` loading, resolution precedence,
  `expired`/`absent` reasons.
- `test_login.py` — cookie-shape extraction (SimpleCookie/cookiejar/chunked),
  `.env` write/replace/round-trip, graceful degradation without pywebview.
- `test_autologin.py` — the auto-login decision matrix (GUI/headless/`--no-login`,
  absent vs expired, login aborted).

---

## 12. Sensible next steps (suggested, not required)

- **Fill in `TASK_INFO`** for all 21 VT VALORANT benchmark scenarios across
  Novice/Intermediate/Advanced (the per-scenario PB fetcher likely already has a
  taskId→name list to reuse).
- **`plays_agg`**: aggregate stats query, **auth untested** but user-play-scoped,
  so the same session credential almost certainly unblocks it.
- **`accTotal` formatting**: if it's a 0–1 fraction, optionally render as a
  percentage in the table.
- **Dashboard integration**: pipe `--json` (rolling median / trimmed mean) into
  the personal analytics dashboard to replace stale PB-based snapshots.
- **Refresh-token flow**: only if unattended runs across the 1h boundary become a
  real need — otherwise auto re-login on session expiry suffices.
- **Hosted version**: build the **local-companion** architecture from §8, never a
  server that holds users' session cookies.
