"""Voltaic benchmark resource loading and score evaluation."""

from __future__ import annotations

import json
from functools import lru_cache
from math import floor
from pathlib import Path
from typing import Optional

RESOURCE_PATH = Path(__file__).resolve().parent / "resources" / "aimlabs" / "valorant_s1.json"


class BenchmarkResourceError(RuntimeError):
    """Raised when the bundled benchmark resource cannot be loaded."""


@lru_cache(maxsize=1)
def load_valorant_s1() -> dict:
    try:
        resource_text = RESOURCE_PATH.read_text(encoding="utf-8")
    except OSError as error:
        raise BenchmarkResourceError(f"Could not read benchmark resource {RESOURCE_PATH}: {error}") from error

    try:
        resource_data = json.loads(resource_text)
    except json.JSONDecodeError as error:
        raise BenchmarkResourceError(f"Could not parse benchmark resource {RESOURCE_PATH}: {error}") from error
    if not isinstance(resource_data, dict):
        raise BenchmarkResourceError(f"Benchmark resource {RESOURCE_PATH} must contain a JSON object.")
    return resource_data


def _label(name: str) -> str:
    return name[:1].upper() + name[1:]


def category_maps(resource_data: dict) -> tuple[dict[int, str], dict[int, str]]:
    """Map category and subcategory ids to display labels for a benchmark resource."""
    categories_by_id = {}
    subcategories_by_id = {}
    for category in resource_data["categories"]:
        categories_by_id[category["id"]] = _label(category["name"])
        for subcategory in category["subcategories"]:
            subcategories_by_id[subcategory["id"]] = _label(subcategory["name"])
    return categories_by_id, subcategories_by_id


def difficulty_maps(resource_data: dict, *, allowed_difficulties: Optional[tuple[str, ...]] = None) -> dict[int, str]:
    """Map tier ids to lowercase difficulty names, validated against an allowed set when given."""
    difficulties_by_tier_id = {}
    for tier in resource_data["tiers"]:
        difficulty = tier["name"].lower()
        if allowed_difficulties is not None and difficulty not in allowed_difficulties:
            raise ValueError(f"Unknown difficulty {difficulty!r} for tier id {tier['id']!r}.")
        difficulties_by_tier_id[tier["id"]] = difficulty
    return difficulties_by_tier_id


def lookup_label(labels_by_id: dict[int, str], label_type: str, resource_scenario: dict) -> str:
    """Resolve a scenario's category/subcategory id to its label, raising on unknown ids."""
    label_id = resource_scenario[f"{label_type}_id"]
    label = labels_by_id.get(label_id)
    if label is None:
        raise ValueError(
            f"Unknown {label_type}_id {label_id!r} for scenario {resource_scenario.get('name', '<unnamed>')!r}."
        )
    return label


def tier_difficulty(difficulties_by_tier_id: dict[int, str], tier: dict, resource_scenario: dict) -> str:
    """Resolve a scenario tier's difficulty, raising on unknown tier ids."""
    tier_id = tier["tier_id"]
    difficulty = difficulties_by_tier_id.get(tier_id)
    if difficulty is None:
        raise ValueError(f"Unknown tier_id {tier_id!r} for scenario {resource_scenario.get('name', '<unnamed>')!r}.")
    return difficulty


@lru_cache(maxsize=1)
def _ranks_by_tier() -> dict[int, list[dict]]:
    ranks_by_tier: dict[int, list[dict]] = {}
    for rank in load_valorant_s1()["ranks"]:
        ranks_by_tier.setdefault(rank["tier_id"], []).append(rank)
    for ranks in ranks_by_tier.values():
        ranks.sort(key=lambda rank: rank["energy_threshold"])
    return ranks_by_tier


@lru_cache(maxsize=1)
def _tier_order() -> tuple[dict, ...]:
    return tuple(load_valorant_s1()["tiers"])


@lru_cache(maxsize=1)
def _tiers_by_difficulty() -> dict[str, dict]:
    return {tier["name"].lower(): tier for tier in _tier_order()}


@lru_cache(maxsize=1)
def _tier_index_by_id() -> dict[int, int]:
    return {tier["id"]: tier_idx for tier_idx, tier in enumerate(_tier_order())}


@lru_cache(maxsize=1)
def _tier_energies() -> tuple[tuple[int, ...], ...]:
    return tuple(
        tuple(rank["energy_threshold"] for rank in _ranks_by_tier().get(tier["id"], [])) for tier in _tier_order()
    )


@lru_cache(maxsize=1)
def _scenarios_by_key() -> dict[tuple[str, str], dict]:
    scenarios_by_key = {}
    for scenario in load_valorant_s1()["scenarios"]:
        scenario_key = (scenario["task_id"], scenario["weapon_id"])
        scenarios_by_key[scenario_key] = scenario
    return scenarios_by_key


def _title_rank(rank_name: Optional[str]) -> Optional[str]:
    if rank_name is None:
        return None
    return rank_name.replace("_", " ").title()


def _threshold_entries(resource_scenario: dict) -> list[dict]:
    entries = []
    ranks_by_tier = _ranks_by_tier()
    for tier in resource_scenario["tiers"]:
        tier_ranks = ranks_by_tier.get(tier["tier_id"], [])
        for threshold_idx, threshold in enumerate(tier["thresholds"]):
            if threshold_idx >= len(tier_ranks):
                continue
            rank = tier_ranks[threshold_idx]
            entries.append(
                {
                    "threshold": threshold,
                    "rank": _title_rank(rank["name"]),
                    "energy": rank["energy_threshold"],
                    "tier_id": tier["tier_id"],
                }
            )
    return sorted(entries, key=lambda entry: entry["energy"])


def _resource_tier_for_difficulty(resource_scenario: dict, difficulty: Optional[str]) -> Optional[dict]:
    if difficulty is not None:
        benchmark_tier = _tiers_by_difficulty().get(difficulty)
        if benchmark_tier is not None:
            tier_id = benchmark_tier["id"]
            for resource_tier in resource_scenario["tiers"]:
                if resource_tier["tier_id"] == tier_id:
                    return resource_tier
    return resource_scenario["tiers"][0] if resource_scenario.get("tiers") else None


def _extended_thresholds(thresholds: list[float], current_tier_idx: int) -> list[float]:
    extended_thresholds = [0.0]
    if current_tier_idx != 0 and len(thresholds) >= 2:
        ghost_rank_threshold = thresholds[0] - (thresholds[1] - thresholds[0])
        extended_thresholds.append(float(ghost_rank_threshold))
    extended_thresholds.extend(float(threshold) for threshold in thresholds)
    return extended_thresholds


def _extended_rank_energies(current_tier_idx: int) -> list[float]:
    tier_energies = _tier_energies()
    extended_energies = [0.0]
    if current_tier_idx != 0:
        extended_energies.append(float(max(tier_energies[current_tier_idx - 1])))
    extended_energies.extend(float(energy) for energy in tier_energies[current_tier_idx])
    return extended_energies


def _interval_differences(values: list[float]) -> list[float]:
    differences = []
    for value_idx, value in enumerate(values):
        if value_idx == len(values) - 1:
            differences.append(value - values[value_idx - 1] if value_idx else value)
        else:
            differences.append(values[value_idx + 1] - value)
    return differences


def _last_reached_threshold_idx(thresholds: list[float], score: float) -> int:
    reached_idx = 0
    for threshold_idx, threshold in enumerate(thresholds):
        if score >= threshold:
            reached_idx = threshold_idx
    return reached_idx


def _uncapped_scenario_energy(score: float, thresholds: list[float], current_tier_idx: int) -> float:
    extended_thresholds = _extended_thresholds(thresholds, current_tier_idx)
    extended_energies = _extended_rank_energies(current_tier_idx)
    reached_idx = _last_reached_threshold_idx(extended_thresholds, score)

    threshold_differences = _interval_differences(extended_thresholds)
    energy_differences = _interval_differences(extended_energies)
    threshold_difference = threshold_differences[reached_idx]
    if threshold_difference <= 0:
        return extended_energies[reached_idx]

    score_above_threshold = score - extended_thresholds[reached_idx]
    return (
        extended_energies[reached_idx] + score_above_threshold / threshold_difference * energy_differences[reached_idx]
    )


def _tier_energy_cap(current_tier_idx: int) -> float:
    tier_energies = _tier_energies()
    if current_tier_idx == len(tier_energies) - 1:
        return float(max(tier_energies[current_tier_idx]))
    return float(tier_energies[current_tier_idx + 1][0] - 1)


def _official_scenario_energies(resource_scenario: dict, difficulty: Optional[str], score: float) -> dict:
    resource_tier = _resource_tier_for_difficulty(resource_scenario, difficulty)
    if resource_tier is None:
        return {"voltaic_energy": 0, "voltaic_uncapped_energy": 0}

    current_tier_idx = _tier_index_by_id()[resource_tier["tier_id"]]
    uncapped_energy = _uncapped_scenario_energy(score, resource_tier["thresholds"], current_tier_idx)
    energy_cap = _tier_energy_cap(current_tier_idx)
    return {
        "voltaic_energy": floor(min(energy_cap, uncapped_energy)),
        "voltaic_uncapped_energy": floor(uncapped_energy),
    }


def _progress_percent(score: float, current_entry: Optional[dict], next_entry: dict) -> float:
    if current_entry is None:
        return min(max(score / next_entry["threshold"] * 100.0, 0.0), 100.0)

    score_range = next_entry["threshold"] - current_entry["threshold"]
    if score_range <= 0:
        return 100.0
    progress = (score - current_entry["threshold"]) / score_range * 100.0
    return min(max(progress, 0.0), 100.0)


def _energy_progress_percent(energy: float, current_rank: Optional[dict], next_rank: dict) -> float:
    if current_rank is None:
        return min(max(energy / next_rank["energy_threshold"] * 100.0, 0.0), 100.0)

    energy_range = next_rank["energy_threshold"] - current_rank["energy_threshold"]
    if energy_range <= 0:
        return 100.0
    progress = (energy - current_rank["energy_threshold"]) / energy_range * 100.0
    return min(max(progress, 0.0), 100.0)


def _rank_summary_for_energy(difficulty: str, energy: float) -> dict:
    tier = _tiers_by_difficulty().get(difficulty)
    result = {
        "rank": "Unranked",
        "rank_energy": 0,
        "next_rank": None,
        "next_rank_energy": None,
        "next_rank_progress_percent": None,
    }
    if tier is None:
        return result

    current_rank = None
    next_rank = None
    for rank in _ranks_by_tier().get(tier["id"], []):
        if energy >= rank["energy_threshold"]:
            current_rank = rank
        elif next_rank is None:
            next_rank = rank
            break

    if current_rank is not None:
        result["rank"] = _title_rank(current_rank["name"])
        result["rank_energy"] = current_rank["energy_threshold"]
    if next_rank is not None:
        result["next_rank"] = _title_rank(next_rank["name"])
        result["next_rank_energy"] = next_rank["energy_threshold"]
        result["next_rank_progress_percent"] = round(
            _energy_progress_percent(energy, current_rank, next_rank),
            1,
        )
    return result


def _harmonic_mean(values: list[float]) -> float:
    if not values or any(value <= 0 for value in values):
        return 0.0
    return len(values) / sum(1 / value for value in values)


def evaluate_score(scenario: dict, score: Optional[float]) -> dict:
    """Return Voltaic rank/progress/energy metadata for a fetched score row."""
    task_id = scenario.get("task_id")
    weapon_id = scenario.get("weapon_id")
    if isinstance(task_id, str) and isinstance(weapon_id, str):
        resource_scenario = _scenarios_by_key().get((task_id, weapon_id))
    else:
        resource_scenario = None
    result = {
        "voltaic_scenario": resource_scenario["name"] if resource_scenario else None,
        "voltaic_rank": None,
        "voltaic_rank_energy": None,
        "voltaic_energy": None,
        "voltaic_uncapped_energy": None,
        "next_rank": None,
        "next_rank_target_score": None,
        "next_rank_progress_percent": None,
    }
    if resource_scenario is None or not isinstance(score, (int, float)):
        return result

    threshold_entries = _threshold_entries(resource_scenario)
    current_entry = None
    next_entry = None
    for threshold_entry in threshold_entries:
        if score >= threshold_entry["threshold"]:
            current_entry = threshold_entry
        elif next_entry is None:
            next_entry = threshold_entry
            break

    result["voltaic_rank"] = current_entry["rank"] if current_entry else "Unranked"
    result["voltaic_rank_energy"] = current_entry["energy"] if current_entry else 0
    result.update(_official_scenario_energies(resource_scenario, scenario.get("difficulty"), float(score)))
    if next_entry is not None:
        result["next_rank"] = next_entry["rank"]
        result["next_rank_target_score"] = next_entry["threshold"]
        result["next_rank_progress_percent"] = round(_progress_percent(float(score), current_entry, next_entry), 1)
    return result


def add_voltaic_metrics(rows: list[dict]) -> list[dict]:
    enriched_rows = []
    for row in rows:
        enriched_row = dict(row)
        enriched_row.update(evaluate_score(row, row.get("score")))
        enriched_rows.append(enriched_row)
    return enriched_rows


def calculate_subcategory_energy(rows: list[dict]) -> list[dict]:
    energy_by_subcategory: dict[tuple[str, str, str], dict] = {}
    for row in rows:
        subcategory_key = (row["difficulty"], row["category"], row["sub"])
        if subcategory_key not in energy_by_subcategory:
            energy_by_subcategory[subcategory_key] = {
                "difficulty": row["difficulty"],
                "category": row["category"],
                "subcategory": row["sub"],
                "energy": 0.0,
                "uncapped_energy": 0.0,
                "source_scenario": None,
                "rank": "Unranked",
            }

        energy = row.get("voltaic_energy")
        if not isinstance(energy, (int, float)):
            continue
        uncapped_energy = row.get("voltaic_uncapped_energy", energy)
        if not isinstance(uncapped_energy, (int, float)):
            uncapped_energy = energy
        previous = energy_by_subcategory.get(subcategory_key)
        if (
            previous is None
            or previous["source_scenario"] is None
            or energy > previous["energy"]
            or (energy == previous["energy"] and uncapped_energy > previous["uncapped_energy"])
        ):
            energy_by_subcategory[subcategory_key] = {
                "difficulty": row["difficulty"],
                "category": row["category"],
                "subcategory": row["sub"],
                "energy": energy,
                "uncapped_energy": uncapped_energy,
                "source_scenario": row["name"],
                "rank": row.get("voltaic_rank"),
            }

    return list(energy_by_subcategory.values())


def calculate_difficulty_overall_rank(rows: list[dict]) -> list[dict]:
    """Calculate overall rank from official Voltaic subcategory energies.

    Voltaic's reference implementation floors the harmonic mean of all capped
    subcategory energies. If the capped energy reaches the highest rank of the
    highest tier, it uses the uncapped harmonic mean for the celestial uncap.
    https://app.voltaic.gg/leaderboards/about
    https://github.com/VoltaicHQ/energy-calculation/blob/main/energy_tutorial.md
    """
    subcategory_summaries = calculate_subcategory_energy(rows)
    summaries_by_difficulty: dict[str, list[dict]] = {}
    for summary in subcategory_summaries:
        summaries_by_difficulty.setdefault(summary["difficulty"], []).append(summary)

    difficulty_order = {difficulty: difficulty_idx for difficulty_idx, difficulty in enumerate(_tiers_by_difficulty())}
    overall_summaries = []
    for difficulty in sorted(
        summaries_by_difficulty,
        key=lambda summary_difficulty: difficulty_order.get(summary_difficulty, len(difficulty_order)),
    ):
        summaries = summaries_by_difficulty[difficulty]
        capped_energies = [float(summary["energy"]) for summary in summaries]
        uncapped_energies = [float(summary.get("uncapped_energy", summary["energy"])) for summary in summaries]
        capped_overall_energy = floor(_harmonic_mean(capped_energies))
        uncapped_overall_energy = floor(_harmonic_mean(uncapped_energies))
        max_rank_energy = max(max(tier_energies) for tier_energies in _tier_energies())
        if capped_overall_energy >= max_rank_energy:
            overall_energy = uncapped_overall_energy
        else:
            overall_energy = capped_overall_energy
        rank_summary = _rank_summary_for_energy(difficulty, overall_energy)
        overall_summaries.append(
            {
                "difficulty": difficulty,
                "energy": overall_energy,
                "rank": rank_summary["rank"],
                "rank_energy": rank_summary["rank_energy"],
                "next_rank": rank_summary["next_rank"],
                "next_rank_energy": rank_summary["next_rank_energy"],
                "next_rank_progress_percent": rank_summary["next_rank_progress_percent"],
                "subcategory_count": len(summaries),
            }
        )
    return overall_summaries
