import unittest

from voltaic_benchmarks import (
    calculate_difficulty_overall_rank,
    calculate_subcategory_energy,
    evaluate_score,
)


class VoltaicBenchmarkTests(unittest.TestCase):
    def test_evaluate_score_includes_next_rank_target_score(self) -> None:
        scenario = {
            "task_id": "CsLevel.Lowgravity56.VT Float.RSM6A6",
            "weapon_id": "Custom_LG56CLICKER4",
        }

        result = evaluate_score(scenario, 450)

        self.assertEqual(result["voltaic_rank"], "Iron")
        self.assertEqual(result["next_rank"], "Bronze")
        self.assertEqual(result["next_rank_target_score"], 500)
        self.assertEqual(result["next_rank_progress_percent"], 50.0)

    def test_subcategory_energy_is_separated_by_difficulty(self) -> None:
        rows = [
            _row("novice", "Micros", "Core", "Novice Scenario", 100, "Iron"),
            _row("advanced", "Micros", "Core", "Advanced Scenario", 900, "Ascendant"),
        ]

        summaries = calculate_subcategory_energy(rows)

        summary_keys = {
            (summary["difficulty"], summary["category"], summary["subcategory"])
            for summary in summaries
        }
        self.assertEqual(summary_keys, {
            ("novice", "Micros", "Core"),
            ("advanced", "Micros", "Core"),
        })

    def test_overall_rank_uses_harmonic_mean_of_subcategory_energy(self) -> None:
        rows = [
            _row("novice", "Micros", "Core", "Core Scenario", 100, "Iron"),
            _row("novice", "Micros", "Reflex", "Reflex Scenario", 200, "Bronze"),
        ]

        summaries = calculate_difficulty_overall_rank(rows)

        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["difficulty"], "novice")
        self.assertEqual(summaries[0]["energy"], 133.3)
        self.assertEqual(summaries[0]["rank"], "Iron")
        self.assertEqual(summaries[0]["subcategory_count"], 2)

    def test_overall_rank_is_unranked_when_a_subcategory_has_no_energy(self) -> None:
        rows = [
            _row("novice", "Micros", "Core", "Core Scenario", 100, "Iron"),
            {
                "difficulty": "novice",
                "category": "Micros",
                "sub": "Reflex",
                "name": "Reflex Scenario",
                "voltaic_energy": None,
                "voltaic_rank": None,
            },
        ]

        summaries = calculate_difficulty_overall_rank(rows)

        self.assertEqual(summaries[0]["energy"], 0.0)
        self.assertEqual(summaries[0]["rank"], "Unranked")
        self.assertEqual(summaries[0]["next_rank"], "Iron")


def _row(
    difficulty: str,
    category: str,
    subcategory: str,
    scenario_name: str,
    energy: float,
    rank: str,
) -> dict:
    return {
        "difficulty": difficulty,
        "category": category,
        "sub": subcategory,
        "name": scenario_name,
        "voltaic_energy": energy,
        "voltaic_rank": rank,
    }


if __name__ == "__main__":
    unittest.main()
