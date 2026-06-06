import unittest

from voltaic_benchmarks import (
    _uncapped_scenario_energy,
    calculate_difficulty_overall_rank,
    calculate_subcategory_energy,
    evaluate_score,
)


class VoltaicBenchmarkTests(unittest.TestCase):
    def test_evaluate_score_includes_next_rank_target_score(self) -> None:
        scenario = {
            "task_id": "CsLevel.Lowgravity56.VT Float.RSM6A6",
            "weapon_id": "Custom_LG56CLICKER4",
            "difficulty": "novice",
        }

        result = evaluate_score(scenario, 450)

        self.assertEqual(result["voltaic_rank"], "Iron")
        self.assertEqual(result["next_rank"], "Bronze")
        self.assertEqual(result["next_rank_target_score"], 500)
        self.assertEqual(result["next_rank_progress_percent"], 50.0)
        self.assertEqual(result["voltaic_energy"], 150)

    def test_energy_formula_matches_official_tutorial_example(self) -> None:
        self.assertAlmostEqual(
            _uncapped_scenario_energy(900, [660, 760, 850, 940], 1),
            755.5555555555555,
        )

    def test_score_rank_can_exceed_capped_energy_contribution(self) -> None:
        scenario = {
            "task_id": "CsLevel.Lowgravity56.VT Adjus.RTUQMP",
            "weapon_id": "Custom_LG56Clicker3",
            "difficulty": "novice",
        }

        result = evaluate_score(scenario, 1050)

        self.assertEqual(result["voltaic_rank"], "Immortal")
        self.assertEqual(result["voltaic_energy"], 499)
        self.assertEqual(result["voltaic_uncapped_energy"], 850)
        self.assertGreater(result["voltaic_uncapped_energy"], result["voltaic_energy"])

    def test_novice_only_scenario_caps_at_difficulty_cap(self) -> None:
        scenario = {
            "task_id": "CsLevel.Lowgravity56.VT Float.RSM6A6",
            "weapon_id": "Custom_LG56CLICKER4",
            "difficulty": "novice",
        }

        result = evaluate_score(scenario, 9999)

        self.assertEqual(result["voltaic_rank"], "Gold")
        self.assertEqual(result["voltaic_energy"], 499)
        self.assertEqual(result["voltaic_uncapped_energy"], 9699)

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
        self.assertEqual(summaries[0]["energy"], 133)
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

    def test_overall_rank_order_follows_benchmark_difficulties(self) -> None:
        rows = [
            _row("advanced", "Micros", "Core", "Advanced Scenario", 900, "Ascendant"),
            _row("novice", "Micros", "Core", "Novice Scenario", 100, "Iron"),
            _row("intermediate", "Micros", "Core", "Intermediate Scenario", 500, "Gold"),
        ]

        summaries = calculate_difficulty_overall_rank(rows)

        self.assertEqual(
            [summary["difficulty"] for summary in summaries],
            ["novice", "intermediate", "advanced"],
        )

    def test_intermediate_overall_rank_matches_known_voltaic_energy(self) -> None:
        rows = [
            _row("intermediate", "Flick-tech", "Dynamic", "Dynamic Scenario", 826, "Immortal"),
            _row("intermediate", "Flick-tech", "Core", "Core Scenario", 825, "Immortal"),
            _row("intermediate", "Flick-tech", "Reflex", "Reflex Scenario", 888, "Immortal"),
            _row("intermediate", "Micros", "Evasive", "Evasive Scenario", 829, "Immortal"),
            _row("intermediate", "Micros", "Core", "Core Micro Scenario", 802, "Immortal"),
            _row("intermediate", "Micros", "Reflex", "Reflex Micro Scenario", 872, "Immortal"),
            _row("intermediate", "Stability", "Strafe", "Strafe Scenario", 819, "Immortal"),
            _row("intermediate", "Stability", "Precise", "Precise Scenario", 899, "Immortal"),
        ]

        summaries = calculate_difficulty_overall_rank(rows)

        self.assertEqual(summaries[0]["difficulty"], "intermediate")
        self.assertEqual(summaries[0]["energy"], 843)
        self.assertEqual(summaries[0]["rank"], "Immortal")


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
