import tempfile
import unittest
from pathlib import Path

from config import ConfigError, load_config


class ConfigTests(unittest.TestCase):
    def test_missing_file_returns_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "missing.toml"

            config = load_config(config_path)

        self.assertIsNone(config.aimlabs_user_id)
        self.assertIsNone(config.aimlabs_session_cookie)
        self.assertIsNone(config.storage_db_path)
        self.assertEqual(config.sync_page_size, 50)
        self.assertEqual(config.sync_request_delay_seconds, 0.25)
        self.assertEqual(config.sync_request_timeout_seconds, 20.0)
        self.assertEqual(config.report_family, "all")
        self.assertEqual(config.report_timezone, "local")
        self.assertEqual(config.report_rolling_median_window, 10)
        self.assertEqual(config.report_rolling_max_window, 25)

    def test_bad_aimlabs_type_raises_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text('aimlabs = "bad"\n', encoding="utf-8")

            with self.assertRaises(ConfigError):
                load_config(config_path)

    def test_non_string_user_id_raises_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text("[aimlabs]\nuser_id = 123\n", encoding="utf-8")

            with self.assertRaises(ConfigError):
                load_config(config_path)

    def test_valid_config_loads_user_id_and_session_cookie(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(
                '[aimlabs]\nuser_id = "ABCDEF1234567890"\nsession_cookie = "cookie-value"\n',
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertEqual(config.aimlabs_user_id, "ABCDEF1234567890")
        self.assertEqual(config.aimlabs_session_cookie, "cookie-value")

    def test_valid_storage_and_sync_config_loads_pinned_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(
                "[storage]\n"
                'db_path = "custom/aimlabs.db"\n'
                "[sync]\n"
                "page_size = 100\n"
                "request_delay_seconds = 0.5\n"
                "request_timeout_seconds = 30\n",
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertEqual(config.storage_db_path, "custom/aimlabs.db")
        self.assertEqual(config.sync_page_size, 100)
        self.assertEqual(config.sync_request_delay_seconds, 0.5)
        self.assertEqual(config.sync_request_timeout_seconds, 30.0)

    def test_sync_page_size_out_of_bounds_raises_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text("[sync]\npage_size = 0\n", encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "page_size"):
                load_config(config_path)

    def test_valid_report_config_loads_pinned_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(
                "[report]\n"
                'family = "valorant"\n'
                'timezone = "America/Los_Angeles"\n'
                "rolling_median_window = 5\n"
                "rolling_max_window = 50\n",
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertEqual(config.report_family, "valorant")
        self.assertEqual(config.report_timezone, "America/Los_Angeles")
        self.assertEqual(config.report_rolling_median_window, 5)
        self.assertEqual(config.report_rolling_max_window, 50)

    def test_report_family_outside_allowed_values_raises_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text('[report]\nfamily = "kovaaks"\n', encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, r"\[report\].family"):
                load_config(config_path)

    def test_report_timezone_not_resolvable_raises_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text('[report]\ntimezone = "Not/AZone"\n', encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, r"\[report\].timezone"):
                load_config(config_path)

    def test_report_rolling_window_non_positive_raises_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text("[report]\nrolling_median_window = 0\n", encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, r"\[report\].rolling_median_window"):
                load_config(config_path)

    def test_report_rolling_window_non_integer_raises_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text("[report]\nrolling_max_window = true\n", encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, r"\[report\].rolling_max_window"):
                load_config(config_path)


if __name__ == "__main__":
    unittest.main()
