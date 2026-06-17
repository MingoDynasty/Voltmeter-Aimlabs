"""Command-line entry point: argparse dispatcher for the run-history pipeline.

The ``report`` path must stay offline (design §10/§11): never import the
sync/auth/network modules at module level — ``sync``/``login`` import them
lazily inside their handlers.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Optional

import history_report
import play_store
import scenario_catalog
from config import AppConfig, ConfigError, load_config


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    handlers = {
        "report": _run_report,
        "sync": _run_sync,
        "login": _run_login,
        "refresh-catalog": _run_refresh_catalog,
    }
    try:
        return handlers[args.command](args)
    except (
        ConfigError,
        play_store.PlayStoreError,
        scenario_catalog.ScenarioCatalogError,
        history_report.HistoryReportError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    # allow_abbrev=False so e.g. `--session VALUE` cannot silently abbreviate
    # `--session-file` and put a literal secret on the command line (decision 24).
    parser = argparse.ArgumentParser(
        prog="voltmeter",
        description="Voltaic Aimlabs progress tracking tools.",
        allow_abbrev=False,
    )
    parser.add_argument("--config", metavar="PATH", default=None, help="path to config.toml")
    parser.add_argument("--verbose", action="store_true", help="print progress details to stderr")

    # The same options, accepted *after* the subcommand too (`voltmeter report --verbose`).
    # SUPPRESS defaults so an absent flag here never clobbers a value given before the
    # subcommand; the top-level parser above carries the real defaults.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", metavar="PATH", default=argparse.SUPPRESS, help="path to config.toml")
    common.add_argument(
        "--verbose", action="store_true", default=argparse.SUPPRESS, help="print progress details to stderr"
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser(
        "sync",
        parents=[common],
        help="fetch new plays into the local store (never opens a login window)",
        allow_abbrev=False,
    )
    sync_parser.add_argument(
        "--full",
        action="store_true",
        help="walk the whole stream and re-derive every projection (status reconcile)",
    )
    sync_parser.add_argument(
        "--show-deleted",
        action="store_true",
        help="with --full: name stored plays that no longer exist upstream",
    )
    sync_parser.add_argument(
        "--report",
        action="store_true",
        help="print the offline report after the sync",
    )
    sync_parser.add_argument(
        "--session-file",
        metavar="PATH",
        default=None,
        help="read the session cookie from this file (never pass the secret itself)",
    )

    login_parser = subparsers.add_parser(
        "login",
        parents=[common],
        help="open the Aim Lab login window and capture the session cookie into .env",
        allow_abbrev=False,
    )
    login_parser.add_argument(
        "--timeout",
        metavar="SECONDS",
        type=float,
        default=300.0,
        help="how long to wait for the login to complete (default: 300)",
    )

    report_parser = subparsers.add_parser(
        "report",
        parents=[common],
        help="render the runs table and per-scenario stats from the local store (offline)",
        allow_abbrev=False,
    )
    report_parser.add_argument(
        "--include-all-statuses",
        action="store_true",
        help="include non-APPROVED runs in the table and stats",
    )

    subparsers.add_parser(
        "refresh-catalog",
        parents=[common],
        help="validate/rebuild the bundled scenario catalog and report duplicate task ids "
        "(maintainer diagnostic; not required for normal use)",
        allow_abbrev=False,
    )
    return parser


def _run_report(args: argparse.Namespace) -> int:
    app_config = load_config(args.config)
    account_id = app_config.aimlabs_user_id
    if not account_id:
        print("error: [aimlabs].user_id must be set in config.toml (or the file passed via --config).", file=sys.stderr)
        return 1

    play_rows = _load_play_rows(app_config.storage_db_path, account_id, verbose=args.verbose)
    print(_render_report(play_rows, app_config, include_all_statuses=args.include_all_statuses))
    return 0


def _run_sync(args: argparse.Namespace) -> int:  # pylint: disable=too-many-locals
    # Lazy imports keep the offline `report` path free of sync/auth/network modules.
    import aimlabs_auth  # pylint: disable=import-outside-toplevel
    import aimlabs_history  # pylint: disable=import-outside-toplevel
    import history_sync  # pylint: disable=import-outside-toplevel

    if args.show_deleted and not args.full:
        print("error: --show-deleted requires --full.", file=sys.stderr)
        return 1

    app_config = load_config(args.config)
    account_id = app_config.aimlabs_user_id
    if not account_id:
        print("error: [aimlabs].user_id must be set in config.toml (or the file passed via --config).", file=sys.stderr)
        return 1

    request_timeout = app_config.sync_request_timeout_seconds
    try:
        # Credential comes from file/env channels only; on failure this raises with
        # "run `voltmeter login`" — sync never opens a login window (decision 23).
        resolved_session = aimlabs_auth.resolve_session_cookie(session_file=args.session_file)
        if args.verbose:
            print(f"auth: using session from {resolved_session.source}", file=sys.stderr)
        bearer_holder = {
            "bearer": aimlabs_auth.get_bearer_from_session(resolved_session.session_cookie, timeout=request_timeout)
        }
    except aimlabs_auth.AimlabsAuthError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    def refresh_auth() -> None:
        bearer_holder["bearer"] = aimlabs_auth.resolve_bearer(
            session_file=args.session_file,
            timeout=request_timeout,
        )

    fetched_pages = 0

    def fetch_page(*, after: Optional[str], page_size: int) -> aimlabs_history.HistoryPage:
        nonlocal fetched_pages
        if fetched_pages and app_config.sync_request_delay_seconds > 0:
            # Inter-page politeness delay ([sync].request_delay_seconds).
            time.sleep(app_config.sync_request_delay_seconds)
        fetched_pages += 1
        return aimlabs_history.fetch_history_page(
            account_id,
            bearer_holder["bearer"],
            page_size=page_size,
            after=after,
            timeout=request_timeout,
        )

    connection = play_store.connect(_db_path(app_config))
    try:
        try:
            if args.full:
                full_result = history_sync.sync_full(
                    connection,
                    account_id,
                    fetch_page,
                    page_size=app_config.sync_page_size,
                    refresh_auth=refresh_auth,
                    collect_deleted=args.show_deleted,
                    warning_stream=sys.stderr,
                )
                summary = (
                    f"sync --full: {full_result.pages_fetched} pages, {full_result.inserted} inserted, "
                    f"{full_result.updated} updated; Aimlabs reports {full_result.api_total_count} plays."
                )
            else:
                incremental_result = history_sync.sync_incremental(
                    connection,
                    account_id,
                    fetch_page,
                    page_size=app_config.sync_page_size,
                    refresh_auth=refresh_auth,
                    warning_stream=sys.stderr,
                )
                summary = (
                    f"sync: {incremental_result.pages_fetched} pages, {incremental_result.inserted} inserted, "
                    f"{incremental_result.skipped} skipped; Aimlabs reports "
                    f"{incremental_result.api_total_count} plays."
                )
        except (aimlabs_auth.AimlabsAuthError, aimlabs_history.AimlabsHistoryError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1

        _warn_on_practice_contamination(account_id, bearer_holder["bearer"], timeout=request_timeout)
        print(summary)

        if args.report:
            # Reporting half of `sync --report`: store-only, no further auth/network (§11).
            play_rows = play_store.list_plays_by_account(connection, account_id)
            print(_render_report(play_rows, app_config, include_all_statuses=False))
    finally:
        connection.close()
    return 0


def _run_login(args: argparse.Namespace) -> int:
    import aimlabs_auth  # pylint: disable=import-outside-toplevel

    captured_session = aimlabs_auth.login_and_capture(timeout=args.timeout)
    if captured_session is None:
        return 1
    return 0


def _run_refresh_catalog(args: argparse.Namespace) -> int:
    catalog = scenario_catalog.refresh_catalog()
    # load_catalog() is lru_cached; clear it so an in-process report sees the rebuild.
    scenario_catalog.load_catalog.cache_clear()
    families = sorted({record.family for record in catalog.records})
    families_text = ", ".join(families) if families else "none"
    print(
        f"catalog: {len(catalog.records)} records across {len(families)} families ({families_text}); "
        f"{len(catalog.duplicate_task_ids)} duplicate task ids."
    )
    if args.verbose:
        for task_id in catalog.duplicate_task_ids:
            print(f"duplicate task id: {task_id}", file=sys.stderr)
    return 0


def _warn_on_practice_contamination(account_id: str, bearer: str, *, timeout: float) -> None:
    """Once per sync, check the aggregate endpoint for practice plays in the mode-42 stream (§8.3).

    This is a safety net, not a gate: if the check itself fails, warn and continue.
    """
    import aimlabs_history  # pylint: disable=import-outside-toplevel

    try:
        practice_count = aimlabs_history.fetch_practice_contamination_count(account_id, bearer, timeout=timeout)
    except (aimlabs_history.AimlabsHistoryError, OSError) as error:
        print(f"warning: practice-contamination check failed ({error}); continuing.", file=sys.stderr)
        return
    if practice_count > 0:
        print(
            f"warning: {practice_count} practice plays found in the mode-42 stream; stats may be polluted.",
            file=sys.stderr,
        )


def _render_report(play_rows: list[sqlite3.Row], app_config: AppConfig, *, include_all_statuses: bool) -> str:
    report = history_report.build_report(
        play_rows,
        scenario_catalog.load_catalog(),
        family=app_config.report_family,
        include_all_statuses=include_all_statuses,
        rolling_median_window=app_config.report_rolling_median_window,
        rolling_max_window=app_config.report_rolling_max_window,
    )
    return history_report.render_report(report, timezone_setting=app_config.report_timezone)


def _db_path(app_config: AppConfig) -> Path:
    return Path(app_config.storage_db_path) if app_config.storage_db_path else play_store.DEFAULT_DB_PATH


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
