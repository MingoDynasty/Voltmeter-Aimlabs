"""Aimlabs GraphQL API client helpers."""

from __future__ import annotations

import json
import time
from typing import Optional

import requests

from benchmark_constants import DEFAULT_DIFFICULTY, DEFAULT_TASK_MODE, get_scenarios

ENDPOINT = "https://api.aimlab.gg/graphql"

LEADERBOARD_ENTRY_QUERY = """
query LeaderboardEntry($leaderboardInput: LeaderboardInput!) {
  aimlab {
    leaderboard(input: $leaderboardInput) {
      leaderboardEntries {
        data
        play {
          gridshieldStatus
        }
      }
    }
  }
}
"""

# Aimlabs expects browser-like metadata for these public web-client calls.
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
LEADERBOARD_SHAPE_ERRORS = (KeyError, TypeError)


def _post_json(url: str, payload: dict, headers: dict, timeout: float) -> tuple[int, str]:
    body = json.dumps(payload).encode("utf-8")
    response = requests.post(url, data=body, headers=headers, timeout=timeout)
    return response.status_code, response.text


def _build_payload(user_id: str, scenario: dict, source: str) -> dict:
    return {
        "operationName": "LeaderboardEntry",
        "query": LEADERBOARD_ENTRY_QUERY,
        "variables": {
            "leaderboardInput": {
                "clientId": "aimlab",
                "taskId": scenario["task_id"],
                "userId": user_id,
                "taskMode": scenario.get("task_mode", DEFAULT_TASK_MODE),
                "inputDevice": "MOUSE",
                "source": source,
                "weaponId": scenario["weapon_id"],
            }
        },
    }


def _parse_entry(body_text: str) -> tuple[Optional[dict], Optional[str]]:  # pylint: disable=too-many-return-statements
    try:
        payload = json.loads(body_text)
    except json.JSONDecodeError:
        return None, f"non-JSON response: {body_text[:200]}"
    if isinstance(payload, dict) and payload.get("errors"):
        return None, "graphql error: " + json.dumps(payload["errors"])[:300]
    try:
        entries = payload["data"]["aimlab"]["leaderboard"]["leaderboardEntries"]
    except LEADERBOARD_SHAPE_ERRORS:
        return None, f"unexpected shape: {body_text[:200]}"
    if not entries:
        return None, "no leaderboard entry (unplayed, or wrong task/weapon id?)"

    data = entries[0].get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return None, "could not parse entry data blob"
    if not isinstance(data, dict):
        return None, "entry data missing"
    return data, None


def _result_skeleton(scenario: dict) -> dict:
    return {
        "key": scenario["key"],
        "name": scenario["name"],
        "category": scenario.get("category"),
        "sub": scenario.get("sub"),
        "difficulty": scenario.get("difficulty"),
        "task_id": scenario.get("task_id"),
        "weapon_id": scenario.get("weapon_id"),
        "ok": False,
        "score": None,
        "accuracy": None,
        "rank": None,
        "ended_at": None,
        "error": None,
        "raw": None,
    }


def fetch_one(  # pylint: disable=too-many-arguments,too-many-locals
    scenario: dict,
    user_id: str,
    *,
    source: str = "cache",
    timeout: float = 20.0,
    retries: int = 2,
    extra_headers: Optional[dict] = None,
) -> dict:
    """Fetch one scenario PB. Always returns a result dict."""
    result = _result_skeleton(scenario)
    if not scenario.get("task_id") or not scenario.get("weapon_id"):
        result["error"] = "task_id/weapon_id not filled in"
        return result

    headers = dict(BASE_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    payload = _build_payload(user_id, scenario, source)

    last_error = None
    for attempt in range(retries + 1):
        try:
            status, text = _post_json(ENDPOINT, payload, headers, timeout)
        except Exception as error:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            last_error = f"request failed: {error}"
        else:
            if status == 200:
                data, parse_error = _parse_entry(text)
                if data:
                    result.update(
                        ok=True,
                        score=data.get("score"),
                        accuracy=data.get("accuracy"),
                        rank=data.get("rank"),
                        ended_at=data.get("ended_at"),
                        raw=data,
                    )
                    return result
                last_error = parse_error
            elif status == 403:
                last_error = "HTTP 403 -- request rejected. Check local network access or configured Aimlabs auth."
            else:
                last_error = f"HTTP {status}: {text[:200]}"
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))

    result["error"] = last_error
    return result


def fetch_all_scores(
    user_id: str,
    scenarios: Optional[list[dict]] = None,
    *,
    difficulty: str = DEFAULT_DIFFICULTY,
    request_delay: float = 0.25,
    deadline_at: Optional[float] = None,
    **kwargs,
) -> list[dict]:
    """Fetch a list of scenarios, defaulting to all difficulties."""
    if scenarios is None:
        scenarios = get_scenarios(difficulty)

    rows = []
    for scenario_idx, scenario in enumerate(scenarios):
        if deadline_at is not None and time.monotonic() >= deadline_at:
            result = _result_skeleton(scenario)
            result["error"] = "run deadline reached before request"
            rows.append(result)
            continue
        if scenario_idx and request_delay > 0:
            time.sleep(request_delay)
        rows.append(fetch_one(scenario, user_id, **kwargs))
    return rows
