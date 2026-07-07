"""Stateless Aimlabs history page fetching and parsing."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Optional

import requests

from aimlabs_client import BASE_HEADERS, ENDPOINT
from benchmark_constants import DEFAULT_TASK_MODE

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

# Practice-contamination check (design §5.2/§8.3): count only — the aggregate's
# max{} 500s on an empty result set, and its mode field is task_mode (not mode).
PRACTICE_CONTAMINATION_QUERY = """
query taskPlaysAgg($where: AimlabPlayWhere!) {
  aimlab {
    plays_agg(where: $where) {
      aggregate {
        count
      }
    }
  }
}
"""

RUN_HISTORY_QUERY = """
query RunHistory($filter: PlayFilterInput, $first: Int, $anthicId: String, $after: String) {
  aimlabProfile(anthicId: $anthicId) {
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
          task {
            id
          }
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


class AimlabsHistoryError(RuntimeError):
    """Raised when an Aimlabs history page cannot be fetched or parsed."""


class AimlabsUnauthorizedError(AimlabsHistoryError):
    """Raised when the history endpoint rejects the supplied credential."""


class AimlabsTransientHistoryError(AimlabsHistoryError):
    """Raised for retryable history endpoint failures."""

    def __init__(self, status_code: int, body_text: str) -> None:
        self.status_code = status_code
        super().__init__(f"history request HTTP {status_code}: {body_text[:200]}")


class AimlabsCursorRejectedError(AimlabsHistoryError):
    """Raised when the API rejects a stored Relay cursor."""


@dataclass(frozen=True)
class HistoryPage:
    plays: tuple[dict[str, Any], ...]
    total_count: int
    has_next_page: bool
    end_cursor: Optional[str]


PostJson = Callable[[str, dict[str, Any], dict[str, str], float], tuple[int, str]]


def validate_page_size(page_size: int) -> int:
    if isinstance(page_size, bool) or not isinstance(page_size, int):
        raise ValueError("page_size must be an integer.")
    if not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}.")
    return page_size


def build_history_payload(
    account_id: str,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    after: Optional[str] = None,
    mode: int = DEFAULT_TASK_MODE,
) -> dict[str, Any]:
    if not account_id:
        raise AimlabsHistoryError("account_id is required.")
    if isinstance(mode, bool) or not isinstance(mode, int):
        raise AimlabsHistoryError("mode must be an integer.")

    variables: dict[str, Any] = {
        "anthicId": account_id,
        "filter": {"mode": mode},
        "first": validate_page_size(page_size),
    }
    if after is not None:
        variables["after"] = after
    return {
        "operationName": "RunHistory",
        "query": RUN_HISTORY_QUERY,
        "variables": variables,
    }


def fetch_history_page(  # noqa: PLR0913
    account_id: str,
    bearer: str,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    after: Optional[str] = None,
    mode: int = DEFAULT_TASK_MODE,
    timeout: float = 20.0,
    post_json: Optional[PostJson] = None,
) -> HistoryPage:
    payload = build_history_payload(
        account_id, page_size=page_size, after=after, mode=mode
    )
    headers = dict(BASE_HEADERS)
    headers["Authorization"] = _authorization_header(bearer)
    sender = _post_json if post_json is None else post_json
    status_code, body_text = sender(ENDPOINT, payload, headers, timeout)
    if status_code == 401:
        raise AimlabsUnauthorizedError(
            "history request was unauthenticated; run `voltmeter login`."
        )
    if status_code == 429 or 500 <= status_code <= 599:
        raise AimlabsTransientHistoryError(status_code, body_text)
    if status_code != 200:
        raise AimlabsHistoryError(
            f"history request HTTP {status_code}: {body_text[:200]}"
        )
    return parse_history_page(body_text)


def build_practice_contamination_payload(
    account_id: str, *, mode: int = DEFAULT_TASK_MODE
) -> dict[str, Any]:
    if not account_id:
        raise AimlabsHistoryError("account_id is required.")
    if isinstance(mode, bool) or not isinstance(mode, int):
        raise AimlabsHistoryError("mode must be an integer.")
    return {
        "operationName": "taskPlaysAgg",
        "query": PRACTICE_CONTAMINATION_QUERY,
        "variables": {
            "where": {
                "user_id": {"_eq": account_id},
                "task_mode": {"_eq": mode},
                "is_practice": {"_eq": True},
            },
        },
    }


def fetch_practice_contamination_count(
    account_id: str,
    bearer: str,
    *,
    mode: int = DEFAULT_TASK_MODE,
    timeout: float = 20.0,
    post_json: Optional[PostJson] = None,
) -> int:
    """Count practice plays in the mode stream via the aggregate endpoint (§8.3 safety net)."""
    payload = build_practice_contamination_payload(account_id, mode=mode)
    headers = dict(BASE_HEADERS)
    headers["Authorization"] = _authorization_header(bearer)
    sender = _post_json if post_json is None else post_json
    status_code, body_text = sender(ENDPOINT, payload, headers, timeout)
    if status_code == 401:
        raise AimlabsUnauthorizedError(
            "aggregate request was unauthenticated; run `voltmeter login`."
        )
    if status_code == 429 or 500 <= status_code <= 599:
        raise AimlabsTransientHistoryError(status_code, body_text)
    if status_code != 200:
        raise AimlabsHistoryError(
            f"aggregate request HTTP {status_code}: {body_text[:200]}"
        )
    return parse_practice_contamination_count(body_text)


def parse_practice_contamination_count(body_text: str) -> int:
    try:
        payload = json.loads(body_text)
    except json.JSONDecodeError as error:
        raise AimlabsHistoryError(
            f"aggregate response was non-JSON: {body_text[:200]}"
        ) from error

    if not isinstance(payload, Mapping):
        raise AimlabsHistoryError("aggregate response must be a JSON object.")
    errors = payload.get("errors")
    if errors:
        raise AimlabsHistoryError(
            "graphql error: " + json.dumps(errors, sort_keys=True)[:300]
        )

    try:
        plays_agg = payload["data"]["aimlab"]["plays_agg"]
    except (KeyError, TypeError) as error:
        raise AimlabsHistoryError("unexpected aggregate response shape.") from error

    # The endpoint wraps plays_agg in a single-element array: [{"aggregate": {"count": N}}].
    # An empty array means no matching plays -> count 0. A bare object form is also tolerated.
    if isinstance(plays_agg, list):
        if not plays_agg:
            return 0
        group = plays_agg[0]
    else:
        group = plays_agg
    if not isinstance(group, Mapping):
        raise AimlabsHistoryError("unexpected aggregate response shape.")
    aggregate = group.get("aggregate")
    if not isinstance(aggregate, Mapping):
        raise AimlabsHistoryError("aggregate block must be an object.")
    count = aggregate.get("count")
    if isinstance(count, bool) or not isinstance(count, int):
        raise AimlabsHistoryError("aggregate count must be an integer.")
    return count


def parse_history_page(body_text: str) -> HistoryPage:
    try:
        payload = json.loads(body_text)
    except json.JSONDecodeError as error:
        raise AimlabsHistoryError(
            f"history response was non-JSON: {body_text[:200]}"
        ) from error

    if not isinstance(payload, Mapping):
        raise AimlabsHistoryError("history response must be a JSON object.")
    errors = payload.get("errors")
    if errors:
        error_text = json.dumps(errors, sort_keys=True)
        if _looks_like_cursor_rejection(error_text):
            raise AimlabsCursorRejectedError(
                "graphql cursor rejected: " + error_text[:300]
            )
        raise AimlabsHistoryError("graphql error: " + error_text[:300])

    plays_block = _plays_block(payload)
    total_count = plays_block.get("totalCount")
    if isinstance(total_count, bool) or not isinstance(total_count, int):
        raise AimlabsHistoryError("plays.totalCount must be an integer.")

    page_info_value = plays_block.get("pageInfo")
    page_info = page_info_value if isinstance(page_info_value, Mapping) else {}
    has_next_page = bool(page_info.get("hasNextPage"))
    end_cursor = _optional_text(page_info.get("endCursor"), "pageInfo.endCursor")
    if has_next_page and end_cursor is None:
        raise AimlabsHistoryError(
            "pageInfo.endCursor is required when hasNextPage is true."
        )

    edges_value = plays_block.get("edges") or []
    if not isinstance(edges_value, list):
        raise AimlabsHistoryError("plays.edges must be a list.")

    plays = []
    for edge in edges_value:
        if not isinstance(edge, Mapping):
            raise AimlabsHistoryError("each play edge must be an object.")
        node = edge.get("node")
        if node is None:
            continue
        if not isinstance(node, Mapping):
            raise AimlabsHistoryError("each play edge node must be an object.")
        plays.append(dict(node))

    return HistoryPage(
        plays=tuple(plays),
        total_count=total_count,
        has_next_page=has_next_page,
        end_cursor=end_cursor,
    )


def _post_json(
    url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float
) -> tuple[int, str]:
    body = json.dumps(payload).encode("utf-8")
    response = requests.post(url, data=body, headers=headers, timeout=timeout)
    return response.status_code, response.text


def _plays_block(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    try:
        data = payload["data"]
        profile = data["aimlabProfile"]
        plays_block = profile["plays"]
    except (KeyError, TypeError) as error:
        raise AimlabsHistoryError("unexpected history response shape.") from error
    if not isinstance(plays_block, Mapping):
        raise AimlabsHistoryError("history response plays block must be an object.")
    return plays_block


def _optional_text(value: Any, field_name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AimlabsHistoryError(f"{field_name} must be a string when present.")
    return value


def _authorization_header(bearer: str) -> str:
    if bearer.startswith("Bearer "):
        return bearer
    return f"Bearer {bearer}"


def _looks_like_cursor_rejection(error_text: str) -> bool:
    # GraphQL does not expose a typed cursor-expiry code here, so this is a best-effort classifier.
    normalized_error = error_text.lower()
    mentions_cursor = ("cursor" in normalized_error) or ("after" in normalized_error)
    mentions_rejection = any(
        rejection_word in normalized_error
        for rejection_word in ("invalid", "expired", "malformed", "rejected", "bad")
    )
    return mentions_cursor and mentions_rejection
