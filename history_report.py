"""Offline runs table and per-scenario stats computed from the local play store.

This module is a pure function of stored rows plus the scenario catalog: it never
performs network, auth, or login work (design §10/§11 report invariant).
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from typing import Any, Optional, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from scenario_catalog import ScenarioCatalog, ScenarioCatalogRecord

REPORT_FAMILIES = ("all", "valorant")
APPROVED_STATUS = "APPROVED"
NO_RUNS_MESSAGE = "no runs found"

PlayRow = Union[Mapping[str, Any], sqlite3.Row]


class HistoryReportError(RuntimeError):
    """Raised when a report cannot be built or rendered from the requested parameters."""


@dataclass(frozen=True)
class ReportPlay:
    task_id: str
    scenario_name: str
    ended_at_utc: str
    score: Optional[float]
    gridshield_status: Optional[str]
    performance_scores: Any


# pylint: disable=too-many-instance-attributes
@dataclass(frozen=True)
class TaskSummary:
    task_id: str
    scenario_name: str
    benchmark_alias: str
    play_count: int
    personal_best: Optional[float]
    mean_score: Optional[float]
    median_score: Optional[float]
    rolling_median: Optional[float]
    rolling_max: Optional[float]
    first_ended_at_utc: Optional[str]
    last_ended_at_utc: Optional[str]


@dataclass(frozen=True)
class OtherSummary:
    scenario_count: int
    play_count: int


@dataclass(frozen=True)
class HistoryReport:
    family: str
    include_all_statuses: bool
    rolling_median_window: int
    rolling_max_window: int
    table_rows: tuple[ReportPlay, ...]
    task_summaries: tuple[TaskSummary, ...]
    excluded_non_approved_count: int
    other: OtherSummary


# pylint: disable-next=too-many-arguments
def build_report(
    play_rows: Iterable[PlayRow],
    catalog: ScenarioCatalog,
    *,
    family: str = "all",
    include_all_statuses: bool = False,
    rolling_median_window: int = 10,
    rolling_max_window: int = 25,
) -> HistoryReport:
    """Build the report model from stored play rows.

    Plays outside the selected family (known-but-other-family and unknown task_ids)
    are retained in the "other" summary, never dropped. Non-APPROVED plays are
    excluded from the table and stats unless ``include_all_statuses`` is set; the
    excluded count is kept for the visible note.
    """
    if family not in REPORT_FAMILIES:
        raise HistoryReportError(f"family must be one of {REPORT_FAMILIES}, got {family!r}.")
    if rolling_median_window < 1 or rolling_max_window < 1:
        raise HistoryReportError("rolling windows must be positive integers.")

    sorted_rows = sorted(play_rows, key=lambda row: row["ended_at"], reverse=True)
    table_plays: list[ReportPlay] = []
    excluded_non_approved_count = 0
    other_task_ids: set[str] = set()
    other_play_count = 0

    for row in sorted_rows:
        task_id = row["task_id"]
        display_record = catalog.resolve_task_id(task_id)[0]
        if not _in_family(display_record, family):
            other_task_ids.add(task_id)
            other_play_count += 1
            continue
        if not include_all_statuses and row["gridshield_status"] != APPROVED_STATUS:
            excluded_non_approved_count += 1
            continue
        table_plays.append(_report_play(row, display_record))

    return HistoryReport(
        family=family,
        include_all_statuses=include_all_statuses,
        rolling_median_window=rolling_median_window,
        rolling_max_window=rolling_max_window,
        table_rows=tuple(table_plays),
        task_summaries=_summarize_tasks(
            table_plays,
            catalog,
            rolling_median_window=rolling_median_window,
            rolling_max_window=rolling_max_window,
        ),
        excluded_non_approved_count=excluded_non_approved_count,
        other=OtherSummary(scenario_count=len(other_task_ids), play_count=other_play_count),
    )


def render_report(report: HistoryReport, *, timezone_setting: str = "local") -> str:
    """Render the report model as console text, converting timestamps for display only."""
    display_tz, timezone_label = _resolve_timezone(timezone_setting)
    lines = [
        f"Voltaic runs report (family: {report.family})",
        f"All times shown in {timezone_label}.",
    ]
    status_note = _status_note(report)
    if status_note:
        lines.append(status_note)
    lines.append("")
    if report.table_rows:
        lines.extend(_render_runs_table(report.table_rows, display_tz))
        lines.append("")
        lines.extend(_render_task_summaries(report, display_tz))
    else:
        lines.append(NO_RUNS_MESSAGE)
    if report.other.play_count:
        lines.append("")
        lines.append(_render_other_footer(report.other))
    return "\n".join(lines)


def _in_family(record: ScenarioCatalogRecord, family: str) -> bool:
    if family == "all":
        return record.is_known
    return record.family == family


def _report_play(row: PlayRow, display_record: ScenarioCatalogRecord) -> ReportPlay:
    performance_text = row["performance_scores"]
    return ReportPlay(
        task_id=row["task_id"],
        scenario_name=display_record.name,
        ended_at_utc=row["ended_at"],
        score=row["score"],
        gridshield_status=row["gridshield_status"],
        performance_scores=json.loads(performance_text) if performance_text else None,
    )


def _summarize_tasks(
    table_plays: Sequence[ReportPlay],
    catalog: ScenarioCatalog,
    *,
    rolling_median_window: int,
    rolling_max_window: int,
) -> tuple[TaskSummary, ...]:
    """Bucket by exact task_id (never by name), most recent play first."""
    buckets: dict[str, list[ReportPlay]] = {}
    for play in table_plays:
        buckets.setdefault(play.task_id, []).append(play)

    summaries = []
    for task_id, plays in buckets.items():
        scores = [play.score for play in plays if play.score is not None]
        summaries.append(
            TaskSummary(
                task_id=task_id,
                scenario_name=plays[0].scenario_name,
                benchmark_alias=catalog.resolve_task_id(task_id)[0].benchmark_alias,
                play_count=len(plays),
                personal_best=max(scores) if scores else None,
                mean_score=statistics.fmean(scores) if scores else None,
                median_score=statistics.median(scores) if scores else None,
                rolling_median=statistics.median(scores[:rolling_median_window]) if scores else None,
                rolling_max=max(scores[:rolling_max_window]) if scores else None,
                first_ended_at_utc=plays[-1].ended_at_utc,
                last_ended_at_utc=plays[0].ended_at_utc,
            )
        )
    return tuple(summaries)


def _status_note(report: HistoryReport) -> Optional[str]:
    if report.include_all_statuses:
        included_count = sum(1 for play in report.table_rows if play.gridshield_status != APPROVED_STATUS)
        if included_count:
            return f"Note: including {_pluralize(included_count, 'non-APPROVED run')} in the table and stats."
        return None
    if report.excluded_non_approved_count:
        excluded_text = _pluralize(report.excluded_non_approved_count, "non-APPROVED run")
        return f"Note: {excluded_text} excluded (pass --include-all-statuses to include them)."
    return None


def _render_runs_table(table_rows: Sequence[ReportPlay], display_tz: Optional[tzinfo]) -> list[str]:
    headers = ("Date", "Scenario", "Score", "Scenario Stats")
    rows = []
    for play in table_rows:
        stats_text = _format_performance_scores(play.performance_scores)
        if play.gridshield_status != APPROVED_STATUS:
            stats_text = f"{stats_text} [{play.gridshield_status or 'UNKNOWN STATUS'}]"
        rows.append(
            (
                _format_timestamp(play.ended_at_utc, display_tz),
                play.scenario_name,
                _format_score(play.score),
                stats_text,
            )
        )
    return _format_table(headers, rows)


def _render_task_summaries(report: HistoryReport, display_tz: Optional[tzinfo]) -> list[str]:
    scope_label = "all statuses" if report.include_all_statuses else "APPROVED runs only"
    section_header = (
        f"Per-scenario stats ({scope_label}; rolling median over last {report.rolling_median_window}, "
        f"rolling max over last {report.rolling_max_window}):"
    )
    headers = (
        "Scenario",
        "Benchmark",
        "Plays",
        "PB",
        "Mean",
        "Median",
        "Rolling median",
        "Rolling max",
        "First",
        "Last",
    )
    rows = []
    for summary in report.task_summaries:
        rows.append(
            (
                summary.scenario_name,
                summary.benchmark_alias,
                str(summary.play_count),
                _format_score(summary.personal_best),
                _format_score(summary.mean_score),
                _format_score(summary.median_score),
                _format_score(summary.rolling_median),
                _format_score(summary.rolling_max),
                _format_date(summary.first_ended_at_utc, display_tz),
                _format_date(summary.last_ended_at_utc, display_tz),
            )
        )
    return [section_header, ""] + _format_table(headers, rows)


def _render_other_footer(other: OtherSummary) -> str:
    scenario_text = _pluralize(other.scenario_count, "scenario")
    play_text = _pluralize(other.play_count, "play")
    return f"+{scenario_text}, {play_text}, untracked"


def _resolve_timezone(timezone_setting: str) -> tuple[Optional[tzinfo], str]:
    if timezone_setting == "local":
        local_name = datetime.now().astimezone().tzname() or "unknown"
        return None, f"local ({local_name})"
    try:
        zone = ZoneInfo(timezone_setting)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise HistoryReportError(f"Unknown timezone {timezone_setting!r}.") from error
    return zone, timezone_setting


def _format_timestamp(ended_at_utc: str, display_tz: Optional[tzinfo]) -> str:
    return _parse_utc(ended_at_utc).astimezone(display_tz).strftime("%Y-%m-%d %H:%M:%S")


def _format_date(ended_at_utc: Optional[str], display_tz: Optional[tzinfo]) -> str:
    if ended_at_utc is None:
        return "-"
    return _parse_utc(ended_at_utc).astimezone(display_tz).strftime("%Y-%m-%d")


def _parse_utc(ended_at_utc: str) -> datetime:
    parsed = datetime.fromisoformat(ended_at_utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _format_score(value: Optional[float]) -> str:
    if value is None:
        return "-"
    if value == int(value):
        return str(int(value))
    return f"{value:.1f}"


def _format_performance_scores(performance_scores: Any) -> str:
    if performance_scores is None:
        return "-"
    if isinstance(performance_scores, Mapping):
        parts = [f"{key}={_format_stat_value(value)}" for key, value in sorted(performance_scores.items())]
        return ", ".join(parts) if parts else "-"
    return json.dumps(performance_scores, sort_keys=True, separators=(",", ":"))


def _format_stat_value(value: Any) -> str:
    if not isinstance(value, bool) and isinstance(value, (int, float)):
        return f"{value:g}"
    return str(value)


def _format_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    widths = [len(header) for header in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))
    separator = tuple("-" * width for width in widths)
    lines = [_format_table_row(headers, widths), _format_table_row(separator, widths)]
    lines.extend(_format_table_row(row, widths) for row in rows)
    return lines


def _format_table_row(cells: Sequence[str], widths: Sequence[int]) -> str:
    return " | ".join(cell.ljust(width) for cell, width in zip(cells, widths)).rstrip()


def _pluralize(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"
