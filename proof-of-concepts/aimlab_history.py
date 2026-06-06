#!/usr/bin/env python3
"""
aimlab_history.py
=================
Pull the full *play history* for a single Voltaic/Aimlabs scenario from the
Aimlabs GraphQL API (the `taskHistory` query app.voltaic.gg uses), paginating
through the Relay cursor, and summarize the distribution.

Where aimlab_scores.py gives you one PB per scenario, this gives you every
play -- so you get trimmed mean, rolling median/max, and cold-run signal
instead of a single peak.

Authentication
--------------
`taskHistory` (aimlabProfile.plays) requires auth. Three ways to supply it,
in priority order:

  1. --header "Authorization: Bearer eyJ..."   (explicit; always wins)
  2. AIMLAB_SESSION  -> a logged-in aimlabs.com NextAuth session cookie.
     The script GETs https://aimlabs.com/api/auth/session, which returns a
     FRESH access token (the site refreshes it server-side via offline_access).
     This is the reusable path: the session cookie lasts ~30 days and mints
     hour-long bearers on demand. PREFERRED.
  3. AIMLAB_TOKEN    -> a raw access token (Bearer). Lasts ~1h, then 401s.
     Used as a fallback if the session route fails.

Credentials are read from the environment (optionally seeded from a gitignored
.env file). Never hardcode them; the session cookie in particular is a ~30-day
"logged in as you" credential -- treat it like a password.

Usage (CLI)
-----------
  # one-time setup: opens an Aim Lab login window, captures the session
  # cookie on success, and writes it to .env (needs: pip install pywebview)
  python aimlab_history.py --login

  # preferred: reusable session cookie
  export AIMLAB_SESSION="<__Secure-next-auth.session-token value>"
  python aimlab_history.py

  # or a raw hour-long bearer
  export AIMLAB_TOKEN="eyJ..."
  python aimlab_history.py

  # or pass a bearer explicitly (overrides the above)
  python aimlab_history.py --header "Authorization: Bearer eyJ..."

  python aimlab_history.py --task-id "CsLevel.Lowgravity56.VT Float.RSM61S"
  python aimlab_history.py --mode 42 --max 200
  python aimlab_history.py --json --out adjust_history.json
  python aimlab_history.py --username MingoDynasty

Usage (import)
--------------
  from aimlab_history import fetch_history, summarize, fetch_bearer_from_session
  plays, total = fetch_history(task_id="CsLevel.Lowgravity56.VT Adjus.RTUQMP")
  stats = summarize([p["score"] for p in plays], plays)

Dependencies
------------
Uses `requests` if installed, else falls back to stdlib urllib (zero install).
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from http.cookies import Morsel
from typing import Any, Optional

ENDPOINT = "https://api.aimlab.gg/graphql"
SESSION_URL = "https://aimlabs.com/api/auth/session"
DEFAULT_SESSION_COOKIE = "__Secure-next-auth.session-token"
DEFAULT_ANTHIC_ID = "A32D4D127BA6094E"
DEFAULT_TASK_ID = "CsLevel.Lowgravity56.VT Adjus.RTUQMP"  # Adjustshot (Intermediate)
DEFAULT_MODE = 42

# Rolling-window sizes (your stated preferences).
ROLL_MEDIAN_N = 10
ROLL_MAX_N = 25

# taskId -> (display name, difficulty). Seeded with the default scenario; extend
# this with the other VT VALORANT benchmark taskIds (or pass --name/--difficulty
# per run). Difficulty is NOT derivable from the taskId, so it must live here.
TASK_INFO: dict[str, tuple[str, str]] = {
    "CsLevel.Lowgravity56.VT Adjus.RTUQMP": ("VT Adjustshot VALORANT", "Intermediate"),
}

# Performance-score keys to show in the per-play table, in column order.
PERF_COLUMNS = ("accTotal", "hitsTotal", "shotsTotal")

QUERY = """
query taskHistory($filter: PlayFilterInput, $first: Int, $anthicId: String, $username: String, $after: String) {
  aimlabProfile(anthicId: $anthicId, username: $username) {
    plays(filter: $filter, first: $first, after: $after) {
      pageInfo {
        hasNextPage
        startCursor
        endCursor
      }
      totalCount
      edges {
        node {
          id
          appId
          endedAt
          score
          manifest {
            playDuration
            pauseDuration
          }
          performanceScores
          gridshieldStatus
        }
      }
    }
  }
}
"""

BASE_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://app.voltaic.gg",
    "Referer": "https://app.voltaic.gg/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
}


# ---------------------------------------------------------------------------
# Secrets: minimal .env loader (zero deps). Does NOT override existing env.
# ---------------------------------------------------------------------------
def load_dotenv(path: str = ".env") -> None:
    """Seed os.environ from a simple KEY=VALUE .env file, if present.

    - Lines that are blank, comments (#), or lack '=' are ignored.
    - Surrounding single/double quotes on the value are stripped.
    - Existing environment variables are NEVER overridden (so an explicit
      `export` always wins over the file).
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw_lines = f.readlines()
    except FileNotFoundError:
        return
    for raw in raw_lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val


# ---------------------------------------------------------------------------
# Auth: exchange an aimlabs.com session cookie for a fresh access token.
# ---------------------------------------------------------------------------
def _session_cookie_header(session: str) -> str:
    """Turn an AIMLAB_SESSION value into a Cookie header string.

    `session` may be the bare __Secure-next-auth.session-token value, or a full
    cookie string (chunked tokens / custom name), which is used verbatim.
    """
    looks_like_full_cookie = ("session-token" in session) or ("; " in session)
    return session if looks_like_full_cookie else f"{DEFAULT_SESSION_COOKIE}={session}"


def fetch_session_json(session: str, timeout: float = 20.0) -> dict:
    """GET aimlabs.com/api/auth/session with the cookie; return the JSON dict.

    Raises RuntimeError on HTTP error or non-JSON body.
    """
    req = urllib.request.Request(
        SESSION_URL,
        headers={
            "Cookie": _session_cookie_header(session),
            "Accept": "application/json",
            "User-Agent": BASE_HEADERS["User-Agent"],
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"session route HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}"
        ) from e
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"session route request failed: {e}") from e

    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError(f"session route returned non-JSON: {body[:200]}")


def fetch_bearer_from_session(session: str, timeout: float = 20.0) -> str:
    """Exchange an aimlabs.com NextAuth session cookie for a fresh access token.

    `session` may be EITHER:
      - the bare value of the __Secure-next-auth.session-token cookie, or
      - a full Cookie header string (e.g. chunked tokens
        "...session-token.0=...; ...session-token.1=..." or a custom name),
        which is sent verbatim.

    Returns the access token string. Raises RuntimeError if the session route
    doesn't return one (cookie expired/invalid, or the site stopped exposing
    accessToken to its frontend).
    """
    data = fetch_session_json(session, timeout=timeout)

    token = data.get("accessToken")
    if not token:
        raise RuntimeError(
            "no accessToken in /api/auth/session response -- session cookie is "
            "likely expired/invalid (re-capture from the browser), or the site "
            "stopped exposing accessToken to its frontend. "
            f"keys seen: {sorted(data.keys())}"
        )

    exp = data.get("accessTokenExpiresAt")
    if isinstance(exp, (int, float)) and exp < time.time():
        print("warning: returned token is already past accessTokenExpiresAt; "
              "the route may not be refreshing as expected.", file=sys.stderr)
    return token


# ---------------------------------------------------------------------------
# Login capture: open a real Aim Lab login window, read the session cookie.
#
# This drives the *real* aimlabs.com login UI in an embedded browser, so MFA
# and any captcha are handled natively by Aim Lab -- we never automate around
# them. After the user logs in, we read the session cookie from the webview's
# native cookie store (which includes httpOnly cookies, unlike document.cookie)
# and write it to .env. The credential never leaves the user's machine.
# ---------------------------------------------------------------------------
LOGIN_START_URL = "https://aimlabs.com/account/info"  # protected -> forces login


def _iter_cookie_pairs(cookies: Any):
    """Yield (name, value) from whatever shape a webview backend returns.

    pywebview's get_cookies() return type varies by backend/version:
      - EdgeChromium/WebView2 (Windows) and others -> list[http.cookies.SimpleCookie]
        (each a dict of name -> Morsel)
      - some backends/versions -> list[http.cookiejar.Cookie] (.name / .value)
      - occasionally a bare Morsel, or a plain {name: value} dict
    Normalize all of them here so the extractor doesn't care.
    """
    for c in cookies or []:
        if isinstance(c, Morsel):
            yield c.key, c.value
        elif isinstance(c, dict):  # SimpleCookie (name -> Morsel) or plain dict
            for k, v in c.items():
                if isinstance(v, Morsel):
                    yield v.key, v.value
                else:
                    yield k, v
        else:  # http.cookiejar.Cookie or similar object with .name/.value
            name = getattr(c, "name", None)
            value = getattr(c, "value", None)
            if name is not None:
                yield name, value


def _cookie_names(cookies: Any) -> list[str]:
    """Sorted, de-duplicated cookie names (no values) -- safe for diagnostics."""
    return sorted({n for n, _ in _iter_cookie_pairs(cookies) if n})


def _extract_session_cookie(cookies: Any) -> Optional[str]:
    """From a list of cookie objects, build the AIMLAB_SESSION value to store.

    Handles:
      - single default-named cookie  -> store bare value (script wraps it)
      - single custom-named cookie   -> store "name=value" (sent verbatim)
      - chunked cookies (.0/.1/...)  -> store joined "n0=v0; n1=v1" string
    Returns None if no session-token cookie is present.
    """
    chunks: dict[str, str] = {}
    single: Optional[tuple[str, str]] = None
    for name, value in _iter_cookie_pairs(cookies):
        if not name or value is None or "session-token" not in name:
            continue
        suffix = name.split("session-token", 1)[1]  # "" or ".0", ".1", ...
        if suffix.startswith(".") and suffix[1:].isdigit():
            chunks[name] = value
        else:
            single = (name, value)
    if chunks:
        ordered = sorted(chunks.items(), key=lambda kv: int(kv[0].rsplit(".", 1)[-1]))
        return "; ".join(f"{n}={v}" for n, v in ordered)
    if single:
        name, value = single
        return value if name == DEFAULT_SESSION_COOKIE else f"{name}={value}"
    return None


def _write_env_var(path: str, key: str, value: str) -> None:
    """Set KEY="value" in a .env file: replace an existing line or append.

    Other lines are preserved. On POSIX the file is chmod'd 0600 since it now
    holds a live credential.
    """
    line = f'{key}="{value}"\n'
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []
    replaced = False
    for i, existing in enumerate(lines):
        stripped = existing.lstrip()
        if stripped.startswith(f"{key}=") and not stripped.startswith("#"):
            lines[i] = line
            replaced = True
            break
    if not replaced:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(line)
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    try:
        os.chmod(path, 0o600)  # best-effort; no-op semantics on Windows
    except OSError:
        pass


def login_and_capture(
    env_path: str = ".env",
    timeout: float = 300.0,
    start_url: str = LOGIN_START_URL,
) -> Optional[str]:
    """Open an Aim Lab login window; on success, write AIMLAB_SESSION to .env.

    Returns the captured session value, or None (dependency missing, timeout,
    or window closed before login). Requires `pip install pywebview`.
    """
    try:
        import webview  # type: ignore
    except ImportError:
        print(
            "auth: --login needs the optional dependency 'pywebview'.\n"
            "      install it with:  pip install pywebview\n"
            "      (on Linux you may also need a webview backend, e.g.\n"
            "       PyGObject + WebKit2GTK, or PyQt/QtWebEngine)\n"
            "      Until then, capture the cookie manually: log into aimlabs.com,\n"
            "      DevTools -> Storage/Application -> Cookies -> aimlabs.com ->\n"
            "      copy __Secure-next-auth.session-token into .env as AIMLAB_SESSION.",
            file=sys.stderr,
        )
        return None

    captured: dict[str, Optional[str]] = {"value": None}
    last_seen: dict[str, list[str]] = {"names": []}

    def _poll(window: Any) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                cookies = window.get_cookies()
            except Exception as e:  # noqa: BLE001 -- backend may not be ready yet
                cookies = None
                if last_seen["names"] != ["<error>"]:
                    print(f"auth: (get_cookies not ready yet: {e})", file=sys.stderr)
                    last_seen["names"] = ["<error>"]
            value = _extract_session_cookie(cookies) if cookies else None
            # Diagnostic: when the set of visible cookie names changes, log the
            # names (NOT values) so we can see whether/when the session token
            # appears and under what name.
            if cookies is not None:
                names = _cookie_names(cookies)
                if names != last_seen["names"]:
                    last_seen["names"] = names
                    print(f"auth: cookies visible now: {names or '(none)'}",
                          file=sys.stderr)
            if value:
                captured["value"] = value
                break
            time.sleep(1.0)
        try:
            window.destroy()
        except Exception:  # noqa: BLE001
            pass

    print("auth: opening Aim Lab login window -- log in normally, then it closes "
          "automatically once your session is captured.", file=sys.stderr)
    window = webview.create_window("Log in to Aim Lab", start_url)
    webview.start(_poll, window)

    value = captured["value"]
    if not value:
        seen = last_seen["names"]
        hint = ""
        if seen and seen != ["<error>"] and not any("session-token" in n for n in seen):
            hint = (" The login cookies were visible but none contained "
                    "'session-token' -- this backend may be hiding the httpOnly "
                    "session cookie. Last seen: " + ", ".join(seen) + ".")
        print("auth: no session captured (window closed or timed out before "
              "login completed)." + hint + " Nothing written.", file=sys.stderr)
        return None

    _write_env_var(env_path, "AIMLAB_SESSION", value)
    print(f"auth: captured session cookie -> wrote AIMLAB_SESSION to {env_path}",
          file=sys.stderr)

    # Validate + show who we're logged in as (best-effort; needs network).
    try:
        info = fetch_session_json(value)
        email = (info.get("user") or {}).get("email")
        expires = info.get("expires")
        who = f" as {email}" if email else ""
        until = f" (session valid until {expires})" if expires else ""
        print(f"auth: verified login{who}{until}.", file=sys.stderr)
    except RuntimeError as e:
        print(f"auth: captured cookie but could not verify it yet ({e}). "
              "Try a normal run.", file=sys.stderr)
    return value


# ---------------------------------------------------------------------------
# HTTP: prefer requests, fall back to urllib.
# ---------------------------------------------------------------------------
def _post_json(url: str, payload: dict, headers: dict, timeout: float) -> tuple[int, str]:
    body = json.dumps(payload).encode("utf-8")
    try:
        import requests  # type: ignore

        resp = requests.post(url, data=body, headers=headers, timeout=timeout)
        return resp.status_code, resp.text
    except ImportError:
        import urllib.error

        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.getcode(), r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")


def _build_payload(
    anthic_id: Optional[str], username: Optional[str],
    task_id: str, mode: int, first: int, after: Optional[str],
) -> dict:
    variables: dict[str, Any] = {
        "filter": {"taskId": task_id, "mode": mode},
        "first": first,
    }
    if anthic_id:
        variables["anthicId"] = anthic_id
    if username:
        variables["username"] = username
    if after:
        variables["after"] = after
    return {"operationName": "taskHistory", "query": QUERY, "variables": variables}


def _node_to_play(node: dict) -> dict:
    manifest = node.get("manifest") or {}
    return {
        "id": node.get("id"),
        "app_id": node.get("appId"),
        "ended_at": node.get("endedAt"),
        "score": node.get("score"),
        "play_duration": manifest.get("playDuration"),
        "pause_duration": manifest.get("pauseDuration"),
        "performance_scores": node.get("performanceScores"),
        "gridshield_status": node.get("gridshieldStatus"),
    }


def _parse_page(body_text: str) -> tuple[Optional[dict], Optional[str]]:
    """Return (plays_block, error). plays_block has edges/pageInfo/totalCount."""
    try:
        payload = json.loads(body_text)
    except json.JSONDecodeError:
        return None, f"non-JSON response: {body_text[:200]}"
    if isinstance(payload, dict) and payload.get("errors"):
        return None, "graphql error: " + json.dumps(payload["errors"])[:300]
    try:
        plays = payload["data"]["aimlabProfile"]["plays"]
    except (KeyError, TypeError):
        return None, f"unexpected shape: {body_text[:200]}"
    if plays is None:
        return None, "no plays block (unknown user/task?)"
    return plays, None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def fetch_history(
    task_id: str = DEFAULT_TASK_ID,
    *,
    anthic_id: Optional[str] = DEFAULT_ANTHIC_ID,
    username: Optional[str] = None,
    mode: int = DEFAULT_MODE,
    first: int = 50,
    max_plays: int = 1000,
    max_pages: int = 100,
    timeout: float = 20.0,
    retries: int = 2,
    delay: float = 0.25,
    extra_headers: Optional[dict] = None,
    verbose: bool = False,
) -> tuple[list[dict], Optional[int]]:
    """Paginate the full play history. Returns (plays, total_count_from_api)."""
    headers = dict(BASE_HEADERS)
    if extra_headers:
        headers.update(extra_headers)

    plays: list[dict] = []
    total_count: Optional[int] = None
    after: Optional[str] = None

    for page in range(max_pages):
        payload = _build_payload(anthic_id, username, task_id, mode, first, after)

        block, err = None, None
        for attempt in range(retries + 1):
            try:
                status, text = _post_json(ENDPOINT, payload, headers, timeout)
            except Exception as e:  # noqa: BLE001
                err = f"request failed: {e}"
            else:
                if status == 200:
                    block, err = _parse_page(text)
                    if block is not None:
                        break
                elif status == 401:
                    err = ("HTTP 401 -- unauthenticated. Supply a credential: "
                           "AIMLAB_SESSION (preferred) or AIMLAB_TOKEN, or "
                           '--header "Authorization: Bearer ...". A token may '
                           "have expired (~1h lifetime); re-fetch via session.")
                elif status == 403:
                    err = ("HTTP 403 -- blocked. host_not_allowed => sandbox egress "
                           "proxy (run locally). Else Aimlabs WAF: add --header cookie.")
                else:
                    err = f"HTTP {status}: {text[:200]}"
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))

        if block is None:
            raise RuntimeError(f"page {page} failed: {err}")

        if total_count is None:
            total_count = block.get("totalCount")
        edges = block.get("edges") or []
        plays.extend(_node_to_play(e["node"]) for e in edges if e.get("node"))

        if verbose:
            print(f"  page {page + 1}: +{len(edges)} plays "
                  f"({len(plays)} total, api totalCount={total_count})", file=sys.stderr)

        page_info = block.get("pageInfo") or {}
        if len(plays) >= max_plays or not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
        if not after:
            break
        if delay:
            time.sleep(delay)

    return plays[:max_plays], total_count


def _trimmed_mean(scores: list[float], proportion: float = 0.10) -> Optional[float]:
    if not scores:
        return None
    s = sorted(scores)
    k = int(len(s) * proportion)
    core = s[k: len(s) - k] if len(s) - 2 * k > 0 else s
    return statistics.mean(core)


def summarize(scores: list[float], plays: Optional[list[dict]] = None) -> dict:
    """Distribution summary. If plays given, rolling windows use ended_at order."""
    scores = [s for s in scores if isinstance(s, (int, float))]
    out: dict[str, Any] = {"n": len(scores)}
    if not scores:
        return out

    out["pb"] = max(scores)
    out["mean"] = round(statistics.mean(scores), 1)
    out["median"] = round(statistics.median(scores), 1)
    tm = _trimmed_mean(scores, 0.10)
    out["trimmed_mean_10pct"] = round(tm, 1) if tm is not None else None
    out["stdev"] = round(statistics.pstdev(scores), 1) if len(scores) > 1 else 0.0

    # Rolling windows over most-recent plays (needs chronology).
    if plays:
        recent = sorted(
            (p for p in plays if isinstance(p.get("score"), (int, float)) and p.get("ended_at")),
            key=lambda p: p["ended_at"], reverse=True,
        )
        recent_scores = [p["score"] for p in recent]
        if recent_scores:
            out["roll_median_last_%d" % ROLL_MEDIAN_N] = round(
                statistics.median(recent_scores[:ROLL_MEDIAN_N]), 1)
            out["roll_max_last_%d" % ROLL_MAX_N] = max(recent_scores[:ROLL_MAX_N])
            out["latest_score"] = recent_scores[0]
            out["latest_date"] = recent[0]["ended_at"][:10]
            out["oldest_date"] = recent[-1]["ended_at"][:10]
    return out


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def resolve_task_info(
    task_id: str,
    name: Optional[str] = None,
    difficulty: Optional[str] = None,
) -> tuple[str, str]:
    """Return (display_name, difficulty) for a taskId.

    Priority: explicit override -> TASK_INFO table -> best-effort fallback.
    The fallback name is the third dotted segment of the taskId (e.g. the
    "VT Adjus" in "CsLevel.Lowgravity56.VT Adjus.RTUQMP"); difficulty falls back
    to "unknown" since it isn't encoded in the taskId.
    """
    known_name, known_diff = TASK_INFO.get(task_id, (None, None))
    name = name or known_name
    difficulty = difficulty or known_diff
    if not name:
        parts = task_id.split(".")
        name = parts[2].strip() if len(parts) >= 3 else task_id
    return name, (difficulty or "unknown")


def _perf_scores(play: dict) -> dict:
    """Return a play's performanceScores as a dict (handles JSON-string form)."""
    raw = play.get("performance_scores")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _fmt_metric(value: Any) -> str:
    """Format a performance metric for the table: ints plain, floats trimmed."""
    if isinstance(value, bool) or value is None:
        return "-"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else f"{value:.3f}".rstrip("0").rstrip(".")
    return str(value)


def format_report(task_id: str, mode: int, plays: list[dict],
                  total_count: Optional[int], show_n: int = 15,
                  name: Optional[str] = None, difficulty: Optional[str] = None) -> str:
    scores = [p["score"] for p in plays]
    stats = summarize(scores, plays)
    code = task_id.rsplit(".", 1)[-1]
    disp_name, disp_diff = resolve_task_info(task_id, name, difficulty)

    lines = [f"{disp_name}  ({disp_diff})",
             f"taskId {task_id}  (code {code}, mode {mode})",
             f"fetched {len(plays)} plays" +
             (f" of {total_count} on record" if total_count is not None else "")]
    if stats.get("n"):
        lines.append("")
        lines.append(f"  PB (max)              {stats['pb']}")
        lines.append(f"  trimmed mean (10%)    {stats.get('trimmed_mean_10pct')}")
        lines.append(f"  rolling median (last {ROLL_MEDIAN_N}) {stats.get('roll_median_last_%d' % ROLL_MEDIAN_N)}")
        lines.append(f"  rolling max (last {ROLL_MAX_N})    {stats.get('roll_max_last_%d' % ROLL_MAX_N)}")
        lines.append(f"  mean / median         {stats['mean']} / {stats['median']}")
        lines.append(f"  stdev                 {stats['stdev']}")
        if stats.get("latest_date"):
            lines.append(f"  date range            {stats.get('oldest_date')} -> {stats.get('latest_date')}")

    # Most-recent plays table.
    recent = sorted((p for p in plays if p.get("ended_at")),
                    key=lambda p: p["ended_at"], reverse=True)[:show_n]
    if recent:
        lines.append("")
        lines.append(f"  most recent {len(recent)} plays:")
        header = f"    {'Date/time':19s}  {'Score':>7s}"
        for col in PERF_COLUMNS:
            header += f"  {col:>10s}"
        lines.append(header)
        any_metric = False
        for p in recent:
            dt = (p.get("ended_at") or "")[:19].replace("T", " ")
            sc = p.get("score")
            sc = f"{sc:.0f}" if isinstance(sc, (int, float)) else "-"
            ps = _perf_scores(p)
            row = f"    {dt:19s}  {sc:>7s}"
            for col in PERF_COLUMNS:
                if col in ps and ps[col] is not None:
                    any_metric = True
                row += f"  {_fmt_metric(ps.get(col)):>10s}"
            lines.append(row)

        # If none of the requested metrics were found, help locate the right keys.
        if not any_metric:
            available = sorted({k for p in recent for k in _perf_scores(p).keys()})
            lines.append("")
            lines.append("  note: none of " + ", ".join(PERF_COLUMNS) +
                         " were found in performanceScores.")
            lines.append("  available keys: " +
                         (", ".join(available) if available else "(performanceScores empty/missing)"))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Auth resolution: pick a credential, return the Authorization header value.
# ---------------------------------------------------------------------------
def resolve_authorization(
    explicit_session: Optional[str],
    explicit_token: Optional[str],
    timeout: float,
) -> tuple[Optional[str], str]:
    """Resolve a credential into a Bearer string.

    Returns (authorization, reason):
      - ("Bearer ...", "ok")        a usable credential was obtained
      - (None, "expired")           a session cookie exists but the route
                                    rejected it (expired/invalid) and no raw
                                    token was available as a fallback
      - (None, "absent")            no credential was supplied at all

    Priority: AIMLAB_SESSION (fetch fresh) -> AIMLAB_TOKEN (raw). The session
    route is tried first; if it fails and a raw token exists, we fall back to it.
    """
    session = explicit_session or os.environ.get("AIMLAB_SESSION")
    token = explicit_token or os.environ.get("AIMLAB_TOKEN")

    session_failed = False
    if session:
        try:
            bearer = fetch_bearer_from_session(session, timeout=timeout)
            print("auth: fetched fresh bearer via aimlabs.com session cookie",
                  file=sys.stderr)
            return f"Bearer {bearer}", "ok"
        except RuntimeError as e:
            print(f"auth: session route failed ({e})", file=sys.stderr)
            session_failed = True
            if token:
                print("auth: falling back to AIMLAB_TOKEN", file=sys.stderr)

    if token:
        return f"Bearer {token}", "ok"
    return None, ("expired" if session_failed else "absent")


def _gui_likely_available() -> bool:
    """Best-effort guess at whether a desktop GUI can open here.

    Used to gate auto-login: we want it to fire for normal desktop use
    (including IDE consoles and `uv run`, where stdin is NOT a TTY) but not on
    a truly headless box where a window would error or hang.

    - Windows / macOS: a desktop session is essentially always present.
    - Linux/other Unix: require a display server (X11 or Wayland).
    """
    if sys.platform.startswith("win") or sys.platform == "darwin":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def resolve_auth_or_login(args: Any) -> Optional[str]:
    """Resolve auth; if none and a desktop GUI is available, auto-launch login.

    Returns a 'Bearer ...' string, or None if the caller should give up
    (a clear message has already been printed in that case).

    Auto-login fires when it's both safe and sensible: a desktop GUI looks
    available (see _gui_likely_available) and --no-login is not set. On a
    headless box, or with --no-login (scripts, cron, the future headless
    companion), it never pops a window -- it prints how to fix it and bails,
    so unattended use stays safe.
    """
    authz, reason = resolve_authorization(args.session, args.token, args.timeout)
    if authz:
        return authz

    why = ("your Aim Lab session has expired" if reason == "expired"
           else "no Aim Lab credential found")

    if args.no_login or not _gui_likely_available():
        suffix = " (--no-login is set)" if args.no_login else ""
        print(f"auth: {why}{suffix}. Set AIMLAB_SESSION (preferred) or "
              "AIMLAB_TOKEN, or run `python aimlab_history.py --login` to "
              "capture a session interactively.", file=sys.stderr)
        return None

    # Interactive + allowed: open the login window, then re-resolve once.
    print(f"auth: {why} -- opening Aim Lab login...", file=sys.stderr)
    value = login_and_capture(env_path=args.env_file, timeout=args.login_timeout)
    if not value:
        return None  # login_and_capture already explained why (e.g. no pywebview)

    authz, _ = resolve_authorization(value, None, args.timeout)
    if authz:
        return authz
    print("auth: captured a session but still couldn't obtain a token. "
          "Try re-running, or capture a credential manually.", file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Pull a scenario's Aimlabs play history.")
    p.add_argument("--task-id", default=DEFAULT_TASK_ID)
    p.add_argument("--mode", type=int, default=DEFAULT_MODE)
    p.add_argument("--name", default=None,
                   help="scenario display name (else looked up from taskId, "
                        "else derived from the taskId)")
    p.add_argument("--difficulty", default=None,
                   help="benchmark difficulty label, e.g. Intermediate (else "
                        "looked up from taskId)")
    p.add_argument("--anthic-id", default=DEFAULT_ANTHIC_ID)
    p.add_argument("--username", default=None, help="use instead of --anthic-id")
    p.add_argument("--first", type=int, default=50, help="page size")
    p.add_argument("--max", type=int, default=1000, help="max plays to fetch")
    p.add_argument("--json", action="store_true")
    p.add_argument("--out", help="write JSON to this file")
    p.add_argument("--timeout", type=float, default=20.0)
    p.add_argument("--delay", type=float, default=0.25, help="pause between pages (be a good citizen)")
    p.add_argument("--header", action="append", default=[],
                   help='extra header "Key: Value" (repeatable)')
    p.add_argument("--session", default=None,
                   help="aimlabs.com session cookie value (else $AIMLAB_SESSION). "
                        "Preferred: mints a fresh bearer via /api/auth/session.")
    p.add_argument("--token", default=None,
                   help="raw access token / bearer (else $AIMLAB_TOKEN). Fallback.")
    p.add_argument("--env-file", default=".env",
                   help="path to a KEY=VALUE .env file to seed credentials (default: .env)")
    p.add_argument("--login", action="store_true",
                   help="open an Aim Lab login window, capture the session cookie "
                        "on success, and write it to --env-file, then exit "
                        "(needs: pip install pywebview)")
    p.add_argument("--login-timeout", type=float, default=300.0,
                   help="seconds to wait for login before giving up (default: 300)")
    p.add_argument("--no-login", action="store_true",
                   help="never auto-open the login window on a missing/expired "
                        "credential; print how to fix it and exit instead. Use "
                        "for scripts, cron, and other unattended runs.")
    args = p.parse_args(argv)

    # --login is a one-time setup command: capture, write .env, exit.
    if args.login:
        value = login_and_capture(env_path=args.env_file, timeout=args.login_timeout)
        if not value:
            return 1
        print("auth: done. run `python aimlab_history.py` to fetch history.",
              file=sys.stderr)
        return 0

    # Seed credentials from .env (never overrides an already-set env var).
    load_dotenv(args.env_file)

    extra_headers = {}
    for h in args.header:
        if ":" not in h:
            print(f"ignoring malformed --header {h!r}", file=sys.stderr)
            continue
        k, v = h.split(":", 1)
        extra_headers[k.strip()] = v.strip()

    # Resolve auth unless an Authorization header was passed explicitly.
    has_explicit_auth = any(k.lower() == "authorization" for k in extra_headers)
    if has_explicit_auth:
        print("auth: using Authorization header passed via --header", file=sys.stderr)
    else:
        authz = resolve_auth_or_login(args)
        if authz is None:
            return 1  # message already printed
        extra_headers["Authorization"] = authz

    # username takes precedence: drop anthic_id if a name was given.
    anthic_id = None if args.username else args.anthic_id

    try:
        plays, total = fetch_history(
            task_id=args.task_id, anthic_id=anthic_id, username=args.username,
            mode=args.mode, first=args.first, max_plays=args.max,
            timeout=args.timeout, delay=args.delay,
            extra_headers=extra_headers or None, verbose=True,
        )
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.json or args.out:
        disp_name, disp_diff = resolve_task_info(args.task_id, args.name, args.difficulty)
        doc = {
            "task_id": args.task_id, "mode": args.mode,
            "scenario": disp_name, "difficulty": disp_diff,
            "anthic_id": anthic_id, "username": args.username,
            "total_count": total, "fetched": len(plays),
            "summary": summarize([p["score"] for p in plays], plays),
            "plays": plays,
        }
        out = json.dumps(doc, indent=2)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(out)
            print(f"wrote {args.out} ({len(plays)} plays)", file=sys.stderr)
        else:
            print(out)
    else:
        print(format_report(args.task_id, args.mode, plays, total,
                            name=args.name, difficulty=args.difficulty))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())