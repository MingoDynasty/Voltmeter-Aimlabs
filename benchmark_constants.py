"""Static Voltaic VALORANT x Aimlabs benchmark scenario metadata."""
from __future__ import annotations

import re

from voltaic_benchmarks import load_valorant_s1

DEFAULT_DIFFICULTY = "all"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _display_name(resource_name: str) -> str:
    name = resource_name.removeprefix("VT ")
    for suffix in (" VALORANT Easy", " VALORANT Hard", " VALORANT"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _label(name: str) -> str:
    return name[:1].upper() + name[1:]


def _category_maps(resource_data: dict) -> tuple[dict[int, str], dict[int, str]]:
    categories_by_id = {}
    subcategories_by_id = {}
    for category in resource_data["categories"]:
        categories_by_id[category["id"]] = _label(category["name"])
        for subcategory in category["subcategories"]:
            subcategories_by_id[subcategory["id"]] = _label(subcategory["name"])
    return categories_by_id, subcategories_by_id


def _difficulty_maps(resource_data: dict) -> tuple[tuple[str, ...], dict[int, str]]:
    difficulty_names = tuple(tier["name"].lower() for tier in resource_data["tiers"])
    difficulties_by_tier_id = {
        tier["id"]: tier["name"].lower()
        for tier in resource_data["tiers"]
    }
    return difficulty_names, difficulties_by_tier_id


def _scenario_record(
    resource_scenario: dict,
    *,
    difficulty: str,
    categories_by_id: dict[int, str],
    subcategories_by_id: dict[int, str],
) -> dict:
    display_name = _display_name(resource_scenario["name"])
    return {
        "key": _slug(display_name),
        "name": display_name,
        "category": categories_by_id[resource_scenario["category_id"]],
        "sub": subcategories_by_id[resource_scenario["subcategory_id"]],
        "task_id": resource_scenario["task_id"],
        "weapon_id": resource_scenario["weapon_id"],
        "task_mode": resource_scenario.get("task_mode", 42),
        "target": None,
        "difficulty": difficulty,
    }


def _build_benchmarks() -> tuple[tuple[str, ...], dict[str, list[dict]]]:
    resource_data = load_valorant_s1()
    difficulty_names, difficulties_by_tier_id = _difficulty_maps(resource_data)
    categories_by_id, subcategories_by_id = _category_maps(resource_data)
    benchmarks = {difficulty: [] for difficulty in difficulty_names}

    for resource_scenario in resource_data["scenarios"]:
        for tier in resource_scenario["tiers"]:
            difficulty = difficulties_by_tier_id[tier["tier_id"]]
            benchmarks[difficulty].append(
                _scenario_record(
                    resource_scenario,
                    difficulty=difficulty,
                    categories_by_id=categories_by_id,
                    subcategories_by_id=subcategories_by_id,
                )
            )
    return difficulty_names, benchmarks


DIFFICULTIES, BENCHMARKS = _build_benchmarks()


def get_scenarios(selected_difficulty: str = DEFAULT_DIFFICULTY) -> list[dict]:
    """Return scenario records for a difficulty, or all scenarios by default."""
    if selected_difficulty == "all":
        all_scenarios: list[dict] = []
        for difficulty in DIFFICULTIES:
            all_scenarios += get_scenarios(difficulty)
        return all_scenarios
    if selected_difficulty not in BENCHMARKS:
        raise ValueError(f"unknown difficulty {selected_difficulty!r}; pick from {DIFFICULTIES} or 'all'")

    return [dict(scenario) for scenario in BENCHMARKS[selected_difficulty]]
