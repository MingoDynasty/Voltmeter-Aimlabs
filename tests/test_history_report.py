import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from history_report import HistoryReportError, build_report, render_report
from scenario_catalog import ScenarioCatalog, refresh_catalog

ACCOUNT_ID = "acct-1"
VALORANT_TASK = "task-valorant-adjust"
AIMLABS_S2_TASK = "task-s2-angle"
AIMLABS_S3_TASK = "task-s3-angle"
UNKNOWN_TASK = "task-unknown-swipe"
BASE_TIME = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)


class HistoryReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = _synthetic_catalog()

    def test_empty_store_renders_no_runs_found(self) -> None:
        report = build_report([], self.catalog)

        text = render_report(report, timezone_setting="UTC")

        self.assertEqual(report.table_rows, ())
        self.assertEqual(report.task_summaries, ())
        self.assertIn("no runs found", text)

    def test_non_approved_excluded_by_default_with_visible_note(self) -> None:
        rows = [
            _play_row("play-1", VALORANT_TASK, idx=1, score=100),
            _play_row("play-2", VALORANT_TASK, idx=2, score=200),
            _play_row("play-3", VALORANT_TASK, idx=3, score=9999, status="PENDING"),
        ]

        report = build_report(rows, self.catalog)
        text = render_report(report, timezone_setting="UTC")

        self.assertEqual(len(report.table_rows), 2)
        self.assertEqual(report.excluded_non_approved_count, 1)
        self.assertEqual(report.task_summaries[0].personal_best, 200)
        self.assertIn("1 non-APPROVED run excluded", text)
        self.assertNotIn("9999", text)
        self.assertNotIn("Status", text)  # all rendered rows are APPROVED, so no Status column

    def test_include_all_statuses_override_includes_flagged_runs(self) -> None:
        rows = [
            _play_row("play-1", VALORANT_TASK, idx=1, score=100),
            _play_row("play-2", VALORANT_TASK, idx=2, score=9999, status="PENDING"),
        ]

        report = build_report(rows, self.catalog, include_all_statuses=True)
        text = render_report(report, timezone_setting="UTC")

        self.assertEqual(len(report.table_rows), 2)
        self.assertEqual(report.excluded_non_approved_count, 0)
        self.assertEqual(report.task_summaries[0].personal_best, 9999)
        self.assertIn("including 1 non-APPROVED run", text)
        self.assertIn("Status", text)
        self.assertIn("PENDING", text)

    def test_related_scenarios_different_task_ids_bucketed_separately(self) -> None:
        rows = [
            _play_row("play-s2", AIMLABS_S2_TASK, idx=1, score=100),
            _play_row("play-s3", AIMLABS_S3_TASK, idx=2, score=300),
        ]

        report = build_report(rows, self.catalog)

        self.assertEqual(len(report.task_summaries), 2)
        summaries_by_task = {summary.task_id: summary for summary in report.task_summaries}
        # The season/difficulty qualifier is retained so sibling scenarios stay distinguishable.
        self.assertEqual(summaries_by_task[AIMLABS_S2_TASK].scenario_name, "Angle Novice")
        self.assertEqual(summaries_by_task[AIMLABS_S3_TASK].scenario_name, "Angle Novice S3")
        self.assertEqual(summaries_by_task[AIMLABS_S2_TASK].personal_best, 100)
        self.assertEqual(summaries_by_task[AIMLABS_S3_TASK].personal_best, 300)
        self.assertNotEqual(
            summaries_by_task[AIMLABS_S2_TASK].benchmark_alias,
            summaries_by_task[AIMLABS_S3_TASK].benchmark_alias,
        )

    def test_rolling_stats_and_date_range_over_most_recent_plays(self) -> None:
        rows = [
            _play_row(f"play-{idx}", VALORANT_TASK, idx=idx, score=31 - idx)
            for idx in range(1, 31)  # newest play (idx 30) has the LOWEST score
        ]

        report = build_report(rows, self.catalog, rolling_median_window=10, rolling_max_window=25)

        summary = report.task_summaries[0]
        self.assertEqual(summary.play_count, 30)
        self.assertEqual(summary.personal_best, 30)
        self.assertEqual(summary.mean_score, 15.5)
        self.assertEqual(summary.median_score, 15.5)
        self.assertEqual(summary.rolling_median, 5.5)  # median of the 10 most recent scores 1..10
        self.assertEqual(summary.rolling_max, 25)  # max of the 25 most recent scores 1..25
        self.assertEqual(summary.first_ended_at_utc, _ended_at(1))
        self.assertEqual(summary.last_ended_at_utc, _ended_at(30))

    def test_rolling_windows_with_fewer_plays_than_window_use_what_exists(self) -> None:
        rows = [
            _play_row("play-1", VALORANT_TASK, idx=1, score=10),
            _play_row("play-2", VALORANT_TASK, idx=2, score=20),
            _play_row("play-3", VALORANT_TASK, idx=3, score=30),
        ]

        report = build_report(rows, self.catalog, rolling_median_window=10, rolling_max_window=25)

        summary = report.task_summaries[0]
        self.assertEqual(summary.rolling_median, 20)
        self.assertEqual(summary.rolling_max, 30)

    def test_family_all_includes_known_s1_s2_s3_and_footers_unknown(self) -> None:
        rows = [
            _play_row("play-1", VALORANT_TASK, idx=1, score=100),
            _play_row("play-2", AIMLABS_S2_TASK, idx=2, score=200),
            _play_row("play-3", AIMLABS_S3_TASK, idx=3, score=300),
            _play_row("play-4", UNKNOWN_TASK, idx=4, score=400),
        ]

        report = build_report(rows, self.catalog, family="all")
        text = render_report(report, timezone_setting="UTC")

        self.assertEqual(len(report.table_rows), 3)
        self.assertEqual(
            {play.task_id for play in report.table_rows}, {VALORANT_TASK, AIMLABS_S2_TASK, AIMLABS_S3_TASK}
        )
        self.assertEqual(report.other.scenario_count, 1)
        self.assertEqual(report.other.play_count, 1)
        self.assertNotIn("Unknown scenario", text)
        self.assertIn("+1 scenario, 1 play, untracked", text)

    def test_family_valorant_restricts_main_table_and_footers_other(self) -> None:
        rows = [
            _play_row("play-1", VALORANT_TASK, idx=1, score=100),
            _play_row("play-2", AIMLABS_S2_TASK, idx=2, score=200),
            _play_row("play-3", AIMLABS_S2_TASK, idx=3, score=250),
            _play_row("play-4", UNKNOWN_TASK, idx=4, score=400),
        ]

        report = build_report(rows, self.catalog, family="valorant")
        text = render_report(report, timezone_setting="UTC")

        self.assertEqual([play.task_id for play in report.table_rows], [VALORANT_TASK])
        self.assertEqual(report.other.scenario_count, 2)
        self.assertEqual(report.other.play_count, 3)
        self.assertIn("+2 scenarios, 3 plays, untracked", text)

    def test_unknown_task_ids_are_retained_in_footer_even_when_table_is_empty(self) -> None:
        rows = [
            _play_row("play-1", UNKNOWN_TASK, idx=1, score=100),
            _play_row("play-2", UNKNOWN_TASK, idx=2, score=200),
        ]

        report = build_report(rows, self.catalog)
        text = render_report(report, timezone_setting="UTC")

        self.assertEqual(report.table_rows, ())
        self.assertEqual(report.other.scenario_count, 1)
        self.assertEqual(report.other.play_count, 2)
        self.assertIn("no runs found", text)
        self.assertIn("+1 scenario, 2 plays, untracked", text)

    def test_timezone_label_present_and_utc_timestamps_render_in_selected_zone(self) -> None:
        rows = [_play_row("play-1", VALORANT_TASK, ended_at="2026-06-06T02:43:54.249Z", score=100)]

        report = build_report(rows, self.catalog)
        text = render_report(report, timezone_setting="America/Los_Angeles")

        self.assertIn("All times shown in America/Los_Angeles.", text)
        self.assertIn("2026-06-05 19:43:54", text)  # UTC-7 (PDT) at that instant

    def test_local_timezone_setting_still_labels_the_zone(self) -> None:
        rows = [_play_row("play-1", VALORANT_TASK, idx=1, score=100)]

        report = build_report(rows, self.catalog)
        text = render_report(report, timezone_setting="local")

        self.assertIn("All times shown in local (", text)

    def test_scenario_stats_render_the_keys_present_per_play(self) -> None:
        rows = [
            _play_row(
                "play-1",
                VALORANT_TASK,
                idx=1,
                score=100,
                performance_scores={"accuracy": 85.25, "hitsTotal": 120},
            ),
            _play_row(
                "play-2",
                VALORANT_TASK,
                idx=2,
                score=200,
                performance_scores={"timeOnTargetMs": 5000},
            ),
        ]

        report = build_report(rows, self.catalog)
        text = render_report(report, timezone_setting="UTC")

        header_line = next(line for line in text.splitlines() if line.startswith("Date"))
        self.assertIn("accuracy", header_line)
        self.assertIn("hitsTotal", header_line)
        self.assertIn("timeOnTargetMs", header_line)
        clicking_line = next(line for line in text.splitlines() if "85.25" in line)
        self.assertIn("120", clicking_line)
        self.assertTrue(clicking_line.endswith("-"))  # no timeOnTargetMs metric on the clicking play
        tracking_line = next(line for line in text.splitlines() if "5000" in line)
        self.assertIn("| -", tracking_line)  # no accuracy/hitsTotal metrics on the tracking play

    def test_table_rows_are_reverse_chronological(self) -> None:
        rows = [
            _play_row("play-old", VALORANT_TASK, idx=1, score=100),
            _play_row("play-new", VALORANT_TASK, idx=2, score=200),
        ]

        report = build_report(reversed(rows), self.catalog)

        self.assertEqual([play.ended_at_utc for play in report.table_rows], [_ended_at(2), _ended_at(1)])

    def test_invalid_family_raises_history_report_error(self) -> None:
        with self.assertRaisesRegex(HistoryReportError, "family"):
            build_report([], self.catalog, family="kovaaks")

    def test_non_positive_rolling_window_raises_history_report_error(self) -> None:
        with self.assertRaisesRegex(HistoryReportError, "rolling"):
            build_report([], self.catalog, rolling_median_window=0)

    def test_invalid_timezone_raises_history_report_error_at_render(self) -> None:
        report = build_report([], self.catalog)

        with self.assertRaisesRegex(HistoryReportError, "timezone"):
            render_report(report, timezone_setting="Not/AZone")


def _synthetic_catalog() -> ScenarioCatalog:
    with tempfile.TemporaryDirectory() as temp_dir:
        resource_dir = Path(temp_dir)
        _write_resource(
            resource_dir,
            "valorant_s1.json",
            alias="valorant_s1",
            benchmark_name="Voltaic Valorant Benchmarks",
            season="1",
            scenarios=[_scenario("VT Adjust VALORANT Easy", VALORANT_TASK)],
        )
        _write_resource(
            resource_dir,
            "aimlabs_s2.json",
            alias="aimlabs_s2",
            benchmark_name="Voltaic Aimlabs Benchmarks",
            season="2",
            has_leaderboards=False,
            scenarios=[_scenario("VT Angle Novice", AIMLABS_S2_TASK)],
        )
        _write_resource(
            resource_dir,
            "aimlabs_s3.json",
            alias="aimlabs_s3",
            benchmark_name="Voltaic Aimlabs Benchmarks",
            season="3",
            scenarios=[_scenario("VT Angle Novice S3", AIMLABS_S3_TASK)],
        )
        return refresh_catalog(resource_dir)


def _write_resource(  # pylint: disable=too-many-arguments
    resource_dir: Path,
    filename: str,
    *,
    alias: str,
    benchmark_name: str,
    season: str,
    scenarios: list[dict],
    has_leaderboards: bool = True,
) -> None:
    resource_data = {
        "alias": alias,
        "name": benchmark_name,
        "season": season,
        "has_leaderboards": has_leaderboards,
        "is_active": True,
        "tiers": [
            {"id": 1, "name": "Novice"},
            {"id": 2, "name": "Intermediate"},
            {"id": 3, "name": "Advanced"},
        ],
        "categories": [
            {
                "id": 1,
                "name": "clicking",
                "subcategories": [
                    {"id": 10, "name": "dynamic"},
                    {"id": 11, "name": "static"},
                ],
            }
        ],
        "scenarios": scenarios,
    }
    (resource_dir / filename).write_text(json.dumps(resource_data), encoding="utf-8")


def _scenario(name: str, task_id: str) -> dict:
    return {
        "name": name,
        "task_id": task_id,
        "task_mode": 42,
        "weapon_id": "SyntheticWeapon",
        "category_id": 1,
        "subcategory_id": 10,
        "tiers": [{"tier_id": 1, "thresholds": [100]}],
    }


def _ended_at(idx: int) -> str:
    return (BASE_TIME + timedelta(hours=idx)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _play_row(  # pylint: disable=too-many-arguments
    play_id: str,
    task_id: str,
    *,
    idx: int = 0,
    ended_at: str | None = None,
    score: float | None = 100.0,
    status: str | None = "APPROVED",
    performance_scores: dict | None = None,
) -> dict:
    if performance_scores is None:
        performance_scores_text = None
    else:
        performance_scores_text = json.dumps(performance_scores, sort_keys=True, separators=(",", ":"))
    return {
        "account_id": ACCOUNT_ID,
        "id": play_id,
        "task_id": task_id,
        "ended_at": ended_at if ended_at is not None else _ended_at(idx),
        "score": score,
        "play_duration": 60000,
        "pause_duration": 0,
        "gridshield_status": status,
        "performance_scores": performance_scores_text,
        "raw": "{}",
        "first_fetched_at": "2026-06-01T00:00:00.000Z",
        "last_seen_at": "2026-06-01T00:00:00.000Z",
    }


if __name__ == "__main__":
    unittest.main()
