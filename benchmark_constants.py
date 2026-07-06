"""Voltaic VALORANT x Aimlabs benchmark scenario metadata."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from functools import lru_cache

from voltaic_benchmarks import (
    category_maps,
    difficulty_maps,
    load_valorant_s1,
    lookup_label,
    tier_difficulty,
)

DEFAULT_DIFFICULTY = "all"
DEFAULT_TASK_MODE = 42
DIFFICULTIES = ("novice", "intermediate", "advanced")


class _LazyBenchmarks(Mapping[str, list[dict]]):
    def __getitem__(self, difficulty: str) -> list[dict]:
        return get_benchmarks()[difficulty]

    def __iter__(self) -> Iterator[str]:
        return iter(get_benchmarks())

    def __len__(self) -> int:
        return len(get_benchmarks())

    def __contains__(self, difficulty: object) -> bool:
        return isinstance(difficulty, str) and difficulty in get_benchmarks()


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _display_name(resource_name: str) -> str:
    name = resource_name.removeprefix("VT ")
    for suffix in (" VALORANT Easy", " VALORANT Hard", " VALORANT"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _scenario_record(
    resource_scenario: dict,
    *,
    difficulty: str,
    categories_by_id: dict[int, str],
    subcategories_by_id: dict[int, str],
) -> dict:
    task_id = resource_scenario.get("task_id")
    weapon_id = resource_scenario.get("weapon_id")
    if not task_id or not weapon_id:
        raise ValueError(
            f"Scenario {resource_scenario.get('name', '<unnamed>')!r} is missing task/weapon ids."
        )

    display_name = _display_name(resource_scenario["name"])
    return {
        "key": _slug(display_name),
        "name": display_name,
        "category": lookup_label(categories_by_id, "category", resource_scenario),
        "sub": lookup_label(subcategories_by_id, "subcategory", resource_scenario),
        "task_id": task_id,
        "weapon_id": weapon_id,
        "task_mode": resource_scenario.get("task_mode", DEFAULT_TASK_MODE),
        "difficulty": difficulty,
    }


@lru_cache(maxsize=1)
def get_benchmarks() -> dict[str, list[dict]]:
    """Build benchmark scenario records from the Voltaic resource file."""
    resource_data = load_valorant_s1()
    difficulties_by_tier_id = difficulty_maps(
        resource_data, allowed_difficulties=DIFFICULTIES
    )
    categories_by_id, subcategories_by_id = category_maps(resource_data)
    benchmarks: dict[str, list[dict]] = {difficulty: [] for difficulty in DIFFICULTIES}

    for resource_scenario in resource_data["scenarios"]:
        for tier in resource_scenario["tiers"]:
            difficulty = tier_difficulty(
                difficulties_by_tier_id, tier, resource_scenario
            )
            benchmarks[difficulty].append(
                _scenario_record(
                    resource_scenario,
                    difficulty=difficulty,
                    categories_by_id=categories_by_id,
                    subcategories_by_id=subcategories_by_id,
                )
            )
    return benchmarks


BENCHMARKS: Mapping[str, list[dict]] = _LazyBenchmarks()


def get_scenarios(selected_difficulty: str = DEFAULT_DIFFICULTY) -> list[dict]:
    """Return scenario records for a difficulty, or all scenarios by default."""
    if selected_difficulty == "all":
        all_scenarios: list[dict] = []
        for difficulty in DIFFICULTIES:
            all_scenarios += get_scenarios(difficulty)
        return all_scenarios
    if selected_difficulty not in DIFFICULTIES:
        raise ValueError(
            f"unknown difficulty {selected_difficulty!r}; pick from {DIFFICULTIES} or 'all'"
        )

    return [dict(scenario) for scenario in get_benchmarks()[selected_difficulty]]
