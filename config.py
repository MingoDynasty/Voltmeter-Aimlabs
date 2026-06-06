"""Application configuration loading."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.toml")


class ConfigError(RuntimeError):
    """Raised when config.toml exists but cannot be used."""


@dataclass(frozen=True)
class AppConfig:
    aimlabs_user_id: Optional[str] = None


def load_config(config_path: Optional[Union[str, Path]] = None) -> AppConfig:
    resolved_path = Path(config_path) if config_path is not None else DEFAULT_CONFIG_PATH
    if not resolved_path.exists():
        return AppConfig()

    config_data = _load_toml(resolved_path)
    aimlabs_config = config_data.get("aimlabs", {})
    if not isinstance(aimlabs_config, dict):
        raise ConfigError(f"{resolved_path} [aimlabs] must be a table.")

    user_id = aimlabs_config.get("user_id")
    if user_id is None:
        return AppConfig()
    if not isinstance(user_id, str):
        raise ConfigError(f"{resolved_path} [aimlabs].user_id must be a string.")

    return AppConfig(aimlabs_user_id=user_id.strip() or None)


def _load_toml(config_path: Path) -> dict[str, Any]:
    try:
        import tomllib as toml_library
    except ModuleNotFoundError:
        try:
            import tomli as toml_library
        except ModuleNotFoundError:
            return _load_basic_toml(config_path)

    try:
        with config_path.open("rb") as config_file:
            config_data = toml_library.load(config_file)
    except toml_library.TOMLDecodeError as error:
        raise ConfigError(f"Could not parse {config_path}: {error}") from error

    if not isinstance(config_data, dict):
        raise ConfigError(f"{config_path} must contain a TOML table.")
    return config_data


def _load_basic_toml(config_path: Path) -> dict[str, Any]:
    config_data: dict[str, Any] = {}
    active_section = config_data

    for line_number, raw_line in enumerate(config_path.read_text(encoding="utf-8").splitlines(), start=1):
        line_text = raw_line.split("#", 1)[0].strip()
        if not line_text:
            continue

        if line_text.startswith("[") and line_text.endswith("]"):
            section_name = line_text[1:-1].strip()
            if not section_name:
                raise ConfigError(f"{config_path}:{line_number} has an empty section name.")
            section_data = config_data.setdefault(section_name, {})
            if not isinstance(section_data, dict):
                raise ConfigError(f"{config_path}:{line_number} section conflicts with a value.")
            active_section = section_data
            continue

        if "=" not in line_text:
            raise ConfigError(f"{config_path}:{line_number} expected key = value.")
        key_text, value_text = line_text.split("=", 1)
        key_name = key_text.strip()
        if not key_name:
            raise ConfigError(f"{config_path}:{line_number} has an empty key.")
        active_section[key_name] = _parse_basic_toml_value(config_path, line_number, value_text.strip())

    return config_data


def _parse_basic_toml_value(config_path: Path, line_number: int, value_text: str) -> str:
    if len(value_text) >= 2 and value_text[0] == value_text[-1] == '"':
        return value_text[1:-1]
    raise ConfigError(f"{config_path}:{line_number} only quoted string values are supported.")
