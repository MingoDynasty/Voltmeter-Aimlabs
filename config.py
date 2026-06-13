"""Application configuration loading."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib
from typing import Any, Optional, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.toml")
REPORT_FAMILIES = ("all", "valorant")


class ConfigError(RuntimeError):
    """Raised when config.toml exists but cannot be used."""


@dataclass(frozen=True)
class AppConfig:  # pylint: disable=too-many-instance-attributes
    aimlabs_user_id: Optional[str] = None
    storage_db_path: Optional[str] = None
    sync_page_size: int = 50
    sync_request_delay_seconds: float = 0.25
    sync_request_timeout_seconds: float = 20.0
    report_family: str = "all"
    report_timezone: str = "local"
    report_rolling_median_window: int = 10
    report_rolling_max_window: int = 25


def load_config(config_path: Optional[Union[str, Path]] = None) -> AppConfig:
    resolved_path = Path(config_path) if config_path is not None else DEFAULT_CONFIG_PATH
    if not resolved_path.exists():
        return AppConfig()

    config_data = _load_toml(resolved_path)
    aimlabs_config = config_data.get("aimlabs", {})
    if not isinstance(aimlabs_config, dict):
        raise ConfigError(f"{resolved_path} [aimlabs] must be a table.")
    storage_config = _optional_table(config_data, "storage", resolved_path)
    sync_config = _optional_table(config_data, "sync", resolved_path)
    report_config = _optional_table(config_data, "report", resolved_path)

    user_id = aimlabs_config.get("user_id")
    if user_id is None:
        aimlabs_user_id = None
    elif not isinstance(user_id, str):
        raise ConfigError(f"{resolved_path} [aimlabs].user_id must be a string.")
    else:
        aimlabs_user_id = user_id.strip() or None

    return AppConfig(
        aimlabs_user_id=aimlabs_user_id,
        storage_db_path=_optional_non_empty_string(storage_config, "db_path", resolved_path, "storage"),
        sync_page_size=_sync_page_size(sync_config, resolved_path),
        sync_request_delay_seconds=_optional_number(
            sync_config,
            "request_delay_seconds",
            resolved_path,
            "sync",
            default=0.25,
        ),
        sync_request_timeout_seconds=_optional_number(
            sync_config,
            "request_timeout_seconds",
            resolved_path,
            "sync",
            default=20.0,
        ),
        report_family=_report_family(report_config, resolved_path),
        report_timezone=_report_timezone(report_config, resolved_path),
        report_rolling_median_window=_positive_integer(
            report_config,
            "rolling_median_window",
            resolved_path,
            "report",
            default=10,
        ),
        report_rolling_max_window=_positive_integer(
            report_config,
            "rolling_max_window",
            resolved_path,
            "report",
            default=25,
        ),
    )


def _load_toml(config_path: Path) -> dict[str, Any]:
    try:
        with config_path.open("rb") as config_file:
            config_data = tomllib.load(config_file)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"Could not parse {config_path}: {error}") from error

    if not isinstance(config_data, dict):
        raise ConfigError(f"{config_path} must contain a TOML table.")
    return config_data


def _optional_table(config_data: dict[str, Any], section: str, config_path: Path) -> dict[str, Any]:
    section_config = config_data.get(section, {})
    if not isinstance(section_config, dict):
        raise ConfigError(f"{config_path} [{section}] must be a table.")
    return section_config


def _optional_non_empty_string(
    section_config: dict[str, Any],
    key: str,
    config_path: Path,
    section: str,
) -> Optional[str]:
    value = section_config.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{config_path} [{section}].{key} must be a string.")
    return value.strip() or None


def _sync_page_size(sync_config: dict[str, Any], config_path: Path) -> int:
    value = sync_config.get("page_size", 50)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{config_path} [sync].page_size must be an integer.")
    if not 1 <= value <= 200:
        raise ConfigError(f"{config_path} [sync].page_size must be between 1 and 200.")
    return value


def _report_family(report_config: dict[str, Any], config_path: Path) -> str:
    value = report_config.get("family", "all")
    if not isinstance(value, str):
        raise ConfigError(f"{config_path} [report].family must be a string.")
    if value not in REPORT_FAMILIES:
        families_text = " or ".join(f'"{family}"' for family in REPORT_FAMILIES)
        raise ConfigError(f"{config_path} [report].family must be {families_text}.")
    return value


def _report_timezone(report_config: dict[str, Any], config_path: Path) -> str:
    value = report_config.get("timezone", "local")
    if not isinstance(value, str):
        raise ConfigError(f"{config_path} [report].timezone must be a string.")
    if value == "local":
        return value
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ConfigError(f'{config_path} [report].timezone must be "local" or a valid IANA zone name.') from error
    return value


def _positive_integer(
    section_config: dict[str, Any],
    key: str,
    config_path: Path,
    section: str,
    *,
    default: int,
) -> int:
    value = section_config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{config_path} [{section}].{key} must be an integer.")
    if value < 1:
        raise ConfigError(f"{config_path} [{section}].{key} must be a positive integer.")
    return value


def _optional_number(
    section_config: dict[str, Any],
    key: str,
    config_path: Path,
    section: str,
    *,
    default: float,
) -> float:
    value = section_config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{config_path} [{section}].{key} must be numeric.")
    if value < 0:
        raise ConfigError(f"{config_path} [{section}].{key} must be greater than or equal to zero.")
    return float(value)
