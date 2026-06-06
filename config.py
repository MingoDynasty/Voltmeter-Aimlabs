"""Application configuration loading."""
from __future__ import annotations

from dataclasses import dataclass
import importlib
from pathlib import Path
from typing import Any, Optional, Union

DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.toml")


class ConfigError(RuntimeError):
    """Raised when config.toml exists but cannot be used."""


@dataclass(frozen=True)
class AppConfig:
    aimlabs_user_id: Optional[str] = None
    aimlabs_session_cookie: Optional[str] = None


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
        aimlabs_user_id = None
    elif not isinstance(user_id, str):
        raise ConfigError(f"{resolved_path} [aimlabs].user_id must be a string.")
    else:
        aimlabs_user_id = user_id.strip() or None

    session_cookie = aimlabs_config.get("session_cookie")
    if session_cookie is None:
        aimlabs_session_cookie = None
    elif not isinstance(session_cookie, str):
        raise ConfigError(f"{resolved_path} [aimlabs].session_cookie must be a string.")
    else:
        aimlabs_session_cookie = session_cookie.strip() or None

    return AppConfig(
        aimlabs_user_id=aimlabs_user_id,
        aimlabs_session_cookie=aimlabs_session_cookie,
    )


def _load_toml(config_path: Path) -> dict[str, Any]:
    try:
        toml_library = importlib.import_module("tomllib")
    except ModuleNotFoundError:
        try:
            toml_library = importlib.import_module("tomli")
        except ModuleNotFoundError:
            raise ConfigError(
                "TOML support requires Python 3.11+ or the tomli package. "
                "Install project dependencies, then try again."
            ) from None

    try:
        with config_path.open("rb") as config_file:
            config_data = toml_library.load(config_file)
    except toml_library.TOMLDecodeError as error:
        raise ConfigError(f"Could not parse {config_path}: {error}") from error

    if not isinstance(config_data, dict):
        raise ConfigError(f"{config_path} must contain a TOML table.")
    return config_data
