"""Throwaway diagnostic: watch an aimlabs.com session decay over time.

Goal: determine whether the server-side access-token *refresh* works at all for
a cookie-captured session, or whether the session only survives as long as the
access token present at capture (~1h).

Single consumer (run with NO browser open on aimlabs.com). Polls
/api/auth/session on an interval, FOLLOWS the rotated Set-Cookie session-token
(so single-use rotation can't orphan us), and records -- relative to the FIRST
observed accessTokenExpiresAt -- exactly when `accessToken` disappears and
`accessTokenError` shows up.

Never prints token values. accessTokenExpiresAt / expires are timestamps, safe.

Usage (after a FRESH `voltmeter login`):
    python _monitor_session.py            # 60s interval, stop on first failure
    python _monitor_session.py --interval 120 --max-min 180
"""
from __future__ import annotations

import argparse
import datetime as dt
import http.cookies
import json
import sys
import time
import urllib.error
import urllib.request

SESSION_URL = "https://aimlabs.com/api/auth/session"
NAME = "__Secure-next-auth.session-token"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
LOG_PATH = "_monitor_session.log"


def load_session_from_env(path: str = ".env") -> str:
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("AIMLAB_SESSION=") and "=" in line:
                return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit("AIMLAB_SESSION not found in .env -- run --login first.")


def cookie_header(value: str) -> str:
    looks_full = ("session-token" in value) or ("; " in value)
    return value if looks_full else f"{NAME}={value}"


def poll(value: str, timeout: float = 20.0):
    req = urllib.request.Request(
        SESSION_URL,
        headers={"Cookie": cookie_header(value), "Accept": "application/json",
                 "User-Agent": UA},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, setc = r.status, (r.headers.get_all("Set-Cookie") or [])
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        status, setc = e.code, (e.headers.get_all("Set-Cookie") or [])
        try:
            data = json.loads(e.read().decode("utf-8", "replace"))
        except json.JSONDecodeError:
            data = {"_nonjson": True}
    rotated = None
    for c in setc:
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(c)
        except http.cookies.CookieError:
            continue
        if NAME in jar:
            rotated = jar[NAME].value
    return status, data, rotated


def fmt_ate(ate) -> tuple[str, float | None]:
    """Return (human, seconds_from_now). accessTokenExpiresAt may be sec or ms."""
    if not isinstance(ate, (int, float)):
        return (repr(ate), None)
    secs = float(ate) / (1000 if float(ate) > 1e12 else 1)
    human = dt.datetime.fromtimestamp(secs).strftime("%H:%M:%S")
    return (human, secs - time.time())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=60.0, help="seconds between polls")
    ap.add_argument("--max-min", type=float, default=150.0, help="max run minutes")
    ap.add_argument("--no-follow-rotation", action="store_true",
                    help="keep sending the ORIGINAL cookie instead of following Set-Cookie")
    args = ap.parse_args()

    follow = not args.no_follow_rotation
    current = load_session_from_env()
    start = time.time()
    deadline = start + args.max_min * 60
    first_ate_human: str | None = None
    poll_n = 0
    log = open(LOG_PATH, "a", encoding="utf-8")

    def emit(msg: str) -> None:
        line = f"{dt.datetime.now():%H:%M:%S} {msg}"
        print(line, flush=True)
        log.write(line + "\n")
        log.flush()

    emit(f"=== monitor start | follow_rotation={follow} interval={args.interval}s "
         f"max={args.max_min}min | cookie_len={len(current)} ===")

    while time.time() < deadline:
        poll_n += 1
        elapsed = (time.time() - start) / 60.0
        try:
            status, data, rotated = poll(current)
        except Exception as e:  # noqa: BLE001
            emit(f"[+{elapsed:5.1f}m] poll #{poll_n} REQUEST FAILED: {e!r}")
            time.sleep(args.interval)
            continue

        has_token = bool(data.get("accessToken"))
        err = data.get("accessTokenError")
        ate_human, ate_in = fmt_ate(data.get("accessTokenExpiresAt"))
        if first_ate_human is None and has_token and ate_human != "None":
            first_ate_human = ate_human
        ate_in_str = f"{ate_in/60:+.1f}m" if ate_in is not None else "n/a"
        rot = ""
        if rotated is not None:
            rot = f" rot(len={len(rotated)},changed={rotated != current})"
            if follow:
                current = rotated

        emit(f"[+{elapsed:5.1f}m] #{poll_n} http={status} token={'Y' if has_token else 'N'}"
             f" err={err!r} ATE={ate_human}(in {ate_in_str}) keys={sorted(data.keys())}{rot}")

        if not has_token:
            emit("=== FAILURE DETECTED ===")
            emit(f"    first accessTokenExpiresAt seen : {first_ate_human}")
            emit(f"    failed at elapsed              : +{elapsed:.1f} min")
            emit(f"    accessTokenError               : {err!r}")
            emit("    => Compare 'failed at' to the first ATE: a match means the "
                 "FIRST refresh failed (refresh is broken / single-use). Surviving "
                 "several ATE cycles before this would mean rotation-following works.")
            log.close()
            return 0
        time.sleep(args.interval)

    emit(f"=== SURVIVED full {args.max_min} min without losing accessToken ===")
    emit(f"    first accessTokenExpiresAt seen: {first_ate_human}")
    emit("    => If this exceeds the ~1h access-token lifetime, refresh IS working "
         "when we follow rotation -> Set-Cookie persistence is the fix.")
    log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
