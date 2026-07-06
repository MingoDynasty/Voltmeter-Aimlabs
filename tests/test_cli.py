import io
import hashlib
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Optional
from unittest import mock

import aimlabs_history
import cli
import play_store
import scenario_catalog

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
        self.assertIn("PENDING", override_stdout)

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

    def test_verbose_is_accepted_after_the_subcommand_too(self) -> None:
        _seed_store(
            self.db_path,
            [
                _raw_play("play-1", KNOWN_TASK_ID, "2026-06-05T10:00:00.000Z", 100),
                _raw_play("play-2", KNOWN_TASK_ID, "2026-06-05T11:00:00.000Z", 200),
            ],
        )

        # --config before, --verbose after the subcommand (the natural invocation).
        exit_code, _, stderr = _run_cli(["--config", str(self.config_path), "report", "--verbose"])

        self.assertEqual(exit_code, 0)
        self.assertIn("loaded 2 plays from", stderr)

    def test_config_is_accepted_after_the_subcommand_too(self) -> None:
        # A --config placed AFTER the subcommand must still be honored. This config omits
        # [aimlabs].user_id, so the report fails with that specific message only if the
        # after-positioned --config was actually read (otherwise it would not be loaded at all).
        config_path = self.temp_path / "after_no_user.toml"
        config_path.write_text('[report]\ntimezone = "UTC"\n', encoding="utf-8")

        exit_code, _, stderr = _run_cli(["report", "--config", str(config_path)])

        self.assertEqual(exit_code, 1)
        self.assertIn("[aimlabs].user_id", stderr)

    def test_missing_command_exits_with_usage_error(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                cli.main([])

        self.assertEqual(raised.exception.code, 2)


class CliSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(self._temp_dir.cleanup)
        self.temp_path = Path(self._temp_dir.name)
        self.db_path = self.temp_path / "aimlabs.db"
        # Run from an empty directory so the default ".env" lookup cannot pick up
        # a real credential from the repo checkout, and scrub the env channels.
        self._original_cwd = Path.cwd()
        os.chdir(self.temp_path)
        self.addCleanup(os.chdir, self._original_cwd)
        env_patcher = mock.patch.dict(os.environ)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        os.environ.pop("AIMLAB_SESSION", None)
        self.config_path = self._write_sync_config()

    def _write_sync_config(self, *, delay: float = 0.0, filename: str = "config.toml") -> Path:
        config_path = self.temp_path / filename
        db_value = str(self.db_path).replace("\\", "/")
        config_path.write_text(
            f'[aimlabs]\nuser_id = "{ACCOUNT_ID}"\n\n'
            f'[storage]\ndb_path = "{db_value}"\n\n'
            f"[sync]\nrequest_delay_seconds = {delay}\n\n"
            '[report]\ntimezone = "UTC"\n',
            encoding="utf-8",
        )
        return config_path

    def test_sync_missing_credential_fails_with_login_hint_and_no_window(self) -> None:
        saved_webview = sys.modules.pop("webview", None)
        try:
            with mock.patch("socket.socket", side_effect=AssertionError("sync must not reach the network here")):
                exit_code, _, stderr = _run_cli(["--config", str(self.config_path), "sync"])
            self.assertNotIn("webview", sys.modules, "sync must never import the login window machinery")
        finally:
            if saved_webview is not None:
                sys.modules["webview"] = saved_webview

        self.assertEqual(exit_code, 1)
        self.assertIn("voltmeter login", stderr)

    def test_sync_happy_path_prints_summary_and_stores_plays(self) -> None:
        os.environ["AIMLAB_SESSION"] = "synthetic-cookie"
        page = _history_page([_raw_play("play-1", KNOWN_TASK_ID, "2026-06-05T10:00:00.000Z", 1234)])
        with (
            mock.patch("aimlabs_auth.fetch_session_json", return_value={"accessToken": "bearer-1"}) as session_mock,
            mock.patch("aimlabs_history.fetch_history_page", return_value=page) as fetch_mock,
            mock.patch("aimlabs_history.fetch_practice_contamination_count", return_value=0),
        ):
            exit_code, stdout, stderr = _run_cli(["--config", str(self.config_path), "sync"])

        self.assertEqual(exit_code, 0)
        # initial backfill (1 page) + the post-backfill top sweep (1 page)
        self.assertIn("sync: 2 pages, 1 inserted, 1 skipped; Aimlabs reports 1 plays.", stdout)
        self.assertNotIn("practice", stderr)
        self.assertNotIn("webview", sys.modules)
        self.assertEqual(session_mock.call_count, 1)
        self.assertEqual(fetch_mock.call_count, 2)
        connection = play_store.connect(self.db_path)
        try:
            self.assertEqual(play_store.count_plays(connection, ACCOUNT_ID), 1)
        finally:
            connection.close()

    def test_sync_session_file_flag_is_plumbed_to_session_resolution(self) -> None:
        session_path = self.temp_path / "session.cookie"
        session_path.write_text("file-cookie\n", encoding="utf-8")
        with (
            mock.patch("aimlabs_auth.fetch_session_json", return_value={"accessToken": "bearer-1"}) as session_mock,
            mock.patch("aimlabs_history.fetch_history_page", return_value=_history_page([])),
            mock.patch("aimlabs_history.fetch_practice_contamination_count", return_value=0),
        ):
            exit_code, _, stderr = _run_cli(
                ["--config", str(self.config_path), "--verbose", "sync", "--session-file", str(session_path)]
            )

        self.assertEqual(exit_code, 0)
        session_mock.assert_called_once()
        self.assertEqual(session_mock.call_args[0][0], "file-cookie")
        # the path (never the value) is reported on the verbose channel
        self.assertIn(str(session_path), stderr)
        self.assertNotIn("file-cookie", stderr)

    def test_sync_rejects_a_literal_secret_flag(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                cli.main(["sync", "--session", "literal-secret"])

        self.assertEqual(raised.exception.code, 2)

    def test_sync_show_deleted_without_full_is_rejected(self) -> None:
        exit_code, _, stderr = _run_cli(["--config", str(self.config_path), "sync", "--show-deleted"])

        self.assertEqual(exit_code, 1)
        self.assertIn("--show-deleted requires --full", stderr)

    def test_sync_remints_bearer_after_unauthorized_page(self) -> None:
        os.environ["AIMLAB_SESSION"] = "synthetic-cookie"
        with (
            mock.patch(
                "aimlabs_auth.fetch_session_json",
                side_effect=[{"accessToken": "bearer-1"}, {"accessToken": "bearer-2"}],
            ) as session_mock,
            mock.patch(
                "aimlabs_history.fetch_history_page",
                side_effect=[aimlabs_history.AimlabsUnauthorizedError("expired bearer"), _history_page([])],
            ) as fetch_mock,
            mock.patch("aimlabs_history.fetch_practice_contamination_count", return_value=0) as contamination_mock,
        ):
            exit_code, _, _ = _run_cli(["--config", str(self.config_path), "sync"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(session_mock.call_count, 2)
        self.assertEqual(fetch_mock.call_count, 2)
        # the retried fetch and the contamination check both use the re-minted bearer
        self.assertEqual(fetch_mock.call_args[0][1], "bearer-2")
        self.assertEqual(contamination_mock.call_args[0][1], "bearer-2")

    def test_sync_relogin_required_is_terminal_with_clear_message(self) -> None:
        os.environ["AIMLAB_SESSION"] = "synthetic-cookie"
        with (
            mock.patch(
                "aimlabs_auth.fetch_session_json",
                side_effect=[
                    {"accessToken": "bearer-1"},
                    {"accessTokenError": "RefreshAccessTokenError"},
                ],
            ) as session_mock,
            mock.patch(
                "aimlabs_history.fetch_history_page",
                side_effect=aimlabs_history.AimlabsUnauthorizedError("expired bearer"),
            ) as fetch_mock,
        ):
            exit_code, _, stderr = _run_cli(["--config", str(self.config_path), "sync"])

        self.assertEqual(exit_code, 1)
        self.assertIn("voltmeter login", stderr)
        # initial mint + one failed re-mint; terminal, not a retry loop
        self.assertEqual(session_mock.call_count, 2)
        self.assertEqual(fetch_mock.call_count, 1)

    def test_sync_report_offline_half_adds_no_auth_or_network(self) -> None:
        os.environ["AIMLAB_SESSION"] = "synthetic-cookie"
        page = _history_page([_raw_play("play-1", KNOWN_TASK_ID, "2026-06-05T10:00:00.000Z", 1234)])
        with (
            mock.patch("aimlabs_auth.fetch_session_json", return_value={"accessToken": "bearer-1"}) as session_mock,
            mock.patch("aimlabs_history.fetch_history_page", return_value=page) as fetch_mock,
            mock.patch("aimlabs_history.fetch_practice_contamination_count", return_value=0) as contamination_mock,
            mock.patch("socket.socket", side_effect=AssertionError("sync --report must not open sockets")),
        ):
            exit_code, stdout, _ = _run_cli(["--config", str(self.config_path), "sync", "--report"])

        self.assertEqual(exit_code, 0)
        self.assertIn("Floatshot", stdout)
        self.assertIn("All times shown in UTC.", stdout)
        # the reporting half added no auth or network calls beyond the sync itself
        self.assertEqual(session_mock.call_count, 1)
        self.assertEqual(fetch_mock.call_count, 2)
        self.assertEqual(contamination_mock.call_count, 1)

    def test_sync_sleeps_between_pages_per_request_delay(self) -> None:
        os.environ["AIMLAB_SESSION"] = "synthetic-cookie"
        config_path = self._write_sync_config(delay=0.5, filename="delay.toml")
        newest_play = _raw_play("play-2", KNOWN_TASK_ID, "2026-06-05T11:00:00.000Z", 200)
        older_play = _raw_play("play-1", KNOWN_TASK_ID, "2026-06-05T10:00:00.000Z", 100)
        pages = [
            _history_page([newest_play], total_count=2, has_next_page=True, end_cursor="cursor-1"),
            _history_page([older_play], total_count=2),
            _history_page([newest_play], total_count=2),  # top sweep stops on the anchor
        ]
        with (
            mock.patch("aimlabs_auth.fetch_session_json", return_value={"accessToken": "bearer-1"}),
            mock.patch("aimlabs_history.fetch_history_page", side_effect=pages) as fetch_mock,
            mock.patch("aimlabs_history.fetch_practice_contamination_count", return_value=0),
            mock.patch("time.sleep") as sleep_mock,
        ):
            exit_code, _, _ = _run_cli(["--config", str(config_path), "sync"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(fetch_mock.call_count, 3)
        self.assertEqual(sleep_mock.call_args_list, [mock.call(0.5), mock.call(0.5)])

    def test_sync_warns_on_practice_contamination(self) -> None:
        os.environ["AIMLAB_SESSION"] = "synthetic-cookie"
        with (
            mock.patch("aimlabs_auth.fetch_session_json", return_value={"accessToken": "bearer-1"}),
            mock.patch("aimlabs_history.fetch_history_page", return_value=_history_page([])),
            mock.patch("aimlabs_history.fetch_practice_contamination_count", return_value=3),
        ):
            exit_code, _, stderr = _run_cli(["--config", str(self.config_path), "sync"])

        self.assertEqual(exit_code, 0)
        self.assertIn("3 practice plays found in the mode-42 stream", stderr)

    def test_sync_contamination_check_failure_warns_and_continues(self) -> None:
        os.environ["AIMLAB_SESSION"] = "synthetic-cookie"
        with (
            mock.patch("aimlabs_auth.fetch_session_json", return_value={"accessToken": "bearer-1"}),
            mock.patch("aimlabs_history.fetch_history_page", return_value=_history_page([])),
            mock.patch(
                "aimlabs_history.fetch_practice_contamination_count",
                side_effect=aimlabs_history.AimlabsTransientHistoryError(503, "maintenance"),
            ),
        ):
            exit_code, stdout, stderr = _run_cli(["--config", str(self.config_path), "sync"])

        self.assertEqual(exit_code, 0)
        self.assertIn("practice-contamination check failed", stderr)
        self.assertIn("sync:", stdout)

    def test_sync_full_reports_summary_and_show_deleted_names_plays(self) -> None:
        os.environ["AIMLAB_SESSION"] = "synthetic-cookie"
        kept_play = _raw_play("play-kept", KNOWN_TASK_ID, "2026-06-05T10:00:00.000Z", 100)
        _seed_store(
            self.db_path,
            [kept_play, _raw_play("play-gone", KNOWN_TASK_ID, "2026-06-04T10:00:00.000Z", 90)],
        )
        with (
            mock.patch("aimlabs_auth.fetch_session_json", return_value={"accessToken": "bearer-1"}),
            mock.patch("aimlabs_history.fetch_history_page", return_value=_history_page([kept_play], total_count=1)),
            mock.patch("aimlabs_history.fetch_practice_contamination_count", return_value=0),
        ):
            exit_code, stdout, stderr = _run_cli(
                ["--config", str(self.config_path), "sync", "--full", "--show-deleted"]
            )

        self.assertEqual(exit_code, 0)
        self.assertIn("sync --full: 1 pages, 0 inserted, 1 updated; Aimlabs reports 1 plays.", stdout)
        self.assertIn("play-gone", stderr)


class CliLoginTests(unittest.TestCase):
    def test_login_passes_timeout_and_maps_capture_success_to_exit_zero(self) -> None:
        with mock.patch("aimlabs_auth.login_and_capture", return_value="captured-session") as capture_mock:
            exit_code, _, _ = _run_cli(["login", "--timeout", "42"])

        self.assertEqual(exit_code, 0)
        capture_mock.assert_called_once_with(timeout=42.0)

    def test_login_capture_failure_exits_nonzero(self) -> None:
        with mock.patch("aimlabs_auth.login_and_capture", return_value=None):
            exit_code, _, _ = _run_cli(["login"])

        self.assertEqual(exit_code, 1)


class CliRefreshCatalogTests(unittest.TestCase):
    def test_refresh_catalog_prints_summary_and_clears_the_load_cache(self) -> None:
        scenario_catalog.load_catalog()  # populate the lru cache

        exit_code, stdout, _ = _run_cli(["refresh-catalog"])

        self.assertEqual(exit_code, 0)
        self.assertRegex(stdout, r"catalog: \d+ records across \d+ families")
        self.assertRegex(stdout, r"\d+ duplicate task ids\.")
        self.assertEqual(scenario_catalog.load_catalog.cache_info().currsize, 0)


class CliScoresTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(self._temp_dir.cleanup)
        self.temp_path = Path(self._temp_dir.name)
        self.config_path = self.temp_path / "config.toml"
        self.config_path.write_text(f'[aimlabs]\nuser_id = "{ACCOUNT_ID}"\n', encoding="utf-8")
        self._original_cwd = Path.cwd()
        os.chdir(self.temp_path)
        self.addCleanup(os.chdir, self._original_cwd)

    def test_scores_table_matches_standalone_golden_without_login(self) -> None:
        expected_lines = json.loads(
            (Path(__file__).parent / "fixtures" / "scores_table_golden.json").read_text(encoding="utf-8")
        )
        expected_stdout = "\n".join(expected_lines) + "\n"
        seen_headers: list[Optional[dict]] = []

        def fake_fetch_all_scores(
            *,
            user_id: str,
            scenarios: list[dict],
            extra_headers: Optional[dict],
            **_kwargs: object,
        ) -> list[dict]:
            self.assertEqual(user_id, ACCOUNT_ID)
            seen_headers.append(extra_headers)
            return [_synthetic_score_row(scenario) for scenario in scenarios]

        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("aimlab_scores.fetch_all_scores", side_effect=fake_fetch_all_scores),
            mock.patch("aimlab_scores.Stopwatch.elapsed", return_value=0.5),
        ):
            standalone_exit, standalone_stdout, standalone_stderr = _run_scores_main(
                [
                    "--config",
                    str(self.config_path),
                    "--scenario",
                    "floatshot",
                ]
            )
            cli_exit, cli_stdout, cli_stderr = _run_cli(
                [
                    "--verbose",
                    "scores",
                    "--config",
                    str(self.config_path),
                    "--scenario",
                    "floatshot",
                ]
            )

        self.assertEqual(standalone_exit, 0)
        self.assertEqual(cli_exit, 0)
        self.assertEqual(standalone_stdout, expected_stdout)
        self.assertEqual(cli_stdout, expected_stdout)
        self.assertEqual(cli_stderr, standalone_stderr)
        self.assertEqual(seen_headers, [None] * 6)
        self.assertNotIn("webview", sys.modules, "scores must never import the login window machinery")

    def test_scores_json_matches_standalone_golden_with_global_options_after_command(self) -> None:
        expected_stdout = (Path(__file__).parent / "fixtures" / "scores_json_golden.json").read_text(encoding="utf-8")

        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch(
                "aimlab_scores.fetch_all_scores",
                side_effect=lambda *, scenarios, **_kwargs: [_synthetic_score_row(scenario) for scenario in scenarios],
            ),
            mock.patch("aimlab_scores.Stopwatch.elapsed", return_value=0.5),
        ):
            standalone_exit, standalone_stdout, _ = _run_scores_main(
                [
                    "--config",
                    str(self.config_path),
                    "--difficulty",
                    "novice",
                    "--scenario",
                    "floatshot",
                    "--json",
                ]
            )
            cli_exit, cli_stdout, _ = _run_cli(
                [
                    "scores",
                    "--config",
                    str(self.config_path),
                    "--verbose",
                    "--difficulty",
                    "novice",
                    "--scenario",
                    "floatshot",
                    "--json",
                ]
            )

        self.assertEqual(standalone_exit, 0)
        self.assertEqual(cli_exit, 0)
        self.assertEqual(standalone_stdout, expected_stdout)
        self.assertEqual(cli_stdout, expected_stdout)

    def test_scores_full_catalog_matches_golden_digests_without_login(self) -> None:
        golden = json.loads(
            (Path(__file__).parent / "fixtures" / "scores_full_catalog_golden.json").read_text(encoding="utf-8")
        )
        fetched_row_counts: list[int] = []

        def fake_fetch_all_scores(
            *,
            user_id: str,
            scenarios: list[dict],
            extra_headers: Optional[dict],
            **_kwargs: object,
        ) -> list[dict]:
            self.assertEqual(user_id, ACCOUNT_ID)
            self.assertIsNone(extra_headers)
            fetched_row_counts.append(len(scenarios))
            return [_synthetic_score_row(scenario) for scenario in scenarios]

        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("aimlab_scores.fetch_all_scores", side_effect=fake_fetch_all_scores),
            mock.patch("aimlab_scores.Stopwatch.elapsed", return_value=0.5),
        ):
            standalone_table_exit, standalone_table, standalone_table_stderr = _run_scores_main(
                ["--config", str(self.config_path)]
            )
            cli_table_exit, cli_table, cli_table_stderr = _run_cli(["scores", "--config", str(self.config_path)])
            standalone_json_exit, standalone_json, standalone_json_stderr = _run_scores_main(
                ["--config", str(self.config_path), "--json"]
            )
            cli_json_exit, cli_json, cli_json_stderr = _run_cli(["scores", "--config", str(self.config_path), "--json"])

        self.assertEqual(
            (standalone_table_exit, cli_table_exit, standalone_json_exit, cli_json_exit),
            (0, 0, 0, 0),
        )
        self.assertEqual(cli_table, standalone_table)
        self.assertEqual(cli_table_stderr, standalone_table_stderr)
        self.assertEqual(cli_json, standalone_json)
        self.assertEqual(cli_json_stderr, standalone_json_stderr)
        self.assertEqual(fetched_row_counts, [21] * 12)
        self.assertEqual(len(json.loads(cli_json)), golden["catalog_rows"])
        self.assertEqual(_sha256(cli_table), golden["table_sha256"])
        self.assertEqual(_sha256(cli_json), golden["json_sha256"])

    def test_scores_forwards_the_full_argument_surface(self) -> None:
        output_path = self.temp_path / "scores.json"
        with mock.patch("aimlab_scores.main", return_value=0) as scores_main:
            exit_code, _, _ = _run_cli(
                [
                    "--config",
                    str(self.config_path),
                    "scores",
                    "--user-id",
                    "ABCDEF1234567890",
                    "--difficulty",
                    "advanced",
                    "--scenario",
                    "floatshot",
                    "--json",
                    "--raw",
                    "--out",
                    str(output_path),
                    "--source",
                    "network",
                    "--timeout",
                    "7",
                    "--request-delay",
                    "0.5",
                    "--run-deadline",
                    "30",
                ]
            )

        self.assertEqual(exit_code, 0)
        scores_main.assert_called_once_with(
            [
                "--difficulty",
                "advanced",
                "--source",
                "network",
                "--timeout",
                "7.0",
                "--request-delay",
                "0.5",
                "--config",
                str(self.config_path),
                "--user-id",
                "ABCDEF1234567890",
                "--scenario",
                "floatshot",
                "--json",
                "--raw",
                "--out",
                str(output_path),
                "--run-deadline",
                "30.0",
            ]
        )

    def test_scores_help_is_listed_and_header_flag_is_retired(self) -> None:
        import aimlab_scores  # pylint: disable=import-outside-toplevel

        top_level_help = io.StringIO()
        with redirect_stdout(top_level_help), self.assertRaises(SystemExit) as top_level_exit:
            cli.main(["--help"])
        self.assertEqual(top_level_exit.exception.code, 0)
        self.assertIn("scores", top_level_help.getvalue())

        scores_help = io.StringIO()
        with redirect_stdout(scores_help), self.assertRaises(SystemExit) as scores_help_exit:
            cli.main(["scores", "--help"])
        self.assertEqual(scores_help_exit.exception.code, 0)
        self.assertNotIn("--header", scores_help.getvalue())

        standalone_help = io.StringIO()
        with redirect_stdout(standalone_help), self.assertRaises(SystemExit) as standalone_help_exit:
            aimlab_scores.main(["--help"])
        self.assertEqual(standalone_help_exit.exception.code, 0)
        self.assertNotIn("--header", standalone_help.getvalue())

        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.main(["scores", "--header", "Cookie: secret"])
            with self.assertRaises(SystemExit):
                aimlab_scores.main(["--header", "Cookie: secret"])


def _history_page(
    plays: list[dict],
    *,
    total_count: Optional[int] = None,
    has_next_page: bool = False,
    end_cursor: Optional[str] = None,
) -> aimlabs_history.HistoryPage:
    return aimlabs_history.HistoryPage(
        plays=tuple(plays),
        total_count=len(plays) if total_count is None else total_count,
        has_next_page=has_next_page,
        end_cursor=end_cursor,
    )


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer):
        exit_code = cli.main(argv)
    return exit_code, stdout_buffer.getvalue(), stderr_buffer.getvalue()


def _run_scores_main(argv: list[str]) -> tuple[int, str, str]:
    import aimlab_scores  # pylint: disable=import-outside-toplevel

    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer):
        exit_code = aimlab_scores.main(argv)
    return exit_code, stdout_buffer.getvalue(), stderr_buffer.getvalue()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _synthetic_score_row(scenario: dict) -> dict:
    scores = {
        "novice": 450,
        "intermediate": 900,
        "advanced": 1350,
    }
    difficulty = scenario["difficulty"]
    return {
        "key": scenario["key"],
        "name": scenario["name"],
        "category": scenario["category"],
        "sub": scenario["sub"],
        "difficulty": difficulty,
        "task_id": scenario["task_id"],
        "weapon_id": scenario["weapon_id"],
        "ok": True,
        "score": scores[difficulty],
        "accuracy": 87.5,
        "rank": 123,
        "ended_at": "2026-07-05T19:30:00Z",
        "error": None,
        "raw": {"synthetic": True},
    }


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
