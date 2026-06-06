"""Voltaic benchmark resource loading and score evaluation."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Optional

RESOURCE_PATH = Path(__file__).resolve().parent / "resources" / "aimlabs" / "valorant_s1.json"


@lru_cache(maxsize=1)
def load_valorant_s1() -> dict:
    return json.loads(RESOURCE_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _ranks_by_tier() -> dict[int, list[dict]]:
    ranks_by_tier: dict[int, list[dict]] = {}
    for rank in load_valorant_s1()["ranks"]:
        ranks_by_tier.setdefault(rank["tier_id"], []).append(rank)
    for ranks in ranks_by_tier.values():
        ranks.sort(key=lambda rank: rank["energy_threshold"])
    return ranks_by_tier


@lru_cache(maxsize=1)
def _tiers_by_difficulty() -> dict[str, dict]:
    return {
        tier["name"].lower(): tier
        for tier in load_valorant_s1()["tiers"]
    }


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
            entries.append({
                "threshold": threshold,
                "rank": _title_rank(rank["name"]),
                "energy": rank["energy_threshold"],
                "tier_id": tier["tier_id"],
            })
    return sorted(entries, key=lambda entry: entry["energy"])


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


def _interpolated_energy(score: float, current_entry: Optional[dict], next_entry: Optional[dict]) -> float:
    if next_entry is None:
        return float(current_entry["energy"]) if current_entry else 0.0

    progress = _progress_percent(score, current_entry, next_entry) / 100.0
    if current_entry is None:
        return progress * next_entry["energy"]
    return current_entry["energy"] + progress * (next_entry["energy"] - current_entry["energy"])


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
    resource_scenario = _scenarios_by_key().get((scenario["task_id"], scenario["weapon_id"]))
    result = {
        "voltaic_scenario": resource_scenario["name"] if resource_scenario else None,
        "voltaic_rank": None,
        "voltaic_rank_energy": None,
        "voltaic_energy": None,
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
    result["voltaic_energy"] = round(_interpolated_energy(float(score), current_entry, next_entry), 1)
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
                "source_scenario": None,
                "rank": "Unranked",
            }

        energy = row.get("voltaic_energy")
        if not isinstance(energy, (int, float)):
            continue
        previous = energy_by_subcategory.get(subcategory_key)
        if previous is None or previous["source_scenario"] is None or energy > previous["energy"]:
            energy_by_subcategory[subcategory_key] = {
                "difficulty": row["difficulty"],
                "category": row["category"],
                "subcategory": row["sub"],
                "energy": energy,
                "source_scenario": row["name"],
                "rank": row.get("voltaic_rank"),
            }

    return list(energy_by_subcategory.values())


def calculate_difficulty_overall_rank(rows: list[dict]) -> list[dict]:
    subcategory_summaries = calculate_subcategory_energy(rows)
    summaries_by_difficulty: dict[str, list[dict]] = {}
    for summary in subcategory_summaries:
        summaries_by_difficulty.setdefault(summary["difficulty"], []).append(summary)

    overall_summaries = []
    for difficulty, summaries in summaries_by_difficulty.items():
        energies = [float(summary["energy"]) for summary in summaries]
        overall_energy = round(_harmonic_mean(energies), 1)
        rank_summary = _rank_summary_for_energy(difficulty, overall_energy)
        overall_summaries.append({
            "difficulty": difficulty,
            "energy": overall_energy,
            "rank": rank_summary["rank"],
            "rank_energy": rank_summary["rank_energy"],
            "next_rank": rank_summary["next_rank"],
            "next_rank_energy": rank_summary["next_rank_energy"],
            "next_rank_progress_percent": rank_summary["next_rank_progress_percent"],
            "subcategory_count": len(summaries),
        })
    return overall_summaries
