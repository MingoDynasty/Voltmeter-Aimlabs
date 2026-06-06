#!/usr/bin/env python3
"""
Pull Voltaic VALORANT x Aimlabs benchmark PB scores.

By default, the CLI fetches every configured scenario across Novice,
Intermediate, and Advanced. Use --difficulty to narrow the run.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from typing import Optional

from aimlabs_client import fetch_all_scores, fetch_one
from benchmark_constants import (
    BENCHMARKS,
    DEFAULT_DIFFICULTY,
    DIFFICULTIES,
    get_scenarios,
)
from config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from stopwatch import Stopwatch
from voltaic_benchmarks import (
    add_voltaic_metrics,
    calculate_difficulty_overall_rank,
    calculate_subcategory_energy,
)

PDT_TIMEZONE = timezone(timedelta(hours=-7), "PDT")

__all__ = [
    "BENCHMARKS",
    "DEFAULT_DIFFICULTY",
    "DIFFICULTIES",
    "fetch_all_scores",
    "fetch_one",
    "format_table",
    "get_scenarios",
    "main",
]


def _code(task_id: Optional[str]) -> str:
    return task_id.rsplit(".", 1)[-1] if task_id else ""


def _format_timestamp(timestamp_text: Optional[str]) -> str:
    if not timestamp_text:
        return "-"
    try:
        utc_timestamp = datetime.fromisoformat(timestamp_text.replace("Z", "+00:00"))
    except ValueError:
        return timestamp_text
    local_timestamp = utc_timestamp.astimezone(PDT_TIMEZONE)
    return local_timestamp.strftime("%Y-%m-%d %H:%M:%S %Z")


def format_table(rows: list[dict]) -> str:
    headers = [
        "Scenario",
        "Category/Subcategory",
        "Code",
        "PB",
        "Rank",
        "Next Rank",
        "Energy",
        "Acc%",
        "LB Rank",
        "Timestamp",
        "Error",
    ]
    table = []
    for row in rows:
        if row["ok"]:
            accuracy = f"{row['accuracy']:.1f}" if isinstance(row["accuracy"], (int, float)) else "-"
            rank = str(row["rank"]) if row["rank"] is not None else "-"
            timestamp = _format_timestamp(row["ended_at"])
            pb_score = f"{row['score']:.0f}" if isinstance(row["score"], (int, float)) else str(row["score"])
            voltaic_rank = row.get("voltaic_rank") or "-"
            next_rank_progress = _format_next_rank_progress(row)
            energy = _format_energy(row.get("voltaic_energy"))
            error = ""
        else:
            accuracy = rank = timestamp = ""
            voltaic_rank = next_rank_progress = energy = ""
            pb_score = "-"
            error = (row["error"] or "")[:55]
        table.append([
            row["name"],
            f"{row['category']}/{row['sub']}" if row["category"] else "",
            _code(row["task_id"]),
            pb_score,
            voltaic_rank,
            next_rank_progress,
            energy,
            accuracy,
            rank,
            timestamp,
            error,
        ])

    widths = [len(header) for header in headers]
    for table_row in table:
        for idx, cell in enumerate(table_row):
            widths[idx] = max(widths[idx], len(str(cell)))

    def fmt(table_row: list[str]) -> str:
        return "  ".join(str(cell).ljust(widths[idx]) for idx, cell in enumerate(table_row))

    lines = [fmt(headers), "  ".join("-" * width for width in widths)]
    lines += [fmt(table_row) for table_row in table]
    ok_count = sum(1 for row in rows if row["ok"])
    lines.append("")
    lines.append(f"{ok_count}/{len(rows)} scenarios returned a score.")
    return "\n".join(lines)


def _format_energy(energy: Optional[float]) -> str:
    if not isinstance(energy, (int, float)):
        return "-"
    if float(energy).is_integer():
        return f"{energy:.0f}"
    return f"{energy:.1f}"


def _format_next_rank_progress(row: dict) -> str:
    progress = row.get("next_rank_progress_percent")
    next_rank = row.get("next_rank")
    target_score = row.get("next_rank_target_score")
    if not isinstance(progress, (int, float)):
        return "Max" if row.get("voltaic_rank") else "-"
    if next_rank:
        if isinstance(target_score, (int, float)):
            return f"{progress:.1f}% to {next_rank} (target {target_score:g})"
        return f"{progress:.1f}% to {next_rank}"
    return f"{progress:.1f}%"


def _format_overall_next_rank(summary: dict) -> str:
    progress = summary.get("next_rank_progress_percent")
    next_rank = summary.get("next_rank")
    next_rank_energy = summary.get("next_rank_energy")
    if not isinstance(progress, (int, float)):
        return "Max" if summary.get("rank") and summary["rank"] != "Unranked" else "-"
    if next_rank:
        if isinstance(next_rank_energy, (int, float)):
            return f"{progress:.1f}% to {next_rank} (energy {next_rank_energy:g})"
        return f"{progress:.1f}% to {next_rank}"
    return f"{progress:.1f}%"


def _format_grid(headers: list[str], table: list[list[str]]) -> str:
    widths = [len(header) for header in headers]
    for table_row in table:
        for idx, cell in enumerate(table_row):
            widths[idx] = max(widths[idx], len(str(cell)))

    def fmt(table_row: list[str]) -> str:
        return "  ".join(str(cell).ljust(widths[idx]) for idx, cell in enumerate(table_row))

    lines = [fmt(headers), "  ".join("-" * width for width in widths)]
    lines += [fmt(table_row) for table_row in table]
    return "\n".join(lines)


def _format_overall_rank_table(rows: list[dict]) -> str:
    summaries = calculate_difficulty_overall_rank(rows)
    if not summaries:
        return "No overall rank available."

    headers = ["Difficulty", "Overall Rank", "Energy", "Next Rank", "Subcats"]
    table = []
    for summary in summaries:
        table.append([
            summary["difficulty"].upper(),
            summary["rank"] or "-",
            _format_energy(summary["energy"]),
            _format_overall_next_rank(summary),
            str(summary["subcategory_count"]),
        ])
    return _format_grid(headers, table)


def format_subcategory_energy_table(rows: list[dict]) -> str:
    summaries = calculate_subcategory_energy(rows)
    if not summaries:
        return "No subcategory energy available."

    headers = ["Category/Subcategory", "Energy", "Source Scenario", "Rank"]
    lines = []
    summaries_by_difficulty: dict[str, list[dict]] = {}
    for summary in summaries:
        summaries_by_difficulty.setdefault(summary["difficulty"], []).append(summary)

    for idx, difficulty in enumerate(DIFFICULTIES):
        difficulty_summaries = summaries_by_difficulty.get(difficulty)
        if not difficulty_summaries:
            continue
        if idx and lines:
            lines.append("")
        lines.append(f"--- {difficulty.upper()} ---")
        table = []
        for summary in difficulty_summaries:
            source_scenario = summary["source_scenario"] or "-"
            table.append([
                f"{summary['category']}/{summary['subcategory']}",
                _format_energy(summary["energy"]),
                source_scenario,
                summary["rank"] or "-",
            ])
        lines.append(_format_grid(headers, table))
    return "\n".join(lines)


def _records_for_json(rows: list[dict], include_raw: bool) -> list[dict]:
    if include_raw:
        return rows
    return [
        {field_name: field_value for field_name, field_value in row.items() if field_name != "raw"}
        for row in rows
    ]


def _parse_extra_headers(header_texts: list[str]) -> dict:
    extra_headers = {}
    for header_text in header_texts:
        if ":" not in header_text:
            print(f"ignoring malformed --header {header_text!r}", file=sys.stderr)
            continue
        header_key, header_value = header_text.split(":", 1)
        extra_headers[header_key.strip()] = header_value.strip()
    return extra_headers


def _select_scenarios(difficulty: str, scenario_key: Optional[str]) -> list[dict]:
    scenarios = get_scenarios(difficulty)
    if not scenario_key:
        return scenarios

    selected_scenarios = [scenario for scenario in scenarios if scenario["key"] == scenario_key]
    if not selected_scenarios:
        raise ValueError(f"unknown scenario key: {scenario_key}")
    return selected_scenarios


def _group_scenarios_by_difficulty(scenarios: list[dict]) -> dict[str, list[dict]]:
    grouped_scenarios = {difficulty: [] for difficulty in DIFFICULTIES}
    for scenario in scenarios:
        difficulty = scenario["difficulty"]
        grouped_scenarios[difficulty].append(scenario)
    return {
        difficulty: difficulty_scenarios
        for difficulty, difficulty_scenarios in grouped_scenarios.items()
        if difficulty_scenarios
    }


def _fetch_scores_with_timing(
    scenarios: list[dict],
    *,
    user_id: str,
    source: str,
    timeout: float,
    extra_headers: Optional[dict],
) -> list[dict]:
    rows = []
    grouped_scenarios = _group_scenarios_by_difficulty(scenarios)
    for difficulty, difficulty_scenarios in grouped_scenarios.items():
        stopwatch = Stopwatch()
        stopwatch.start()
        difficulty_rows = fetch_all_scores(
            user_id=user_id,
            scenarios=difficulty_scenarios,
            source=source,
            timeout=timeout,
            extra_headers=extra_headers,
        )
        stopwatch.stop()

        ok_count = sum(1 for row in difficulty_rows if row["ok"])
        print(
            f"Fetched {difficulty} scores in {stopwatch.elapsed():.2f}s "
            f"({ok_count}/{len(difficulty_rows)} returned).",
            file=sys.stderr,
        )
        rows.extend(difficulty_rows)
    return rows


def _print_tables(rows: list[dict]) -> None:
    present_difficulties = [
        difficulty for difficulty in DIFFICULTIES
        if any(row["difficulty"] == difficulty for row in rows)
    ]
    for idx, difficulty in enumerate(present_difficulties):
        difficulty_rows = [row for row in rows if row["difficulty"] == difficulty]
        if idx:
            print()
        print(f"=== {difficulty.upper()} ({len(difficulty_rows)} scenarios) ===")
        print(format_table(difficulty_rows))

    print()
    print("=== OVERALL RANK ===")
    print(_format_overall_rank_table(rows))

    print()
    print("=== SUBCATEGORY ENERGY BY DIFFICULTY ===")
    print(format_subcategory_energy_table(rows))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Pull VT VALORANT x Aimlabs benchmark PBs.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="path to config.toml")
    parser.add_argument("--user-id", help="Aimlabs user id; overrides config.toml")
    parser.add_argument("--difficulty", default=DEFAULT_DIFFICULTY, choices=[*DIFFICULTIES, "all"])
    parser.add_argument("--scenario", help="only this scenario key")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    parser.add_argument("--raw", action="store_true", help="include full entry data blobs in JSON")
    parser.add_argument("--out", help="write JSON to this file")
    parser.add_argument("--source", default="cache")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--header", action="append", default=[], help='extra header "Key: Value"')
    args = parser.parse_args(argv)

    try:
        app_config = load_config(Path(args.config))
    except ConfigError as error:
        print(error, file=sys.stderr)
        return 2

    user_id = args.user_id or app_config.aimlabs_user_id
    if not user_id:
        print(
            "Aimlabs user id is required. Set [aimlabs].user_id in config.toml "
            "or pass --user-id.",
            file=sys.stderr,
        )
        return 2

    try:
        scenarios = _select_scenarios(args.difficulty, args.scenario)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2

    rows = _fetch_scores_with_timing(
        scenarios,
        user_id=user_id,
        source=args.source,
        timeout=args.timeout,
        extra_headers=_parse_extra_headers(args.header) or None,
    )
    rows = add_voltaic_metrics(rows)

    if args.json or args.out:
        output = json.dumps(_records_for_json(rows, args.raw), indent=2)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as output_file:
                output_file.write(output)
            print(f"wrote {args.out}", file=sys.stderr)
        else:
            print(output)
    else:
        _print_tables(rows)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
