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


def _interpolated_energy(score: float, current_entry: Optional[dict], next_entry: Optional[dict]) -> float:
    if next_entry is None:
        return float(current_entry["energy"]) if current_entry else 0.0

    progress = _progress_percent(score, current_entry, next_entry) / 100.0
    if current_entry is None:
        return progress * next_entry["energy"]
    return current_entry["energy"] + progress * (next_entry["energy"] - current_entry["energy"])


def evaluate_score(scenario: dict, score: Optional[float]) -> dict:
    """Return Voltaic rank/progress/energy metadata for a fetched score row."""
    resource_scenario = _scenarios_by_key().get((scenario["task_id"], scenario["weapon_id"]))
    result = {
        "voltaic_scenario": resource_scenario["name"] if resource_scenario else None,
        "voltaic_rank": None,
        "voltaic_rank_energy": None,
        "voltaic_energy": None,
        "next_rank": None,
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
    energy_by_subcategory: dict[tuple[str, str], dict] = {}
    for row in rows:
        energy = row.get("voltaic_energy")
        if not isinstance(energy, (int, float)):
            continue
        subcategory_key = (row["category"], row["sub"])
        previous = energy_by_subcategory.get(subcategory_key)
        if previous is None or energy > previous["energy"]:
            energy_by_subcategory[subcategory_key] = {
                "category": row["category"],
                "subcategory": row["sub"],
                "energy": energy,
                "source_scenario": row["name"],
                "rank": row.get("voltaic_rank"),
            }

    return list(energy_by_subcategory.values())
