"""Aimlabs credential resolution for sync and the interactive `login` capture."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from http.cookies import Morsel, SimpleCookie
import json
import os
from pathlib import Path
import stat
import sys
import threading
import time
from typing import Any, Literal, Optional, TextIO, Union
import urllib.error
import urllib.request

from aimlabs_client import BASE_HEADERS

SESSION_URL = "https://aimlabs.com/api/auth/session"
DEFAULT_SESSION_COOKIE = "__Secure-next-auth.session-token"
ENV_SESSION_KEY = "AIMLAB_SESSION"
LOGIN_START_URL = "https://aimlabs.com/account/info"  # protected -> forces login
LOGIN_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_LOGIN_TIMEOUT_SECONDS = 300.0
SESSION_STATE_VERSION = 1
DEFAULT_SESSION_STATE_PATH = Path("data/session.json")
DEFAULT_SESSION_LOCK_PATH = Path("data/session.lock")
SESSION_LOCK_STALE_SECONDS = 10 * 60
SESSION_LOCK_WAIT_TIMEOUT_SECONDS = 60.0
SESSION_LOCK_POLL_SECONDS = 0.1
WINDOWS_FILETIME_EPOCH_SECONDS = 11_644_473_600
CorruptStatePolicy = Literal["raise", "warn"]


class AimlabsAuthError(RuntimeError):
    """Raised when sync cannot resolve or exchange an Aimlabs credential."""


class ReloginRequiredError(AimlabsAuthError):
    """Raised when Aimlabs accepts the session but cannot mint a bearer."""


class SessionStateCorruptError(AimlabsAuthError):
    """Raised when data/session.json exists but cannot safely be trusted."""


@dataclass(frozen=True)
class ResolvedSession:
    session_cookie: str
    source: str
    rotation_managed: bool = True


@dataclass(frozen=True)
class ResolvedBearer:
    bearer: str
    session_source: str


@dataclass(frozen=True)
class SessionFetchResult:
    session_json: Mapping[str, Any]
    rotated_session_cookie: Optional[str] = None


RawSessionFetchResult = Union[SessionFetchResult, Mapping[str, Any]]
SessionFetcher = Callable[[str, float], RawSessionFetchResult]


def resolve_session_cookie(  # pylint: disable=too-many-arguments
    *,
    session_file: Optional[Union[str, Path]] = None,
    state_path: Optional[Union[str, Path]] = DEFAULT_SESSION_STATE_PATH,
    env: Optional[Mapping[str, str]] = None,
    dotenv_path: Optional[Union[str, Path]] = ".env",
    corrupt_state_policy: CorruptStatePolicy = "raise",
    warning_stream: TextIO = sys.stderr,
) -> ResolvedSession:
    """Resolve the session cookie without opening a login window.

    Channels, in precedence order: ``--session-file`` (a path) > ``data/session.json`` >
    ``$AIMLAB_SESSION`` > a repo-root ``.env``. The secret never appears as a literal CLI
    argument (design §11/§12).
    """
    if session_file is not None:
        session_path = Path(session_file)
        session_cookie = read_session_cookie_file(session_path, warning_stream=warning_stream)
        return ResolvedSession(session_cookie=session_cookie, source=str(session_path), rotation_managed=False)

    if state_path is not None:
        try:
            state_cookie = read_session_state_cookie(state_path)
        except SessionStateCorruptError as error:
            if corrupt_state_policy == "raise":
                raise
            print(f"auth: warning: {error}; falling back to environment credentials.", file=warning_stream)
        else:
            if state_cookie is not None:
                return ResolvedSession(session_cookie=state_cookie, source=str(Path(state_path)))

    environment = os.environ if env is None else env
    env_cookie = _non_empty(environment.get(ENV_SESSION_KEY))
    if env_cookie is not None:
        return ResolvedSession(session_cookie=env_cookie, source=f"${ENV_SESSION_KEY}")

    if dotenv_path is not None:
        dotenv_values = load_dotenv_values(dotenv_path)
        dotenv_cookie = _non_empty(dotenv_values.get(ENV_SESSION_KEY))
        if dotenv_cookie is not None:
            return ResolvedSession(session_cookie=dotenv_cookie, source=str(Path(dotenv_path)))

    raise AimlabsAuthError("no Aimlabs session cookie found; run `voltmeter login`.")


def resolve_bearer(  # pylint: disable=too-many-arguments
    *,
    session_file: Optional[Union[str, Path]] = None,
    state_path: Union[str, Path] = DEFAULT_SESSION_STATE_PATH,
    lock_path: Union[str, Path] = DEFAULT_SESSION_LOCK_PATH,
    env: Optional[Mapping[str, str]] = None,
    dotenv_path: Optional[Union[str, Path]] = ".env",
    timeout: float = 20.0,
    session_fetcher: Optional[SessionFetcher] = None,
    warning_stream: TextIO = sys.stderr,
) -> str:
    """Resolve a session cookie and exchange it for a fresh bearer token."""
    resolved_bearer = resolve_bearer_with_source(
        session_file=session_file,
        state_path=state_path,
        lock_path=lock_path,
        env=env,
        dotenv_path=dotenv_path,
        timeout=timeout,
        session_fetcher=session_fetcher,
        warning_stream=warning_stream,
    )
    return resolved_bearer.bearer


def resolve_bearer_with_source(  # pylint: disable=too-many-arguments
    *,
    session_file: Optional[Union[str, Path]] = None,
    state_path: Union[str, Path] = DEFAULT_SESSION_STATE_PATH,
    lock_path: Union[str, Path] = DEFAULT_SESSION_LOCK_PATH,
    env: Optional[Mapping[str, str]] = None,
    dotenv_path: Optional[Union[str, Path]] = ".env",
    timeout: float = 20.0,
    session_fetcher: Optional[SessionFetcher] = None,
    warning_stream: TextIO = sys.stderr,
) -> ResolvedBearer:
    """Resolve and mint a bearer, persisting rotated session cookies when managed."""
    if session_file is not None:
        print(
            "auth: warning: using --session-file; this run will not persist session rotation. "
            "The file must come from an independent login or it can revoke your managed credential.",
            file=warning_stream,
        )
        resolved_session = resolve_session_cookie(
            session_file=session_file,
            state_path=state_path,
            env=env,
            dotenv_path=dotenv_path,
            warning_stream=warning_stream,
        )
        return ResolvedBearer(
            bearer=get_bearer_from_session(
                resolved_session.session_cookie,
                timeout=timeout,
                session_fetcher=session_fetcher,
            ),
            session_source=resolved_session.source,
        )

    with session_state_lock(lock_path):
        return _resolve_bearer_with_source_locked(
            state_path=state_path,
            env=env,
            dotenv_path=dotenv_path,
            timeout=timeout,
            session_fetcher=session_fetcher,
            warning_stream=warning_stream,
        )


def _resolve_bearer_with_source_locked(  # pylint: disable=too-many-arguments
    *,
    state_path: Union[str, Path],
    env: Optional[Mapping[str, str]],
    dotenv_path: Optional[Union[str, Path]],
    timeout: float,
    session_fetcher: Optional[SessionFetcher],
    warning_stream: TextIO,
) -> ResolvedBearer:
    resolved_session = resolve_session_cookie(
        state_path=state_path,
        env=env,
        dotenv_path=dotenv_path,
        warning_stream=warning_stream,
    )
    session_result = _fetch_session(resolved_session.session_cookie, timeout, session_fetcher)
    bearer = bearer_from_session_result(session_result)
    if session_result.rotated_session_cookie:
        write_session_state(
            session_result.rotated_session_cookie,
            session_result.session_json,
            state_path,
        )
    return ResolvedBearer(bearer=bearer, session_source=resolved_session.source)


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


def read_session_state_cookie(state_path: Union[str, Path]) -> Optional[str]:
    """Read the managed rotated session cookie, distinguishing missing from corrupt."""
    resolved_path = Path(state_path)
    try:
        raw_text = resolved_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise SessionStateCorruptError(
            f"session state at {resolved_path} is corrupt; run `voltmeter login` "
            "or remove the file only if .env is current"
        ) from error

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as error:
        raise SessionStateCorruptError(
            f"session state at {resolved_path} is corrupt; run `voltmeter login` "
            "or remove the file only if .env is current"
        ) from error

    if not isinstance(payload, Mapping):
        raise SessionStateCorruptError(
            f"session state at {resolved_path} is corrupt; run `voltmeter login` "
            "or remove the file only if .env is current"
        )
    if payload.get("version") != SESSION_STATE_VERSION:
        raise SessionStateCorruptError(
            f"session state at {resolved_path} has an unsupported version; run `voltmeter login` "
            "or remove the file only if .env is current"
        )
    session_cookie = payload.get("session_cookie")
    if not isinstance(session_cookie, str) or not session_cookie.strip():
        raise SessionStateCorruptError(
            f"session state at {resolved_path} is missing a session cookie; run `voltmeter login` "
            "or remove the file only if .env is current"
        )
    return session_cookie.strip()


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
    session_result = _fetch_session(session_cookie, timeout, session_fetcher)
    return bearer_from_session_result(session_result)


def bearer_from_session_result(session_result: SessionFetchResult) -> str:
    session_json = session_result.session_json
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


def _fetch_session(
    session_cookie: str,
    timeout: float,
    session_fetcher: Optional[SessionFetcher],
) -> SessionFetchResult:
    fetcher = fetch_session_json if session_fetcher is None else session_fetcher
    session_result = fetcher(session_cookie, timeout)
    if isinstance(session_result, SessionFetchResult):
        return session_result
    return SessionFetchResult(session_json=session_result)


def fetch_session_json(session_cookie: str, timeout: float = 20.0) -> SessionFetchResult:
    request = urllib.request.Request(
        SESSION_URL,
        headers={
            "Cookie": session_cookie_header(session_cookie),
            "Accept": "application/json",
            "User-Agent": BASE_HEADERS["User-Agent"],
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body_text = response.read().decode("utf-8", "replace")
            rotated_session_cookie = extract_session_cookie_from_set_cookie_headers(
                response.headers.get_all("Set-Cookie", [])
            )
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
    return SessionFetchResult(session_json=payload, rotated_session_cookie=rotated_session_cookie)


def session_cookie_header(session_cookie: str) -> str:
    """Build a Cookie header value from a bare token or pass a full cookie string through."""
    looks_like_full_cookie = ("session-token" in session_cookie) or ("; " in session_cookie)
    if looks_like_full_cookie:
        return session_cookie
    return f"{DEFAULT_SESSION_COOKIE}={session_cookie}"


def extract_session_cookie_from_set_cookie_headers(set_cookie_headers: list[str]) -> Optional[str]:
    """Extract the rotated NextAuth session cookie from response Set-Cookie headers."""
    parsed_cookies = []
    for set_cookie_header in set_cookie_headers:
        cookie = SimpleCookie()
        cookie.load(set_cookie_header)
        parsed_cookies.append(cookie)
    return extract_session_cookie(parsed_cookies)


def write_session_state(
    session_cookie: str,
    session_json: Mapping[str, Any],
    state_path: Union[str, Path] = DEFAULT_SESSION_STATE_PATH,
) -> None:
    """Atomically persist the managed rotated session cookie."""
    resolved_path = Path(state_path)
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    expires_value = session_json.get("expires")
    payload = {
        "version": SESSION_STATE_VERSION,
        "session_cookie": session_cookie,
        "expires": expires_value if isinstance(expires_value, str) else None,
        "updated_at": _utc_now_text(),
    }
    temp_path = resolved_path.with_name(f".{resolved_path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    try:
        file_descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as state_file:
            json.dump(payload, state_file, indent=2)
            state_file.write("\n")
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        os.replace(temp_path, resolved_path)
    except OSError as error:
        raise AimlabsAuthError(f"could not persist rotated session state to {resolved_path}: {error}") from error
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


@contextmanager
def session_state_lock(
    lock_path: Union[str, Path] = DEFAULT_SESSION_LOCK_PATH,
    *,
    stale_after_seconds: float = SESSION_LOCK_STALE_SECONDS,
    wait_timeout_seconds: float = SESSION_LOCK_WAIT_TIMEOUT_SECONDS,
    sleep_func: Callable[[float], None] = time.sleep,
) -> Iterator[None]:
    """Hold the existence-based session-state lock."""
    resolved_path = Path(lock_path)
    _acquire_session_lock(
        resolved_path,
        stale_after_seconds=stale_after_seconds,
        wait_timeout_seconds=wait_timeout_seconds,
        sleep_func=sleep_func,
    )
    try:
        yield
    finally:
        try:
            resolved_path.unlink()
        except FileNotFoundError:
            pass


def _acquire_session_lock(
    lock_path: Path,
    *,
    stale_after_seconds: float,
    wait_timeout_seconds: float,
    sleep_func: Callable[[float], None],
) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait_timeout_seconds
    while True:
        try:
            _create_session_lock_file(lock_path)
            return
        except FileExistsError as error:
            if _steal_stale_session_lock(lock_path, stale_after_seconds=stale_after_seconds):
                continue
            if time.monotonic() >= deadline:
                raise AimlabsAuthError(f"timed out waiting for session lock at {lock_path}") from error
            sleep_func(SESSION_LOCK_POLL_SECONDS)


def _create_session_lock_file(lock_path: Path) -> None:
    file_descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(file_descriptor, "w", encoding="utf-8") as lock_file:
        json.dump(
            {
                "pid": os.getpid(),
                "start_time": _process_start_time(os.getpid()),
                "created_at": time.time(),
            },
            lock_file,
        )
        lock_file.write("\n")
    try:
        os.chmod(lock_path, 0o600)
    except OSError:
        pass


def _steal_stale_session_lock(lock_path: Path, *, stale_after_seconds: float) -> bool:
    if not _session_lock_is_stale(lock_path, stale_after_seconds=stale_after_seconds):
        return False
    stolen_path = lock_path.with_name(f"{lock_path.name}.stale.{os.getpid()}.{time.monotonic_ns()}")
    try:
        os.replace(lock_path, stolen_path)
    except FileNotFoundError:
        return True
    except OSError:
        return False

    try:
        if _session_lock_is_stale(stolen_path, stale_after_seconds=stale_after_seconds):
            stolen_path.unlink()
            return True
        if not lock_path.exists():
            os.replace(stolen_path, lock_path)
    finally:
        try:
            stolen_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
    return False


# pylint: disable-next=too-many-return-statements
def _session_lock_is_stale(lock_path: Path, *, stale_after_seconds: float) -> bool:
    try:
        lock_stat = lock_path.stat()
    except FileNotFoundError:
        return True
    except OSError:
        return False

    if time.time() - lock_stat.st_mtime > stale_after_seconds:
        return True

    payload = _read_lock_payload(lock_path)
    pid_value = payload.get("pid")
    start_time_value = payload.get("start_time")
    if not isinstance(pid_value, int) or pid_value <= 0:
        return False
    if not _process_exists(pid_value):
        return True
    if isinstance(start_time_value, (int, float)):
        live_start_time = _process_start_time(pid_value)
        if live_start_time is not None and abs(live_start_time - float(start_time_value)) > 1.0:
            return True
    return False


def _read_lock_payload(lock_path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return {}
    if isinstance(payload, Mapping):
        return payload
    return {}


def _process_exists(pid_value: int) -> bool:
    if pid_value == os.getpid():
        return True
    if os.name == "nt":
        return _open_windows_process(pid_value) is not None
    try:
        os.kill(pid_value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_start_time(pid_value: int) -> Optional[float]:
    if os.name == "nt":
        return _windows_process_start_time(pid_value)
    return _posix_process_start_time(pid_value)


def _posix_process_start_time(pid_value: int) -> Optional[float]:
    stat_path = Path("/proc") / str(pid_value) / "stat"
    proc_stat_path = Path("/proc/stat")
    try:
        stat_text = stat_path.read_text(encoding="utf-8")
        proc_stat_text = proc_stat_path.read_text(encoding="utf-8")
        after_command = stat_text.rsplit(") ", 1)[1]
        stat_fields = after_command.split()
        start_ticks = int(stat_fields[19])
        sysconf_func = getattr(os, "sysconf", None)
        if not callable(sysconf_func):
            return None
        clock_ticks = int(sysconf_func("SC_CLK_TCK"))  # pylint: disable=not-callable
        boot_time = next(int(line.split()[1]) for line in proc_stat_text.splitlines() if line.startswith("btime "))
    except IndexError, StopIteration, ValueError, OSError:
        return None
    return boot_time + (start_ticks / clock_ticks)


def _windows_process_start_time(pid_value: int) -> Optional[float]:
    handle = _open_windows_process(pid_value)
    if handle is None:
        return None
    try:
        import ctypes  # pylint: disable=import-outside-toplevel
        from ctypes import wintypes  # pylint: disable=import-outside-toplevel

        creation_time = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        success = ctypes.windll.kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation_time),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        )
        if not success:
            return None
        filetime_ticks = (creation_time.dwHighDateTime << 32) + creation_time.dwLowDateTime
        return (filetime_ticks / 10_000_000) - WINDOWS_FILETIME_EPOCH_SECONDS
    finally:
        _close_windows_handle(handle)


def _open_windows_process(pid_value: int) -> Optional[int]:
    if os.name != "nt":
        return None
    import ctypes  # pylint: disable=import-outside-toplevel

    process_query_limited_information = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid_value)
    return int(handle) if handle else None


def _close_windows_handle(handle: int) -> None:
    if os.name != "nt":
        return
    import ctypes  # pylint: disable=import-outside-toplevel

    ctypes.windll.kernel32.CloseHandle(handle)


# ---------------------------------------------------------------------------
# Login capture: drive the real aimlabs.com login UI in an embedded browser
# (MFA/captcha are handled natively by Aim Lab), then read the session cookie
# from the webview's native cookie store (which includes httpOnly cookies,
# unlike document.cookie) and write it to .env. The credential never leaves
# the machine. `login` is the ONLY command that may open a window (decision
# 23); pywebview is optional and only login_and_capture may import it.
# ---------------------------------------------------------------------------


def _iter_cookie_pairs(cookies: Any) -> Iterator[tuple[str, Optional[str]]]:
    """Yield (name, value) from whatever shape a webview backend returns.

    pywebview's get_cookies() return type varies by backend/version:
      - EdgeChromium/WebView2 (Windows) and others -> list[http.cookies.SimpleCookie]
        (each a dict of name -> Morsel)
      - some backends/versions -> list[http.cookiejar.Cookie] (.name / .value)
      - occasionally a bare Morsel, or a plain {name: value} dict (not in a list)
    """
    if isinstance(cookies, (Morsel, dict)):  # a bare cookie container, not a list of them
        cookies = [cookies]
    for cookie in cookies or []:
        if isinstance(cookie, Morsel):
            yield cookie.key, cookie.value
        elif isinstance(cookie, dict):  # SimpleCookie (name -> Morsel) or plain dict
            for cookie_name, cookie_value in cookie.items():
                if isinstance(cookie_value, Morsel):
                    yield cookie_value.key, cookie_value.value
                else:
                    yield cookie_name, cookie_value
        else:  # http.cookiejar.Cookie or similar object with .name/.value
            cookie_name = getattr(cookie, "name", None)
            cookie_value = getattr(cookie, "value", None)
            if cookie_name is not None:
                yield cookie_name, cookie_value


def _cookie_names(cookies: Any) -> list[str]:
    """Sorted, de-duplicated cookie names (no values) — safe for diagnostics."""
    return sorted({cookie_name for cookie_name, _ in _iter_cookie_pairs(cookies) if cookie_name})


def extract_session_cookie(cookies: Any) -> Optional[str]:
    """From webview cookies, build the AIMLAB_SESSION value to store.

    Handles:
      - single default-named cookie  -> bare value (resolution re-wraps it)
      - single custom-named cookie   -> "name=value" (sent verbatim)
      - chunked cookies (.0/.1/...)  -> joined "n0=v0; n1=v1" string
    Returns None if no session-token cookie is present.
    """
    chunked_values: dict[str, str] = {}
    single_cookie: Optional[tuple[str, str]] = None
    for cookie_name, cookie_value in _iter_cookie_pairs(cookies):
        if not cookie_name or cookie_value is None or "session-token" not in cookie_name:
            continue
        suffix = cookie_name.split("session-token", 1)[1]  # "" or ".0", ".1", ...
        if suffix.startswith(".") and suffix[1:].isdigit():
            chunked_values[cookie_name] = cookie_value
        else:
            single_cookie = (cookie_name, cookie_value)
    if chunked_values:
        ordered_chunks = sorted(chunked_values.items(), key=lambda chunk: int(chunk[0].rsplit(".", 1)[-1]))
        return "; ".join(f"{chunk_name}={chunk_value}" for chunk_name, chunk_value in ordered_chunks)
    if single_cookie is not None:
        cookie_name, cookie_value = single_cookie
        return cookie_value if cookie_name == DEFAULT_SESSION_COOKIE else f"{cookie_name}={cookie_value}"
    return None


def write_env_var(env_path: Union[str, Path], key: str, value: str) -> None:
    """Set KEY="value" in a .env file: replace an existing line or append.

    Other lines are preserved. On POSIX the file is chmod'd 0600 since it now
    holds a live credential.
    """
    resolved_path = Path(env_path)
    env_line = f'{key}="{value}"\n'
    try:
        lines = resolved_path.read_text(encoding="utf-8").splitlines(keepends=True)
    except FileNotFoundError:
        lines = []
    replaced = False
    for idx, existing_line in enumerate(lines):
        stripped_line = existing_line.lstrip()
        if stripped_line.startswith(f"{key}=") and not stripped_line.startswith("#"):
            lines[idx] = env_line
            replaced = True
            break
    if not replaced:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(env_line)
    resolved_path.write_text("".join(lines), encoding="utf-8")
    try:
        os.chmod(resolved_path, 0o600)  # best-effort; no-op semantics on Windows
    except OSError:
        pass


def login_and_capture(  # pylint: disable=too-many-arguments
    env_path: Union[str, Path] = ".env",
    *,
    timeout: float = DEFAULT_LOGIN_TIMEOUT_SECONDS,
    start_url: str = LOGIN_START_URL,
    state_path: Union[str, Path] = DEFAULT_SESSION_STATE_PATH,
    lock_path: Union[str, Path] = DEFAULT_SESSION_LOCK_PATH,
    message_stream: TextIO = sys.stderr,
) -> Optional[str]:
    """Open the Aim Lab login window; on success, write AIMLAB_SESSION to .env.

    Returns the captured session value, or None (pywebview missing, timeout, or
    window closed before login).
    """
    try:
        import webview  # type: ignore  # pylint: disable=import-outside-toplevel
    except ImportError:
        print(
            "login needs the optional dependency 'pywebview'.\n"
            '  install it with:  pip install "voltmeter-aimlabs[login]"  (or: pip install pywebview)\n'
            "  (on Linux you may also need a webview backend, e.g. PyGObject + WebKit2GTK,\n"
            "   or PyQt/QtWebEngine)\n"
            "  Until then, capture the cookie manually: log into aimlabs.com, DevTools ->\n"
            "  Application -> Cookies -> aimlabs.com -> copy __Secure-next-auth.session-token\n"
            "  into .env as AIMLAB_SESSION.",
            file=message_stream,
        )
        return None

    captured: dict[str, Optional[str]] = {"value": None}
    last_seen_names: dict[str, list[str]] = {"names": []}

    stop = threading.Event()

    def _poll(window: Any, stop_event: threading.Event) -> None:
        deadline = time.time() + timeout
        while not stop_event.is_set() and time.time() < deadline:
            try:
                cookies = window.get_cookies()
            except Exception as error:  # pylint: disable=broad-exception-caught
                # The backend may not be ready to serve cookies yet.
                cookies = None
                if last_seen_names["names"] != ["<error>"]:
                    print(f"login: (get_cookies not ready yet: {error})", file=message_stream)
                    last_seen_names["names"] = ["<error>"]
            session_value = extract_session_cookie(cookies) if cookies else None
            # Diagnostic: when the set of visible cookie names changes, log the names
            # (NOT values) so we can see whether/when the session token appears.
            if cookies is not None:
                cookie_names = _cookie_names(cookies)
                if cookie_names != last_seen_names["names"]:
                    last_seen_names["names"] = cookie_names
                    print(f"login: cookies visible now: {cookie_names or '(none)'}", file=message_stream)
            if session_value:
                captured["value"] = session_value
                break
            if stop_event.wait(LOGIN_POLL_INTERVAL_SECONDS):
                break
        try:
            window.destroy()
        except Exception:  # pylint: disable=broad-exception-caught
            pass

    def _start_poll(window: Any) -> None:
        poll_thread = threading.Thread(target=_poll, args=(window, stop), daemon=True)
        poll_thread.start()

    print(
        "login: opening the Aim Lab login window -- log in normally; it closes "
        "automatically once your session is captured.",
        file=message_stream,
    )
    login_window = webview.create_window("Log in to Aim Lab", start_url)
    try:
        webview.start(_start_poll, (login_window,))
    finally:
        stop.set()

    session_value = captured["value"]
    if not session_value:
        _print_no_capture_message(last_seen_names["names"], message_stream)
        return None

    with session_state_lock(lock_path):
        reset_login_session_locked(
            session_value,
            env_path,
            state_path=state_path,
            message_stream=message_stream,
        )
    return session_value


def reset_login_session_locked(
    session_value: str,
    env_path: Union[str, Path] = ".env",
    *,
    state_path: Union[str, Path] = DEFAULT_SESSION_STATE_PATH,
    message_stream: TextIO = sys.stderr,
    session_fetcher: Optional[SessionFetcher] = None,
) -> None:
    """Reset managed rotation state and write the freshly captured login cookie.

    The caller must hold ``session_state_lock``.
    """
    resolved_state_path = Path(state_path)
    try:
        resolved_state_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as error:
        raise AimlabsAuthError(f"could not reset session state at {resolved_state_path}: {error}") from error

    write_env_var(env_path, ENV_SESSION_KEY, session_value)
    print(f"login: captured session cookie -> wrote {ENV_SESSION_KEY} to {Path(env_path)}", file=message_stream)
    _verify_and_report_identity(
        session_value,
        message_stream,
        state_path=state_path,
        session_fetcher=session_fetcher,
    )


def _print_no_capture_message(seen_names: list[str], message_stream: TextIO) -> None:
    hint = ""
    if seen_names and seen_names != ["<error>"] and not any("session-token" in name for name in seen_names):
        hint = (
            " The login cookies were visible but none contained 'session-token' -- this"
            " backend may be hiding the httpOnly session cookie (cookie capture is"
            " verified on Windows WebView2 only). Last seen: " + ", ".join(seen_names) + "."
        )
    print(
        "login: no session captured (window closed or timed out before login completed)." + hint + " Nothing written.",
        file=message_stream,
    )


def _verify_and_report_identity(
    session_value: str,
    message_stream: TextIO,
    *,
    state_path: Union[str, Path] = DEFAULT_SESSION_STATE_PATH,
    session_fetcher: Optional[SessionFetcher] = None,
) -> None:
    """Validate the captured cookie + show who we're logged in as (best-effort; needs network)."""
    try:
        session_result = _fetch_session(session_value, 20.0, session_fetcher)
        bearer_from_session_result(session_result)
    except AimlabsAuthError as error:
        print(
            f"login: captured the cookie but could not verify it yet ({error}). Try `voltmeter sync`.",
            file=message_stream,
        )
        return
    if session_result.rotated_session_cookie:
        write_session_state(session_result.rotated_session_cookie, session_result.session_json, state_path)
    session_json = session_result.session_json
    user_value = session_json.get("user")
    user = user_value if isinstance(user_value, Mapping) else {}
    email = user.get("email")
    expires = session_json.get("expires")
    who = f" as {email}" if email else ""
    until = f" (session valid until {expires})" if expires else ""
    print(f"login: verified login{who}{until}.", file=message_stream)


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


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
