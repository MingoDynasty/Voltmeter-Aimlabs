import json
import unittest

from aimlabs_client import _parse_entry


class AimlabsClientTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
