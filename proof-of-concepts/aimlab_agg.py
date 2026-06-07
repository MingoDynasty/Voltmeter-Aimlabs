#!/usr/bin/env python3
"""
aimlab_agg.py
=============
Fetch server-side play aggregates (count / avg / max) for Voltaic-Aimlabs
scenarios via the `taskPlaysAgg` query. One cheap call per scenario, no
pagination -- ideal for a fast overview across all 21/63 benchmark tasks.

How this differs from the other two scripts
-------------------------------------------
  aimlab_scores.py  -> PB leaderboard entry (one per scenario)
  aimlab_history.py -> every play + trimmed/rolling stats (paginated)
  aimlab_agg.py     -> count + lifetime avg + max, computed server-side (1 call)

IMPORTANT — what `avg` and `max` actually mean here
----------------------------------------------------
* `avg` is the LIFETIME average over every non-practice play, including months
  -old runs. It is NOT a rolling or trimmed mean, so it can sit well below your
  current ability. For a skill estimate use aimlab_history.py's trimmed/rolling
  stats; treat this avg as a lifetime baseline only.
* In this aggregate, max{score, accuracy, created_at} are computed
  INDEPENDENTLY (Hasura semantics). So:
    - max.score      = your PB                                  (trustworthy)
    - max.accuracy   = best accuracy EVER, likely on a different run than the PB
    - max.created_at = your most recent play timestamp = LAST PLAYED,
                       NOT when the PB was set.
  Columns are labeled accordingly (best_acc / last_played) to avoid that trap.

Usage (CLI)
-----------
  python aimlab_agg.py                                       # Adjustshot (default)
  python aimlab_agg.py --task-id "CsLevel.Lowgravity56.VT Float.RSM61S"
  python aimlab_agg.py --task-id A --task-id B               # several at once
  python aimlab_agg.py --difficulty intermediate            # sweep (needs aimlab_scores.py)
  python aimlab_agg.py --difficulty all --json --out agg.json
  python aimlab_agg.py --include-practice                    # count practice plays too

Dependencies
------------
Uses `requests` if installed, else stdlib urllib (zero install).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Optional

ENDPOINT = "https://api.aimlab.gg/graphql"
DEFAULT_USER_ID = "A32D4D127BA6094E"
DEFAULT_TASK_ID = "CsLevel.Lowgravity56.VT Adjus.RTUQMP"  # Adjustshot (Intermediate)

QUERY = """
query taskPlaysAgg($where: AimlabPlayWhere!) {
  aimlab {
    plays_agg(where: $where) {
      aggregate {
        count
        avg {
          score
          accuracy
        }
        max {
          score
          accuracy
          created_at
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
        import urllib.request

        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.getcode(), r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")


def _build_payload(user_id: str, task_id: str, is_practice: Optional[bool]) -> dict:
    # is_practice: False -> benchmark plays only (matches the captured request);
    #              True  -> practice plays only;
    #              None  -> omit the filter, count ALL plays.
    where: dict[str, Any] = {
        "task_id": {"_eq": task_id},
        "user_id": {"_eq": user_id},
    }
    if is_practice is not None:
        where["is_practice"] = {"_eq": is_practice}
    return {
        "operationName": "taskPlaysAgg",
        "query": QUERY,
        "variables": {"where": where},
    }


def _parse_agg(body_text: str) -> tuple[Optional[dict], Optional[str]]:
    try:
        payload = json.loads(body_text)
    except json.JSONDecodeError:
        return None, f"non-JSON response: {body_text[:200]}"
    if isinstance(payload, dict) and payload.get("errors"):
        return None, "graphql error: " + json.dumps(payload["errors"])[:300]
    try:
        # agg = payload["data"]["aimlab"]["plays_agg"]["aggregate"]
        agg = None
    except (KeyError, TypeError):
        return None, f"unexpected shape: {body_text[:200]}"
    if agg is None:
        return None, "no aggregate (unknown user/task?)"
    return agg, None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def fetch_agg(
    task_id: str = DEFAULT_TASK_ID,
    user_id: str = DEFAULT_USER_ID,
    *,
    is_practice: Optional[bool] = False,
    label: Optional[dict] = None,
    timeout: float = 20.0,
    retries: int = 2,
    extra_headers: Optional[dict] = None,
) -> dict:
    """Fetch aggregates for one task. Always returns a result dict (never raises)."""
    result: dict[str, Any] = {
        "task_id": task_id,
        "name": (label or {}).get("name"),
        "category": (label or {}).get("category"),
        "sub": (label or {}).get("sub"),
        "ok": False, "count": None,
        "avg_score": None, "avg_accuracy": None,
        "pb": None, "best_accuracy": None, "last_played": None,
        "error": None,
    }
    headers = dict(BASE_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    payload = _build_payload(user_id, task_id, is_practice)

    last_err = None
    for attempt in range(retries + 1):
        try:
            status, text = _post_json(ENDPOINT, payload, headers, timeout)
        except Exception as e:  # noqa: BLE001
            last_err = f"request failed: {e}"
        else:
            if status == 200:
                agg, err = _parse_agg(text)
                if agg is not None:
                    avg = agg.get("avg") or {}
                    mx = agg.get("max") or {}
                    result.update(
                        ok=True,
                        count=agg.get("count"),
                        avg_score=avg.get("score"),
                        avg_accuracy=avg.get("accuracy"),
                        pb=mx.get("score"),
                        best_accuracy=mx.get("accuracy"),
                        last_played=mx.get("created_at"),
                    )
                    return result
                last_err = err
            elif status == 403:
                last_err = ("HTTP 403 -- blocked. host_not_allowed => sandbox egress "
                            "proxy (run locally). Else Aimlabs WAF: add --header cookie.")
            else:
                last_err = f"HTTP {status}: {text[:200]}"
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))

    result["error"] = last_err
    return result


def fetch_many(
    items: list[dict],
    user_id: str = DEFAULT_USER_ID,
    *,
    delay: float = 0.15,
    **kwargs,
) -> list[dict]:
    """items: list of {task_id, name?, category?, sub?}."""
    out = []
    for i, item in enumerate(items):
        out.append(fetch_agg(item["task_id"], user_id, label=item, **kwargs))
        if delay and i < len(items) - 1:
            time.sleep(delay)
    return out


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def _code(task_id: Optional[str]) -> str:
    return task_id.rsplit(".", 1)[-1] if task_id else ""


def format_table(rows: list[dict]) -> str:
    has_names = any(r.get("name") for r in rows)
    headers = (["Scenario", "Cat/Sub"] if has_names else []) + \
        ["Code", "Plays", "AvgScore", "AvgAcc%", "PB", "BestAcc%", "Last played", "Note"]
    table = []
    for r in rows:
        def num(v, d=0):
            return f"{v:.{d}f}" if isinstance(v, (int, float)) else "-"
        if r["ok"]:
            row = [
                _code(r["task_id"]),
                str(r["count"]) if r["count"] is not None else "-",
                num(r["avg_score"], 0), num(r["avg_accuracy"], 1),
                num(r["pb"], 0), num(r["best_accuracy"], 1),
                (r["last_played"] or "")[:10], "",
            ]
        else:
            row = [_code(r["task_id"]), "-", "-", "-", "—", "-", "", (r["error"] or "")[:50]]
        if has_names:
            row = [r.get("name") or "", f"{r.get('category')}/{r.get('sub')}"
                   if r.get("category") else ""] + row
        table.append(row)

    widths = [len(h) for h in headers]
    for row in table:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))

    def fmt(row):
        return "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row))

    lines = [fmt(headers), "  ".join("-" * w for w in widths)]
    lines += [fmt(row) for row in table]
    ok = sum(1 for r in rows if r["ok"])
    total_plays = sum(r["count"] for r in rows if r.get("count"))
    lines.append("")
    lines.append(f"{ok}/{len(rows)} scenarios returned aggregates; {total_plays} plays counted total.")
    lines.append("Note: AvgScore is LIFETIME (not rolling); BestAcc% / Last played are "
                 "independent of the PB run.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _items_for_difficulty(difficulty: str) -> list[dict]:
    try:
        from aimlab_scores import get_scenarios
    except ImportError:
        raise SystemExit("--difficulty needs aimlab_scores.py next to this script "
                         "(or on PYTHONPATH).")
    return [{"task_id": s["task_id"], "name": s["name"],
             "category": s["category"], "sub": s["sub"]}
            for s in get_scenarios(difficulty)]


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Fetch Aimlabs play aggregates (count/avg/max).")
    p.add_argument("--task-id", action="append", default=[],
                   help="task id (repeatable). Defaults to Adjustshot if none given.")
    p.add_argument("--difficulty", choices=["novice", "intermediate", "advanced", "all"],
                   help="sweep every scenario in a difficulty (needs aimlab_scores.py)")
    p.add_argument("--user-id", default=DEFAULT_USER_ID)
    p.add_argument("--include-practice", action="store_true",
                   help="count practice plays too (default: benchmark plays only)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--out", help="write JSON to this file")
    p.add_argument("--timeout", type=float, default=20.0)
    p.add_argument("--delay", type=float, default=0.15, help="pause between scenarios")
    p.add_argument("--header", action="append", default=[],
                   help='extra header "Key: Value" (repeatable)')
    args = p.parse_args(argv)

    extra_headers = {}
    for h in args.header:
        if ":" not in h:
            print(f"ignoring malformed --header {h!r}", file=sys.stderr)
            continue
        k, v = h.split(":", 1)
        extra_headers[k.strip()] = v.strip()

    if args.difficulty:
        items = _items_for_difficulty(args.difficulty)
    elif args.task_id:
        items = [{"task_id": t} for t in args.task_id]
    else:
        items = [{"task_id": DEFAULT_TASK_ID}]

    rows = fetch_many(
        items, user_id=args.user_id,
        # default False = benchmark plays only; --include-practice -> None = all plays
        is_practice=None if args.include_practice else False,
        delay=args.delay, timeout=args.timeout,
        extra_headers=extra_headers or None,
    )

    if args.json or args.out:
        out = json.dumps(rows, indent=2)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(out)
            print(f"wrote {args.out}", file=sys.stderr)
        else:
            print(out)
    else:
        print(format_table(rows))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
