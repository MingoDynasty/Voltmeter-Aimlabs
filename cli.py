"""Command-line entry point: argparse dispatcher for the run-history pipeline.

Only the offline ``report`` verb is wired so far; ``sync``/``login``/``refresh-catalog``
integration is separate work (design §11).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Optional

import history_report
import play_store
import scenario_catalog
from config import ConfigError, load_config


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return _run_report(args)
    except (
        ConfigError,
        play_store.PlayStoreError,
        scenario_catalog.ScenarioCatalogError,
        history_report.HistoryReportError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="voltmeter", description="Voltaic Aimlabs progress tracking tools.")
    parser.add_argument("--config", metavar="PATH", default=None, help="path to config.toml")
    parser.add_argument("--verbose", action="store_true", help="print progress details to stderr")
    subparsers = parser.add_subparsers(dest="command", required=True)
    report_parser = subparsers.add_parser(
        "report",
        help="render the runs table and per-scenario stats from the local store (offline)",
    )
    report_parser.add_argument(
        "--include-all-statuses",
        action="store_true",
        help="include non-APPROVED runs in the table and stats",
    )
    return parser


def _run_report(args: argparse.Namespace) -> int:
    app_config = load_config(args.config)
    account_id = app_config.aimlabs_user_id
    if not account_id:
        print("error: [aimlabs].user_id must be set in config.toml (or the file passed via --config).", file=sys.stderr)
        return 1

    play_rows = _load_play_rows(app_config.storage_db_path, account_id, verbose=args.verbose)
    report = history_report.build_report(
        play_rows,
        scenario_catalog.load_catalog(),
        family=app_config.report_family,
        include_all_statuses=args.include_all_statuses,
        rolling_median_window=app_config.report_rolling_median_window,
        rolling_max_window=app_config.report_rolling_max_window,
    )
    print(history_report.render_report(report, timezone_setting=app_config.report_timezone))
    return 0


def _load_play_rows(db_path_setting: Optional[str], account_id: str, *, verbose: bool) -> list[sqlite3.Row]:
    db_path = Path(db_path_setting) if db_path_setting else play_store.DEFAULT_DB_PATH
    if not db_path.exists():
        # report is offline-only and must not create the store as a side effect
        if verbose:
            print(f"store not found at {db_path}; rendering an empty report", file=sys.stderr)
        return []

    connection = play_store.connect(db_path)
    try:
        rows = play_store.list_plays_by_account(connection, account_id)
    finally:
        connection.close()
    if verbose:
        print(f"loaded {len(rows)} plays from {db_path}", file=sys.stderr)
    return rows


if __name__ == "__main__":
    sys.exit(main())
