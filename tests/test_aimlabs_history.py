import json
import unittest
from typing import Optional

from aimlabs_history import (
    AimlabsHistoryError,
    AimlabsUnauthorizedError,
    build_history_payload,
    fetch_history_page,
    parse_history_page,
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


if __name__ == "__main__":
    unittest.main()
