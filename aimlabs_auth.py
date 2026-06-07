"""Non-interactive Aimlabs credential resolution for sync."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any, Optional, TextIO, Union
import urllib.error
import urllib.request

from aimlabs_client import BASE_HEADERS
from config import AppConfig

SESSION_URL = "https://aimlabs.com/api/auth/session"
DEFAULT_SESSION_COOKIE = "__Secure-next-auth.session-token"
ENV_SESSION_KEY = "AIMLAB_SESSION"


class AimlabsAuthError(RuntimeError):
    """Raised when sync cannot resolve or exchange an Aimlabs credential."""


class ReloginRequiredError(AimlabsAuthError):
    """Raised when Aimlabs accepts the session but cannot mint a bearer."""


@dataclass(frozen=True)
class ResolvedSession:
    session_cookie: str
    source: str


SessionFetcher = Callable[[str, float], Mapping[str, Any]]


def resolve_session_cookie(
    *,
    session_file: Optional[Union[str, Path]] = None,
    env: Optional[Mapping[str, str]] = None,
    dotenv_path: Optional[Union[str, Path]] = ".env",
    app_config: Optional[AppConfig] = None,
    warning_stream: TextIO = sys.stderr,
) -> ResolvedSession:
    """Resolve the session cookie without opening a login window."""
    if session_file is not None:
        session_path = Path(session_file)
        session_cookie = read_session_cookie_file(session_path, warning_stream=warning_stream)
        return ResolvedSession(session_cookie=session_cookie, source=str(session_path))

    environment = os.environ if env is None else env
    env_cookie = _non_empty(environment.get(ENV_SESSION_KEY))
    if env_cookie is not None:
        return ResolvedSession(session_cookie=env_cookie, source=f"${ENV_SESSION_KEY}")

    if dotenv_path is not None:
        dotenv_values = load_dotenv_values(dotenv_path)
        dotenv_cookie = _non_empty(dotenv_values.get(ENV_SESSION_KEY))
        if dotenv_cookie is not None:
            return ResolvedSession(session_cookie=dotenv_cookie, source=str(Path(dotenv_path)))

    if app_config is not None:
        config_cookie = _non_empty(app_config.aimlabs_session_cookie)
        if config_cookie is not None:
            return ResolvedSession(session_cookie=config_cookie, source="config.toml [aimlabs].session_cookie")

    raise AimlabsAuthError("no Aimlabs session cookie found; run `voltmeter login`.")


def resolve_bearer(  # pylint: disable=too-many-arguments
    *,
    session_file: Optional[Union[str, Path]] = None,
    env: Optional[Mapping[str, str]] = None,
    dotenv_path: Optional[Union[str, Path]] = ".env",
    app_config: Optional[AppConfig] = None,
    timeout: float = 20.0,
    session_fetcher: Optional[SessionFetcher] = None,
    warning_stream: TextIO = sys.stderr,
) -> str:
    """Resolve a session cookie and exchange it for a fresh bearer token."""
    resolved_session = resolve_session_cookie(
        session_file=session_file,
        env=env,
        dotenv_path=dotenv_path,
        app_config=app_config,
        warning_stream=warning_stream,
    )
    return get_bearer_from_session(
        resolved_session.session_cookie,
        timeout=timeout,
        session_fetcher=session_fetcher,
    )


def read_session_cookie_file(
    session_file: Union[str, Path],
    *,
    warning_stream: TextIO = sys.stderr,
) -> str:
    """Read the first non-empty line from a bare-cookie session file."""
    session_path = Path(session_file)
    _warn_if_loose_posix_permissions(session_path, warning_stream)
    try:
        raw_lines = session_path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise AimlabsAuthError(f"could not read session file {session_path}: {error}") from error

    for raw_line in raw_lines:
        session_cookie = raw_line.strip()
        if session_cookie:
            return session_cookie
    raise AimlabsAuthError(f"session file {session_path} does not contain a session cookie.")


def load_dotenv_values(dotenv_path: Union[str, Path]) -> dict[str, str]:
    """Load simple KEY=VALUE pairs from .env without mutating os.environ."""
    resolved_path = Path(dotenv_path)
    try:
        raw_lines = resolved_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise AimlabsAuthError(f"could not read {resolved_path}: {error}") from error

    values = {}
    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw_value = line.partition("=")
        key = key.strip()
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def get_bearer_from_session(
    session_cookie: str,
    *,
    timeout: float = 20.0,
    session_fetcher: Optional[SessionFetcher] = None,
) -> str:
    """Exchange an aimlabs.com NextAuth session cookie for a bearer token."""
    fetcher = fetch_session_json if session_fetcher is None else session_fetcher
    session_json = fetcher(session_cookie, timeout)
    token = session_json.get("accessToken")
    if isinstance(token, str) and token:
        return token

    access_token_error = session_json.get("accessTokenError")
    if access_token_error:
        relogin_message = (
            "aimlabs.com accepted the session but could not refresh an access token; " + "run `voltmeter login`."
        )
        raise ReloginRequiredError(relogin_message)
    raise AimlabsAuthError("aimlabs.com returned no access token for this session; run `voltmeter login`.")


def fetch_session_json(session_cookie: str, timeout: float = 20.0) -> Mapping[str, Any]:
    request = urllib.request.Request(
        SESSION_URL,
        headers={
            "Cookie": _session_cookie_header(session_cookie),
            "Accept": "application/json",
            "User-Agent": BASE_HEADERS["User-Agent"],
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body_text = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        body_text = error.read().decode("utf-8", "replace")
        raise AimlabsAuthError(f"session route HTTP {error.code}: {body_text[:200]}") from error
    except OSError as error:
        raise AimlabsAuthError(f"session route request failed: {error}") from error

    try:
        payload = json.loads(body_text)
    except json.JSONDecodeError as error:
        raise AimlabsAuthError(f"session route returned non-JSON: {body_text[:200]}") from error
    if not isinstance(payload, Mapping):
        raise AimlabsAuthError("session route returned a non-object JSON payload.")
    return payload


def _session_cookie_header(session_cookie: str) -> str:
    looks_like_full_cookie = ("session-token" in session_cookie) or ("; " in session_cookie)
    if looks_like_full_cookie:
        return session_cookie
    return f"{DEFAULT_SESSION_COOKIE}={session_cookie}"


def _warn_if_loose_posix_permissions(session_path: Path, warning_stream: TextIO) -> None:
    if os.name == "nt":
        return
    try:
        mode = session_path.stat().st_mode
    except OSError:
        return
    if stat.S_IMODE(mode) & 0o077:
        print(
            f"auth: warning: {session_path} is group/world-readable; consider chmod 600.",
            file=warning_stream,
        )


def _non_empty(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped_value = value.strip()
    return stripped_value or None
