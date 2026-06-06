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


if __name__ == "__main__":
    unittest.main()
