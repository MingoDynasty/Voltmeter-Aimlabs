import json
import unittest
from typing import Optional

from aimlabs_history import (
    AimlabsCursorRejectedError,
    AimlabsHistoryError,
    AimlabsTransientHistoryError,
    AimlabsUnauthorizedError,
    build_history_payload,
    build_practice_contamination_payload,
    fetch_history_page,
    fetch_practice_contamination_count,
    parse_history_page,
    parse_practice_contamination_count,
    validate_page_size,
)

ACCOUNT_ID = "anthic-account-a"


def history_body(
    *,
    plays: list[dict],
    total_count: int,
    has_next_page: bool = False,
    end_cursor: Optional[str] = None,
) -> str:
    return json.dumps(
        {
            "data": {
                "aimlabProfile": {
                    "plays": {
                        "pageInfo": {
                            "hasNextPage": has_next_page,
                            "endCursor": end_cursor,
                        },
                        "totalCount": total_count,
                        "edges": [{"node": play} for play in plays],
                    },
                },
            },
        }
    )


class AimlabsHistoryTests(unittest.TestCase):
    def test_build_history_payload_uses_unfiltered_mode_stream(self) -> None:
        payload = build_history_payload(ACCOUNT_ID, page_size=50, after="cursor-1")

        variables = payload["variables"]
        self.assertEqual(variables["anthicId"], ACCOUNT_ID)
        self.assertEqual(variables["filter"], {"mode": 42})
        self.assertEqual(variables["first"], 50)
        self.assertEqual(variables["after"], "cursor-1")
        self.assertNotIn("username", variables)
        self.assertNotIn("taskId", variables["filter"])

    def test_parse_history_page_returns_nodes_and_pagination(self) -> None:
        play = {
            "id": "play-1",
            "endedAt": "2026-06-06T02:43:54.249Z",
            "task": {"id": "task-a"},
            "score": 100,
        }

        page = parse_history_page(
            history_body(
                plays=[play],
                total_count=3,
                has_next_page=True,
                end_cursor="cursor-2",
            )
        )

        self.assertEqual(page.plays, (play,))
        self.assertEqual(page.total_count, 3)
        self.assertTrue(page.has_next_page)
        self.assertEqual(page.end_cursor, "cursor-2")

    def test_parse_history_page_rejects_graphql_errors(self) -> None:
        with self.assertRaisesRegex(AimlabsHistoryError, "graphql error"):
            parse_history_page(json.dumps({"errors": [{"message": "bad"}]}))

    def test_parse_history_page_classifies_cursor_rejection(self) -> None:
        with self.assertRaises(AimlabsCursorRejectedError):
            parse_history_page(json.dumps({"errors": [{"message": "invalid cursor for after"}]}))

    def test_page_size_bounds(self) -> None:
        self.assertEqual(validate_page_size(1), 1)
        self.assertEqual(validate_page_size(200), 200)
        for bad_page_size in (0, 201, True):
            with self.assertRaises(ValueError):
                validate_page_size(bad_page_size)

    def test_fetch_history_page_sends_bearer_and_parses_response(self) -> None:
        requests = []

        def fake_post(_url: str, _payload: dict, _headers: dict, _timeout: float) -> tuple[int, str]:
            requests.append((_url, _payload, _headers, _timeout))
            return 200, history_body(plays=[], total_count=0)

        page = fetch_history_page(
            ACCOUNT_ID,
            "fresh-token",
            page_size=25,
            after=None,
            timeout=12,
            post_json=fake_post,
        )

        self.assertEqual(page.total_count, 0)
        self.assertEqual(requests[0][2]["Authorization"], "Bearer fresh-token")
        self.assertEqual(requests[0][1]["variables"]["first"], 25)
        self.assertEqual(requests[0][3], 12)

    def test_fetch_history_page_raises_for_unauthorized(self) -> None:
        def fake_post(_url: str, _payload: dict, _headers: dict, _timeout: float) -> tuple[int, str]:
            return 401, "{}"

        with self.assertRaises(AimlabsUnauthorizedError):
            fetch_history_page(ACCOUNT_ID, "expired-token", post_json=fake_post)

    def test_fetch_history_page_classifies_transient_http_statuses(self) -> None:
        for status_code in (429, 500, 503):

            def fake_post(_url: str, _payload: dict, _headers: dict, _timeout: float) -> tuple[int, str]:
                return status_code, "{}"

            with self.assertRaises(AimlabsTransientHistoryError):
                fetch_history_page(ACCOUNT_ID, "fresh-token", post_json=fake_post)


def contamination_body(count: int) -> str:
    # The live endpoint wraps plays_agg in a single-element array.
    return json.dumps({"data": {"aimlab": {"plays_agg": [{"aggregate": {"count": count}}]}}})


class PracticeContaminationTests(unittest.TestCase):
    def test_payload_filters_practice_plays_by_task_mode(self) -> None:
        payload = build_practice_contamination_payload(ACCOUNT_ID)

        self.assertEqual(
            payload["variables"]["where"],
            {
                "user_id": {"_eq": ACCOUNT_ID},
                # The aggregate's mode field is task_mode, not the history filter's mode.
                "task_mode": {"_eq": 42},
                "is_practice": {"_eq": True},
            },
        )
        # count only — the aggregate's max{} 500s on an empty result set (§5.2).
        self.assertIn("count", payload["query"])
        self.assertNotIn("max", payload["query"])
        self.assertNotIn("avg", payload["query"])

    def test_fetch_sends_bearer_and_parses_count(self) -> None:
        requests = []

        def fake_post(url: str, payload: dict, headers: dict, timeout: float) -> tuple[int, str]:
            requests.append((url, payload, headers, timeout))
            return 200, contamination_body(7)

        practice_count = fetch_practice_contamination_count(
            ACCOUNT_ID,
            "fresh-token",
            timeout=12,
            post_json=fake_post,
        )

        self.assertEqual(practice_count, 7)
        self.assertEqual(requests[0][2]["Authorization"], "Bearer fresh-token")
        self.assertEqual(requests[0][3], 12)

    def test_fetch_maps_http_statuses_to_domain_errors(self) -> None:
        status_expectations = (
            (401, AimlabsUnauthorizedError),
            (429, AimlabsTransientHistoryError),
            (503, AimlabsTransientHistoryError),
            (404, AimlabsHistoryError),
        )
        for status_code, expected_error in status_expectations:

            def fake_post(_url: str, _payload: dict, _headers: dict, _timeout: float) -> tuple[int, str]:
                return status_code, "{}"

            with self.assertRaises(expected_error):
                fetch_practice_contamination_count(ACCOUNT_ID, "fresh-token", post_json=fake_post)

    def test_parse_rejects_errors_and_malformed_shapes(self) -> None:
        with self.assertRaisesRegex(AimlabsHistoryError, "graphql error"):
            parse_practice_contamination_count(json.dumps({"errors": [{"message": "bad"}]}))
        with self.assertRaisesRegex(AimlabsHistoryError, "unexpected aggregate response shape"):
            parse_practice_contamination_count(json.dumps({"data": {}}))
        with self.assertRaisesRegex(AimlabsHistoryError, "count must be an integer"):
            parse_practice_contamination_count(
                json.dumps({"data": {"aimlab": {"plays_agg": {"aggregate": {"count": "7"}}}}})
            )
        with self.assertRaisesRegex(AimlabsHistoryError, "non-JSON"):
            parse_practice_contamination_count("not json")

    def test_parse_handles_array_wrapped_aggregate(self) -> None:
        # The live endpoint returns plays_agg as a single-element array.
        self.assertEqual(parse_practice_contamination_count(contamination_body(3)), 3)
        # An empty array means no matching plays.
        self.assertEqual(
            parse_practice_contamination_count(json.dumps({"data": {"aimlab": {"plays_agg": []}}})),
            0,
        )


if __name__ == "__main__":
    unittest.main()
