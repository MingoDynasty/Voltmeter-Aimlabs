import copy
import unittest
from unittest.mock import patch

import benchmark_constants
from voltaic_benchmarks import load_valorant_s1


class BenchmarkConstantsTests(unittest.TestCase):
    def tearDown(self) -> None:
        benchmark_constants.get_benchmarks.cache_clear()

    def test_difficulties_do_not_require_resource_load(self) -> None:
        with patch(
            "benchmark_constants.load_valorant_s1",
            side_effect=AssertionError("resource loaded"),
        ):
            self.assertEqual(
                benchmark_constants.DIFFICULTIES,
                ("novice", "intermediate", "advanced"),
            )

    def test_unknown_category_id_raises_clear_error(self) -> None:
        resource_data = copy.deepcopy(load_valorant_s1())
        resource_data["scenarios"][0]["category_id"] = 999999

        with patch("benchmark_constants.load_valorant_s1", return_value=resource_data):
            with self.assertRaisesRegex(ValueError, "Unknown category_id 999999"):
                benchmark_constants.get_benchmarks()


if __name__ == "__main__":
    unittest.main()
