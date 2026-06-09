import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import cli
import play_store

ACCOUNT_ID = "acct-1"
# Known task_id from the bundled valorant_s1 resource (resolves to "Floatshot").
KNOWN_TASK_ID = "CsLevel.Lowgravity56.VT Float.RSM6A6"
SEEN_AT = "2026-06-06T00:00:00.000Z"

# The report path must never touch sync/auth/network modules (design §10/§11).
BLOCKED_MODULES = ("aimlabs_auth", "aimlabs_client", "aimlabs_history", "history_sync", "requests")


class CliReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(self._temp_dir.cleanup)
        self.temp_path = Path(self._temp_dir.name)
        self.db_path = self.temp_path / "aimlabs.db"
        self.config_path = _write_config(self.temp_path, self.db_path)

    def test_report_renders_runs_from_store(self) -> None:
        _seed_store(
            self.db_path,
            [_raw_play("play-1", KNOWN_TASK_ID, "2026-06-05T10:00:00.000Z", 1234)],
        )

        exit_code, stdout, _ = _run_cli(["--config", str(self.config_path), "report"])

        self.assertEqual(exit_code, 0)
        self.assertIn("Floatshot", stdout)
        self.assertIn("1234", stdout)
        self.assertIn("All times shown in UTC.", stdout)

    def test_report_missing_user_id_fails_with_message(self) -> None:
        config_path = self.temp_path / "no_user.toml"
        config_path.write_text('[report]\ntimezone = "UTC"\n', encoding="utf-8")

        exit_code, _, stderr = _run_cli(["--config", str(config_path), "report"])

        self.assertEqual(exit_code, 1)
        self.assertIn("[aimlabs].user_id", stderr)

    def test_report_with_missing_store_prints_no_runs_found_without_creating_db(self) -> None:
        missing_db_path = self.temp_path / "missing" / "aimlabs.db"
        config_path = _write_config(self.temp_path, missing_db_path, filename="missing_db.toml")

        exit_code, stdout, _ = _run_cli(["--config", str(config_path), "report"])

        self.assertEqual(exit_code, 0)
        self.assertIn("no runs found", stdout)
        self.assertFalse(missing_db_path.exists())

    def test_report_empty_store_prints_no_runs_found(self) -> None:
        _seed_store(self.db_path, [])

        exit_code, stdout, _ = _run_cli(["--config", str(self.config_path), "report"])

        self.assertEqual(exit_code, 0)
        self.assertIn("no runs found", stdout)

    def test_report_path_performs_no_auth_or_network(self) -> None:
        _seed_store(
            self.db_path,
            [_raw_play("play-1", KNOWN_TASK_ID, "2026-06-05T10:00:00.000Z", 1234)],
        )
        saved_modules = {name: sys.modules.pop(name) for name in BLOCKED_MODULES if name in sys.modules}
        try:
            with mock.patch("socket.socket", side_effect=AssertionError("report path must not open sockets")):
                exit_code, stdout, _ = _run_cli(["--config", str(self.config_path), "report"])
            self.assertEqual(exit_code, 0)
            self.assertIn("Floatshot", stdout)
            for module_name in BLOCKED_MODULES:
                self.assertNotIn(module_name, sys.modules, f"report path imported {module_name}")
        finally:
            sys.modules.update(saved_modules)

    def test_report_include_all_statuses_flag_includes_flagged_runs(self) -> None:
        _seed_store(
            self.db_path,
            [
                _raw_play("play-1", KNOWN_TASK_ID, "2026-06-05T10:00:00.000Z", 100),
                _raw_play("play-2", KNOWN_TASK_ID, "2026-06-05T11:00:00.000Z", 9999, status="PENDING"),
            ],
        )

        default_exit_code, default_stdout, _ = _run_cli(["--config", str(self.config_path), "report"])
        override_exit_code, override_stdout, _ = _run_cli(
            ["--config", str(self.config_path), "report", "--include-all-statuses"]
        )

        self.assertEqual(default_exit_code, 0)
        self.assertNotIn("9999", default_stdout)
        self.assertIn("1 non-APPROVED run excluded", default_stdout)
        self.assertEqual(override_exit_code, 0)
        self.assertIn("9999", override_stdout)
        self.assertIn("[PENDING]", override_stdout)

    def test_verbose_reports_loaded_play_count_to_stderr(self) -> None:
        _seed_store(
            self.db_path,
            [
                _raw_play("play-1", KNOWN_TASK_ID, "2026-06-05T10:00:00.000Z", 100),
                _raw_play("play-2", KNOWN_TASK_ID, "2026-06-05T11:00:00.000Z", 200),
            ],
        )

        exit_code, _, stderr = _run_cli(["--config", str(self.config_path), "--verbose", "report"])

        self.assertEqual(exit_code, 0)
        self.assertIn("loaded 2 plays from", stderr)

    def test_missing_command_exits_with_usage_error(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                cli.main([])

        self.assertEqual(raised.exception.code, 2)


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer):
        exit_code = cli.main(argv)
    return exit_code, stdout_buffer.getvalue(), stderr_buffer.getvalue()


def _write_config(temp_path: Path, db_path: Path, *, filename: str = "config.toml") -> Path:
    config_path = temp_path / filename
    db_value = str(db_path).replace("\\", "/")
    config_path.write_text(
        f'[aimlabs]\nuser_id = "{ACCOUNT_ID}"\n\n[storage]\ndb_path = "{db_value}"\n\n[report]\ntimezone = "UTC"\n',
        encoding="utf-8",
    )
    return config_path


def _seed_store(db_path: Path, raw_plays: list[dict]) -> None:
    connection = play_store.connect(db_path)
    try:
        play_store.upsert_plays(connection, ACCOUNT_ID, raw_plays, seen_at=SEEN_AT)
    finally:
        connection.close()


def _raw_play(play_id: str, task_id: str, ended_at: str, score: float, *, status: str = "APPROVED") -> dict:
    return {
        "id": play_id,
        "endedAt": ended_at,
        "task": {"id": task_id},
        "score": score,
        "manifest": {"playDuration": 60000, "pauseDuration": 0},
        "performanceScores": {"hitsTotal": 45, "shotsTotal": 50},
        "gridshieldStatus": status,
    }


if __name__ == "__main__":
    unittest.main()
