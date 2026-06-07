import json
import tempfile
import unittest
from pathlib import Path

import scenario_catalog
from scenario_catalog import ScenarioCatalogError, UNKNOWN_SCENARIO_NAME, refresh_catalog


class ScenarioCatalogTests(unittest.TestCase):
    def tearDown(self) -> None:
        scenario_catalog.load_catalog.cache_clear()

    def test_load_catalog_caches_bundled_catalog(self) -> None:
        first_catalog = scenario_catalog.load_catalog()
        second_catalog = scenario_catalog.load_catalog()

        self.assertIs(first_catalog, second_catalog)
        self.assertTrue(first_catalog.records_for_task_id("CsLevel.Lowgravity56.VT Float.RSM6A6"))

    def test_module_resolve_task_ids_uses_cached_catalog_and_retains_unknown(self) -> None:
        resolved_records = scenario_catalog.resolve_task_ids(
            [
                "CsLevel.Lowgravity56.VT Float.RSM6A6",
                "missing-task",
            ]
        )

        self.assertTrue(resolved_records["CsLevel.Lowgravity56.VT Float.RSM6A6"][0].is_known)
        self.assertFalse(resolved_records["missing-task"][0].is_known)
        self.assertEqual(resolved_records["missing-task"][0].name, UNKNOWN_SCENARIO_NAME)

    def test_bundled_catalog_carries_product_surface_fields(self) -> None:
        catalog = refresh_catalog()

        valorant_record = catalog.records_for_task_id("CsLevel.Lowgravity56.VT Float.RSM6A6")[0]
        self.assertEqual(valorant_record.name, "Floatshot")
        self.assertEqual(valorant_record.category, "Flick-tech")
        self.assertEqual(valorant_record.sub, "Dynamic")
        self.assertEqual(valorant_record.difficulty, "novice")
        self.assertEqual(valorant_record.season, "1")
        self.assertEqual(valorant_record.benchmark_alias, "valorant_s1")
        self.assertEqual(valorant_record.benchmark_name, "Voltaic Valorant Benchmarks")
        self.assertEqual(valorant_record.family, "valorant")
        self.assertTrue(valorant_record.is_active)
        self.assertTrue(valorant_record.has_leaderboards)

        aimlabs_s2_record = catalog.records_for_task_id("CsLevel.VT Empyrean.VT Angle.RB668Z")[0]
        self.assertEqual(aimlabs_s2_record.name, "Angleshot")
        self.assertEqual(aimlabs_s2_record.family, "aimlabs")
        self.assertEqual(aimlabs_s2_record.season, "2")
        self.assertEqual(aimlabs_s2_record.benchmark_alias, "aimlabs_s2")
        self.assertFalse(aimlabs_s2_record.has_leaderboards)

        aimlabs_s3_record = catalog.records_for_task_id("CsLevel.VT Lowgravity56.VT Angle.SGAB0P")[0]
        self.assertEqual(aimlabs_s3_record.name, "Angleshot")
        self.assertEqual(aimlabs_s3_record.family, "aimlabs")
        self.assertEqual(aimlabs_s3_record.season, "3")
        self.assertTrue(aimlabs_s3_record.has_leaderboards)

    def test_refresh_catalog_unions_resource_files_and_resolves_by_exact_task_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            resource_dir = Path(temp_dir)
            _write_resource(
                resource_dir,
                "valorant_s1.json",
                alias="valorant_s1",
                benchmark_name="Voltaic Valorant Benchmarks",
                season="1",
                scenarios=[_scenario("VT Shared Name VALORANT Easy", "task-valorant")],
            )
            _write_resource(
                resource_dir,
                "aimlabs_s2.json",
                alias="aimlabs_s2",
                benchmark_name="Voltaic Aimlabs Benchmarks",
                season="2",
                has_leaderboards=False,
                scenarios=[_scenario("VT Shared Name Novice", "task-aimlabs")],
            )

            catalog = refresh_catalog(resource_dir)
            resolved_records = catalog.resolve_task_ids(["task-valorant", "task-aimlabs"])

        self.assertEqual(len(catalog.records), 2)
        self.assertEqual(set(resolved_records), {"task-valorant", "task-aimlabs"})
        self.assertEqual(resolved_records["task-valorant"][0].family, "valorant")
        self.assertEqual(resolved_records["task-aimlabs"][0].family, "aimlabs")
        self.assertEqual(resolved_records["task-valorant"][0].name, "Shared Name")
        self.assertEqual(resolved_records["task-aimlabs"][0].name, "Shared Name")
        self.assertNotEqual(resolved_records["task-valorant"][0].task_id, resolved_records["task-aimlabs"][0].task_id)

    def test_unknown_task_ids_are_retained_and_labelled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            resource_dir = Path(temp_dir)
            _write_resource(
                resource_dir,
                "aimlabs_s2.json",
                alias="aimlabs_s2",
                benchmark_name="Voltaic Aimlabs Benchmarks",
                season="2",
                scenarios=[_scenario("VT Known Novice", "known-task")],
            )

            catalog = refresh_catalog(resource_dir)
            resolved_records = catalog.resolve_task_ids(["known-task", "missing-task"])

        self.assertTrue(resolved_records["known-task"][0].is_known)
        unknown_record = resolved_records["missing-task"][0]
        self.assertFalse(unknown_record.is_known)
        self.assertEqual(unknown_record.task_id, "missing-task")
        self.assertEqual(unknown_record.name, UNKNOWN_SCENARIO_NAME)
        self.assertEqual(unknown_record.family, "unknown")
        self.assertEqual(catalog.unknown_task_ids(["known-task", "missing-task"]), ("missing-task",))

    def test_duplicate_task_id_policy_prefers_newest_source_and_keeps_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            resource_dir = Path(temp_dir)
            _write_resource(
                resource_dir,
                "valorant_s1.json",
                alias="valorant_s1",
                benchmark_name="Voltaic Valorant Benchmarks",
                season="1",
                scenarios=[_scenario("VT Old Name VALORANT Easy", "shared-task")],
            )
            _write_resource(
                resource_dir,
                "aimlabs_s3.json",
                alias="aimlabs_s3",
                benchmark_name="Voltaic Aimlabs Benchmarks",
                season="3",
                scenarios=[_scenario("VT New Name Novice S3", "shared-task")],
            )

            catalog = refresh_catalog(resource_dir)

        records = catalog.records_for_task_id("shared-task")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].benchmark_alias, "aimlabs_s3")
        self.assertEqual(records[0].name, "New Name")
        self.assertEqual(catalog.duplicate_task_ids, ("shared-task",))
        shadowed_records = catalog.shadowed_records_for_task_id("shared-task")
        self.assertEqual(len(shadowed_records), 1)
        self.assertEqual(shadowed_records[0].benchmark_alias, "valorant_s1")

    def test_multitier_task_id_keeps_all_difficulty_records_from_same_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            resource_dir = Path(temp_dir)
            _write_resource(
                resource_dir,
                "valorant_s1.json",
                alias="valorant_s1",
                benchmark_name="Voltaic Valorant Benchmarks",
                season="1",
                scenarios=[_scenario("VT Multitier VALORANT", "multi-tier-task", tier_ids=(1, 2))],
            )

            catalog = refresh_catalog(resource_dir)

        records = catalog.records_for_task_id("multi-tier-task")
        self.assertEqual([record.difficulty for record in records], ["novice", "intermediate"])
        self.assertEqual(catalog.duplicate_task_ids, ())

    def test_malformed_resource_shape_raises_catalog_error_with_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            resource_dir = Path(temp_dir)
            (resource_dir / "aimlabs_s2.json").write_text(json.dumps({"alias": "aimlabs_s2"}), encoding="utf-8")

            with self.assertRaisesRegex(
                ScenarioCatalogError, r"Malformed scenario resource .*aimlabs_s2\.json.*categories"
            ):
                refresh_catalog(resource_dir)

    def test_unknown_family_alias_raises_catalog_error_with_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            resource_dir = Path(temp_dir)
            _write_resource(
                resource_dir,
                "mystery_s1.json",
                alias="mystery_s1",
                benchmark_name="Mystery Benchmarks",
                season="1",
                scenarios=[_scenario("VT Mystery Novice", "mystery-task")],
            )

            with self.assertRaisesRegex(
                ScenarioCatalogError,
                r"Malformed scenario resource .*mystery_s1\.json.*Unknown benchmark family",
            ):
                refresh_catalog(resource_dir)


def _write_resource(
    resource_dir: Path,
    filename: str,
    *,
    alias: str,
    benchmark_name: str,
    season: str,
    scenarios: list[dict],
    has_leaderboards: bool = True,
) -> None:
    resource_data = {
        "alias": alias,
        "name": benchmark_name,
        "season": season,
        "has_leaderboards": has_leaderboards,
        "is_active": True,
        "tiers": [
            {"id": 1, "name": "Novice"},
            {"id": 2, "name": "Intermediate"},
            {"id": 3, "name": "Advanced"},
        ],
        "categories": [
            {
                "id": 1,
                "name": "clicking",
                "subcategories": [
                    {"id": 10, "name": "dynamic"},
                    {"id": 11, "name": "static"},
                ],
            }
        ],
        "scenarios": scenarios,
    }
    (resource_dir / filename).write_text(json.dumps(resource_data), encoding="utf-8")


def _scenario(name: str, task_id: str, *, tier_ids: tuple[int, ...] = (1,)) -> dict:
    return {
        "name": name,
        "task_id": task_id,
        "task_mode": 42,
        "weapon_id": "SyntheticWeapon",
        "category_id": 1,
        "subcategory_id": 10,
        "tiers": [{"tier_id": tier_id, "thresholds": [100]} for tier_id in tier_ids],
    }


if __name__ == "__main__":
    unittest.main()
