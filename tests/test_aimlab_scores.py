import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

import aimlab_scores


class AuthHeaderTests(unittest.TestCase):
    def test_env_aimlab_session_resolves_cookie_header(self) -> None:
        headers = aimlab_scores._auth_headers_from_config(
            env={"AIMLAB_SESSION": "unified-token"},
            dotenv_path=None,
        )

        self.assertEqual(headers, {"Cookie": "__Secure-next-auth.session-token=unified-token"})

    def test_env_aimlab_session_wins_over_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            dotenv_path = Path(temp_dir) / ".env"
            dotenv_path.write_text('AIMLAB_SESSION="dotenv-token"\n', encoding="utf-8")

            headers = aimlab_scores._auth_headers_from_config(
                env={"AIMLAB_SESSION": "env-token"},
                dotenv_path=dotenv_path,
            )

        self.assertEqual(headers, {"Cookie": "__Secure-next-auth.session-token=env-token"})

    def test_dotenv_aimlab_session_resolves_when_env_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            dotenv_path = Path(temp_dir) / ".env"
            dotenv_path.write_text('AIMLAB_SESSION="dotenv-token"\n', encoding="utf-8")

            headers = aimlab_scores._auth_headers_from_config(
                env={},
                dotenv_path=dotenv_path,
            )

        self.assertEqual(headers, {"Cookie": "__Secure-next-auth.session-token=dotenv-token"})

    def test_no_channels_yields_no_auth_headers(self) -> None:
        headers = aimlab_scores._auth_headers_from_config(env={}, dotenv_path=None)

        self.assertEqual(headers, {})


class AimlabScoresTests(unittest.TestCase):
    def test_format_timestamp_uses_winter_pst(self) -> None:
        self.assertEqual(
            aimlab_scores._format_timestamp("2026-01-15T08:00:00Z"),
            "2026-01-15 00:00:00 PST",
        )

    def test_format_timestamp_uses_summer_pdt(self) -> None:
        self.assertEqual(
            aimlab_scores._format_timestamp("2026-07-15T08:00:00Z"),
            "2026-07-15 01:00:00 PDT",
        )

    def test_format_timestamp_falls_back_to_utc_without_timezone_data(self) -> None:
        aimlab_scores._local_timezone.cache_clear()
        try:
            with patch("aimlab_scores.ZoneInfo", side_effect=ZoneInfoNotFoundError):
                with patch("sys.stderr", new=io.StringIO()) as stderr:
                    self.assertEqual(
                        aimlab_scores._format_timestamp("2026-01-15T08:00:00Z"),
                        "2026-01-15 08:00:00 UTC",
                    )
                    self.assertIn("timezone data is unavailable", stderr.getvalue())
        finally:
            aimlab_scores._local_timezone.cache_clear()

    def test_format_table_handles_ok_and_error_rows(self) -> None:
        output = aimlab_scores.format_table(
            [
                {
                    "ok": True,
                    "name": "Floatshot",
                    "category": "Flick-tech",
                    "sub": "Dynamic",
                    "task_id": "CsLevel.Lowgravity56.VT Float.RSM6A6",
                    "score": 450,
                    "voltaic_rank": "Iron",
                    "next_rank": "Bronze",
                    "next_rank_target_score": 500,
                    "next_rank_progress_percent": 50.0,
                    "voltaic_energy": 150,
                    "accuracy": 99.0,
                    "rank": 123,
                    "ended_at": None,
                    "error": None,
                },
                {
                    "ok": False,
                    "name": "Angleshot",
                    "category": "Flick-tech",
                    "sub": "Dynamic",
                    "task_id": "CsLevel.Lowgravity56.VT Angle.ROAHX3",
                    "score": None,
                    "voltaic_rank": None,
                    "next_rank": None,
                    "next_rank_target_score": None,
                    "next_rank_progress_percent": None,
                    "voltaic_energy": None,
                    "accuracy": None,
                    "rank": None,
                    "ended_at": None,
                    "error": "request failed",
                },
            ]
        )

        self.assertIn("50.0% to Bronze (target 500)", output)
        self.assertIn("Score Rank", output)
        self.assertIn("request failed", output)
        self.assertIn("1/2 scenarios returned a score.", output)

    def test_main_returns_nonzero_when_all_fetches_fail(self) -> None:
        failed_row = {
            "ok": False,
            "difficulty": "novice",
            "category": "Flick-tech",
            "sub": "Dynamic",
            "name": "Floatshot",
            "task_id": "CsLevel.Lowgravity56.VT Float.RSM6A6",
            "error": "request failed",
        }

        with patch("aimlab_scores._fetch_scores_with_timing", return_value=[failed_row]):
            with patch("sys.stdout", new=io.StringIO()):
                with patch("sys.stderr", new=io.StringIO()):
                    exit_code = aimlab_scores.main(
                        [
                            "--user-id",
                            "ABCDEF1234567890",
                            "--difficulty",
                            "novice",
                            "--scenario",
                            "floatshot",
                        ]
                    )

        self.assertEqual(exit_code, 1)

    def test_atomic_write_replaces_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "scores.json"
            aimlab_scores._write_text_atomic(str(output_path), "first")
            aimlab_scores._write_text_atomic(str(output_path), "second")

            self.assertEqual(output_path.read_text(encoding="utf-8"), "second")


if __name__ == "__main__":
    unittest.main()
