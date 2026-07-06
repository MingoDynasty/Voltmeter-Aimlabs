import json
import unittest
from unittest.mock import Mock, call, patch

import aimlabs_client
from aimlabs_client import _parse_entry


def _scenario(scenario_idx: int = 1) -> dict:
    return {
        "key": f"synthetic-{scenario_idx}",
        "name": f"Synthetic Scenario {scenario_idx}",
        "category": "Synthetic Category",
        "sub": "Synthetic Subcategory",
        "difficulty": "Synthetic Difficulty",
        "task_id": f"synthetic-task-{scenario_idx}",
        "weapon_id": f"synthetic-weapon-{scenario_idx}",
    }


def _entry_body(entry_data: dict) -> str:
    return json.dumps(
        {
            "data": {
                "aimlab": {
                    "leaderboard": {
                        "leaderboardEntries": [{"data": entry_data}],
                    },
                },
            },
        }
    )


class AimlabsClientTests(unittest.TestCase):
    def test_post_json_returns_response_status_and_text(self) -> None:
        response = Mock(status_code=202, text="synthetic response")
        with patch("aimlabs_client.requests.post", return_value=response) as post_mock:
            result = aimlabs_client._post_json(
                "https://example.invalid/graphql",
                {"synthetic": True},
                {"X-Synthetic": "header"},
                4.5,
            )

        self.assertEqual(result, (202, "synthetic response"))
        post_mock.assert_called_once_with(
            "https://example.invalid/graphql",
            data=b'{"synthetic": true}',
            headers={"X-Synthetic": "header"},
            timeout=4.5,
        )

    def test_post_json_propagates_requests_exceptions(self) -> None:
        with patch(
            "aimlabs_client.requests.post",
            side_effect=aimlabs_client.requests.exceptions.Timeout("synthetic timeout"),
        ):
            with self.assertRaises(aimlabs_client.requests.exceptions.Timeout):
                aimlabs_client._post_json(
                    "https://example.invalid/graphql",
                    {"synthetic": True},
                    {},
                    4.5,
                )

    def test_parse_entry_valid_data_dict(self) -> None:
        body_text = json.dumps(
            {
                "data": {
                    "aimlab": {
                        "leaderboard": {
                            "leaderboardEntries": [
                                {"data": {"score": 123, "accuracy": 99.5}},
                            ],
                        },
                    },
                },
            }
        )

        data, error = _parse_entry(body_text)

        self.assertEqual(data, {"score": 123, "accuracy": 99.5})
        self.assertIsNone(error)

    def test_parse_entry_valid_stringified_data_blob(self) -> None:
        body_text = json.dumps(
            {
                "data": {
                    "aimlab": {
                        "leaderboard": {
                            "leaderboardEntries": [
                                {"data": json.dumps({"score": 456})},
                            ],
                        },
                    },
                },
            }
        )

        data, error = _parse_entry(body_text)

        self.assertEqual(data, {"score": 456})
        self.assertIsNone(error)

    def test_parse_entry_graphql_errors(self) -> None:
        data, error = _parse_entry(json.dumps({"errors": [{"message": "bad request"}]}))

        self.assertIsNone(data)
        self.assertIn("graphql error", error or "")

    def test_parse_entry_non_json_body(self) -> None:
        data, error = _parse_entry("<html>nope</html>")

        self.assertIsNone(data)
        self.assertIn("non-JSON response", error or "")

    def test_parse_entry_empty_leaderboard_entries(self) -> None:
        body_text = json.dumps(
            {
                "data": {
                    "aimlab": {
                        "leaderboard": {
                            "leaderboardEntries": [],
                        },
                    },
                },
            }
        )

        data, error = _parse_entry(body_text)

        self.assertIsNone(data)
        self.assertIn("no leaderboard entry", error or "")

    def test_parse_entry_bad_stringified_data_blob(self) -> None:
        body_text = json.dumps(
            {
                "data": {
                    "aimlab": {
                        "leaderboard": {
                            "leaderboardEntries": [
                                {"data": "{not json"},
                            ],
                        },
                    },
                },
            }
        )

        data, error = _parse_entry(body_text)

        self.assertIsNone(data)
        self.assertIn("could not parse entry data blob", error or "")

    def test_fetch_one_success_on_first_attempt(self) -> None:
        entry_data = {
            "score": 123456,
            "accuracy": 98.75,
            "rank": 42,
            "ended_at": "2026-01-02T03:04:05Z",
        }
        with (
            patch(
                "aimlabs_client._post_json", return_value=(200, _entry_body(entry_data))
            ) as post_mock,
            patch("aimlabs_client.time.sleep") as sleep_mock,
        ):
            result = aimlabs_client.fetch_one(_scenario(), "synthetic-user")

        self.assertTrue(result["ok"])
        self.assertEqual(result["score"], 123456)
        self.assertEqual(result["accuracy"], 98.75)
        self.assertEqual(result["rank"], 42)
        self.assertEqual(result["ended_at"], "2026-01-02T03:04:05Z")
        self.assertEqual(result["raw"], entry_data)
        self.assertIsNone(result["error"])
        post_mock.assert_called_once()
        sleep_mock.assert_not_called()

    def test_fetch_one_retries_transient_exception_then_succeeds(self) -> None:
        entry_data = {"score": 654321}
        with (
            patch(
                "aimlabs_client._post_json",
                side_effect=[
                    RuntimeError("synthetic connection failure"),
                    (200, _entry_body(entry_data)),
                ],
            ) as post_mock,
            patch("aimlabs_client.time.sleep") as sleep_mock,
        ):
            result = aimlabs_client.fetch_one(_scenario(), "synthetic-user")

        self.assertTrue(result["ok"])
        self.assertEqual(result["score"], 654321)
        self.assertEqual(result["raw"], entry_data)
        self.assertEqual(post_mock.call_count, 2)
        sleep_mock.assert_called_once_with(1.5)

    def test_fetch_one_retries_http_403_through_final_attempt(self) -> None:
        with (
            patch(
                "aimlabs_client._post_json", return_value=(403, "synthetic forbidden")
            ) as post_mock,
            patch("aimlabs_client.time.sleep") as sleep_mock,
        ):
            result = aimlabs_client.fetch_one(_scenario(), "synthetic-user", retries=2)

        self.assertFalse(result["ok"])
        self.assertIn("403", result["error"])
        self.assertIn("auth", result["error"].lower())
        self.assertEqual(post_mock.call_count, 3)
        self.assertEqual(sleep_mock.call_args_list, [call(1.5), call(3.0)])

    def test_fetch_one_non_200_uses_last_failure_text(self) -> None:
        with (
            patch(
                "aimlabs_client._post_json",
                side_effect=[
                    (500, "synthetic first failure"),
                    (502, "synthetic last failure"),
                ],
            ) as post_mock,
            patch("aimlabs_client.time.sleep") as sleep_mock,
        ):
            result = aimlabs_client.fetch_one(_scenario(), "synthetic-user", retries=1)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "HTTP 502: synthetic last failure")
        self.assertEqual(post_mock.call_count, 2)
        sleep_mock.assert_called_once_with(1.5)

    def test_fetch_one_missing_ids_returns_before_request(self) -> None:
        with (
            patch("aimlabs_client._post_json") as post_mock,
            patch("aimlabs_client.time.sleep") as sleep_mock,
        ):
            for missing_key in ("task_id", "weapon_id"):
                with self.subTest(missing_key=missing_key):
                    scenario = _scenario()
                    scenario[missing_key] = None
                    result = aimlabs_client.fetch_one(scenario, "synthetic-user")

                    self.assertFalse(result["ok"])
                    self.assertIn("task_id/weapon_id", result["error"])
        post_mock.assert_not_called()
        sleep_mock.assert_not_called()

    def test_fetch_all_scores_past_deadline_skips_every_request(self) -> None:
        scenarios = [_scenario(1), _scenario(2)]
        with (
            patch("aimlabs_client._post_json") as post_mock,
            patch("aimlabs_client.time.sleep") as sleep_mock,
        ):
            results = aimlabs_client.fetch_all_scores(
                "synthetic-user",
                scenarios,
                deadline_at=aimlabs_client.time.monotonic() - 1.0,
            )

        self.assertEqual(len(results), 2)
        for result in results:
            self.assertFalse(result["ok"])
            self.assertIn("run deadline reached", result["error"])
        post_mock.assert_not_called()
        sleep_mock.assert_not_called()

    def test_fetch_all_scores_sleeps_between_requests(self) -> None:
        scenarios = [_scenario(1), _scenario(2), _scenario(3)]
        with (
            patch(
                "aimlabs_client._post_json",
                return_value=(200, _entry_body({"score": 123})),
            ) as post_mock,
            patch("aimlabs_client.time.sleep") as sleep_mock,
        ):
            results = aimlabs_client.fetch_all_scores(
                "synthetic-user",
                scenarios,
                request_delay=0.25,
            )

        self.assertTrue(all(result["ok"] for result in results))
        self.assertEqual(post_mock.call_count, 3)
        self.assertEqual(sleep_mock.call_args_list, [call(0.25), call(0.25)])


if __name__ == "__main__":
    unittest.main()
