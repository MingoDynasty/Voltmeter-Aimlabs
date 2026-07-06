"""Scenario catalog metadata projection for Voltaic Aimlabs resources."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from voltaic_benchmarks import (
    category_maps,
    difficulty_maps,
    lookup_label,
    tier_difficulty,
)

DEFAULT_RESOURCE_DIR = Path(__file__).resolve().parent / "resources" / "aimlabs"
DEFAULT_TASK_MODE = 42
DIFFICULTY_ORDER = {"novice": 0, "intermediate": 1, "advanced": 2}
UNKNOWN_VALUE = "unknown"
UNKNOWN_SCENARIO_NAME = "Unknown scenario"


class ScenarioCatalogError(RuntimeError):
    """Raised when bundled scenario catalog resources cannot be loaded."""


@dataclass(frozen=True, slots=True)
class ScenarioCatalogRecord:
    task_id: str
    weapon_id: str
    name: str
    category: str
    sub: str
    difficulty: str
    season: str
    benchmark_alias: str
    benchmark_name: str
    family: str
    is_active: bool
    has_leaderboards: bool
    is_known: bool = True


class ScenarioCatalog:
    """Resolved scenario catalog keyed by exact Aimlabs task_id."""

    def __init__(
        self,
        records: Iterable[ScenarioCatalogRecord],
        *,
        shadowed_records: Iterable[ScenarioCatalogRecord] = (),
    ) -> None:
        self._records = tuple(records)
        self._records_by_task_id = _records_by_task_id(self._records)
        self._shadowed_records = tuple(shadowed_records)
        self._shadowed_records_by_task_id = _records_by_task_id(self._shadowed_records)

    @property
    def records(self) -> tuple[ScenarioCatalogRecord, ...]:
        return self._records

    @property
    def task_ids(self) -> tuple[str, ...]:
        return tuple(self._records_by_task_id)

    @property
    def duplicate_task_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._shadowed_records_by_task_id))

    def records_for_task_id(self, task_id: str) -> tuple[ScenarioCatalogRecord, ...]:
        return self._records_by_task_id.get(task_id, ())

    def shadowed_records_for_task_id(
        self, task_id: str
    ) -> tuple[ScenarioCatalogRecord, ...]:
        return self._shadowed_records_by_task_id.get(task_id, ())

    def resolve_task_id(self, task_id: str) -> tuple[ScenarioCatalogRecord, ...]:
        records = self.records_for_task_id(task_id)
        if records:
            return records
        return (unknown_scenario_record(task_id),)

    def resolve_task_ids(
        self, task_ids: Iterable[str]
    ) -> dict[str, tuple[ScenarioCatalogRecord, ...]]:
        return {task_id: self.resolve_task_id(task_id) for task_id in task_ids}

    def unknown_task_ids(self, task_ids: Iterable[str]) -> tuple[str, ...]:
        return tuple(
            task_id for task_id in task_ids if not self.records_for_task_id(task_id)
        )


def unknown_scenario_record(task_id: str) -> ScenarioCatalogRecord:
    return ScenarioCatalogRecord(
        task_id=task_id,
        weapon_id=UNKNOWN_VALUE,
        name=UNKNOWN_SCENARIO_NAME,
        category=UNKNOWN_VALUE,
        sub=UNKNOWN_VALUE,
        difficulty=UNKNOWN_VALUE,
        season=UNKNOWN_VALUE,
        benchmark_alias=UNKNOWN_VALUE,
        benchmark_name=UNKNOWN_SCENARIO_NAME,
        family=UNKNOWN_VALUE,
        is_active=False,
        has_leaderboards=False,
        is_known=False,
    )


def refresh_catalog(resource_dir: Path | str = DEFAULT_RESOURCE_DIR) -> ScenarioCatalog:
    """Rebuild the scenario catalog from every resource JSON file in a directory."""
    resource_path = Path(resource_dir)
    records: list[ScenarioCatalogRecord] = []
    for path in sorted(resource_path.glob("*.json")):
        records.extend(_records_from_resource_file(path))
    kept_records, shadowed_records = _apply_duplicate_task_id_policy(records)
    return ScenarioCatalog(kept_records, shadowed_records=shadowed_records)


@lru_cache(maxsize=1)
def load_catalog() -> ScenarioCatalog:
    """Load the bundled catalog once for normal read paths."""
    return refresh_catalog()


def resolve_task_ids(
    task_ids: Iterable[str], catalog: ScenarioCatalog | None = None
) -> dict[str, tuple[ScenarioCatalogRecord, ...]]:
    """Resolve task_ids through a catalog, retaining unknown task_ids as labelled records."""
    active_catalog = catalog if catalog is not None else load_catalog()
    return active_catalog.resolve_task_ids(task_ids)


def _records_from_resource_file(path: Path) -> tuple[ScenarioCatalogRecord, ...]:
    try:
        resource_text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ScenarioCatalogError(
            f"Could not read scenario resource {path}: {error}"
        ) from error

    try:
        resource_data = json.loads(resource_text)
    except json.JSONDecodeError as error:
        raise ScenarioCatalogError(
            f"Could not parse scenario resource {path}: {error}"
        ) from error
    if not isinstance(resource_data, dict):
        raise ScenarioCatalogError(
            f"Scenario resource {path} must contain a JSON object."
        )
    try:
        return _records_from_resource(resource_data)
    except (KeyError, TypeError, ValueError) as error:
        raise ScenarioCatalogError(
            f"Malformed scenario resource {path}: {error}"
        ) from error


def _records_from_resource(resource_data: dict) -> tuple[ScenarioCatalogRecord, ...]:
    categories_by_id, subcategories_by_id = category_maps(resource_data)
    difficulties_by_tier_id = difficulty_maps(resource_data)
    records: list[ScenarioCatalogRecord] = []
    for resource_scenario in resource_data["scenarios"]:
        for tier in resource_scenario["tiers"]:
            difficulty = tier_difficulty(
                difficulties_by_tier_id, tier, resource_scenario
            )
            records.append(
                _scenario_record(
                    resource_data,
                    resource_scenario,
                    difficulty=difficulty,
                    categories_by_id=categories_by_id,
                    subcategories_by_id=subcategories_by_id,
                )
            )
    return tuple(records)


def _scenario_record(
    resource_data: dict,
    resource_scenario: dict,
    *,
    difficulty: str,
    categories_by_id: dict[int, str],
    subcategories_by_id: dict[int, str],
) -> ScenarioCatalogRecord:
    task_id = resource_scenario.get("task_id")
    weapon_id = resource_scenario.get("weapon_id")
    if not task_id or not weapon_id:
        raise ValueError(
            f"Scenario {resource_scenario.get('name', '<unnamed>')!r} is missing task/weapon ids."
        )

    return ScenarioCatalogRecord(
        task_id=task_id,
        weapon_id=weapon_id,
        name=_display_name(resource_scenario["name"]),
        category=lookup_label(categories_by_id, "category", resource_scenario),
        sub=lookup_label(subcategories_by_id, "subcategory", resource_scenario),
        difficulty=difficulty,
        season=str(resource_data["season"]),
        benchmark_alias=resource_data["alias"],
        benchmark_name=resource_data["name"],
        family=_derive_family(resource_data),
        is_active=bool(resource_data["is_active"]),
        has_leaderboards=bool(resource_data["has_leaderboards"]),
    )


def _display_name(resource_name: str) -> str:
    # Strip only the universal "VT " (Voltaic) prefix — it is on every scenario in every
    # source and so never disambiguates. The rest of the upstream name is retained verbatim
    # so each task_id keeps a unique label: the difficulty/season qualifiers ("VALORANT Hard",
    # "Novice S3", …) are what tell e.g. MiniTS Normal from MiniTS Hard apart in the report.
    # (Some task_ids span multiple difficulty tiers — e.g. "Fourshot Adaptive" — so difficulty
    # is a property of the benchmark tier, not the task_id, and cannot be a per-row column.)
    return resource_name.removeprefix("VT ").strip()


def _derive_family(resource_data: dict) -> str:
    alias = resource_data["alias"]
    season = str(resource_data["season"])
    if alias.startswith("valorant_"):
        return "valorant"
    if alias.startswith("aimlabs_"):
        return "aimlabs"
    raise ValueError(f"Unknown benchmark family for alias {alias!r} season {season!r}.")


def _apply_duplicate_task_id_policy(
    records: Iterable[ScenarioCatalogRecord],
) -> tuple[tuple[ScenarioCatalogRecord, ...], tuple[ScenarioCatalogRecord, ...]]:
    """Prefer the newest benchmark source for duplicate task_ids across sources.

    Multi-tier records from the same source are kept together. When multiple resource
    sources claim the same task_id, the newest season wins; same-season ties use the
    lexicographically later benchmark alias. Shadowed records remain available for diagnostics.
    """
    kept_records: list[ScenarioCatalogRecord] = []
    shadowed_records: list[ScenarioCatalogRecord] = []
    for task_records in _records_by_task_id(tuple(records)).values():
        source_keys = {_source_key(record) for record in task_records}
        selected_source_key = max(source_keys, key=_source_sort_key)
        for record in task_records:
            if len(source_keys) == 1 or _source_key(record) == selected_source_key:
                kept_records.append(record)
            else:
                shadowed_records.append(record)
    return tuple(sorted(kept_records, key=_record_sort_key)), tuple(
        sorted(shadowed_records, key=_record_sort_key)
    )


def _records_by_task_id(
    records: Iterable[ScenarioCatalogRecord],
) -> dict[str, tuple[ScenarioCatalogRecord, ...]]:
    grouped_records: dict[str, list[ScenarioCatalogRecord]] = {}
    for record in records:
        grouped_records.setdefault(record.task_id, []).append(record)
    return {
        task_id: tuple(sorted(task_records, key=_record_sort_key))
        for task_id, task_records in grouped_records.items()
    }


def _source_key(record: ScenarioCatalogRecord) -> tuple[str, str]:
    return record.season, record.benchmark_alias


def _source_sort_key(source_key: tuple[str, str]) -> tuple[int, str, str]:
    season, alias = source_key
    try:
        season_number = int(season)
    except ValueError:
        season_number = -1
    return season_number, season, alias


def _record_sort_key(
    record: ScenarioCatalogRecord,
) -> tuple[int, str, str, str, int, str]:
    season_number, season, alias = _source_sort_key(_source_key(record))
    difficulty_idx = DIFFICULTY_ORDER.get(record.difficulty, len(DIFFICULTY_ORDER))
    return season_number, season, alias, record.name, difficulty_idx, record.task_id
