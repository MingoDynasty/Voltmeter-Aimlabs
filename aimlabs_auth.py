"""Aimlabs credential resolution for sync and the interactive `login` capture."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from http.cookies import Morsel
import json
import os
from pathlib import Path
import stat
import sys
import time
from typing import Any, Optional, TextIO, Union
import urllib.error
import urllib.request

from aimlabs_client import BASE_HEADERS

SESSION_URL = "https://aimlabs.com/api/auth/session"
DEFAULT_SESSION_COOKIE = "__Secure-next-auth.session-token"
ENV_SESSION_KEY = "AIMLAB_SESSION"
LOGIN_START_URL = "https://aimlabs.com/account/info"  # protected -> forces login
LOGIN_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_LOGIN_TIMEOUT_SECONDS = 300.0


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
    warning_stream: TextIO = sys.stderr,
) -> ResolvedSession:
    """Resolve the session cookie without opening a login window.

    Channels, in precedence order: ``--session-file`` (a path) > ``$AIMLAB_SESSION`` > a
    repo-root ``.env``. The secret never appears as a literal CLI argument (design §11/§12).
    """
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

    raise AimlabsAuthError("no Aimlabs session cookie found; run `voltmeter login`.")


def resolve_bearer(  # pylint: disable=too-many-arguments
    *,
    session_file: Optional[Union[str, Path]] = None,
    env: Optional[Mapping[str, str]] = None,
    dotenv_path: Optional[Union[str, Path]] = ".env",
    timeout: float = 20.0,
    session_fetcher: Optional[SessionFetcher] = None,
    warning_stream: TextIO = sys.stderr,
) -> str:
    """Resolve a session cookie and exchange it for a fresh bearer token."""
    resolved_session = resolve_session_cookie(
        session_file=session_file,
        env=env,
        dotenv_path=dotenv_path,
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
            "Cookie": session_cookie_header(session_cookie),
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


def session_cookie_header(session_cookie: str) -> str:
    """Build a Cookie header value from a bare token or pass a full cookie string through."""
    looks_like_full_cookie = ("session-token" in session_cookie) or ("; " in session_cookie)
    if looks_like_full_cookie:
        return session_cookie
    return f"{DEFAULT_SESSION_COOKIE}={session_cookie}"


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


def login_and_capture(
    env_path: Union[str, Path] = ".env",
    *,
    timeout: float = DEFAULT_LOGIN_TIMEOUT_SECONDS,
    start_url: str = LOGIN_START_URL,
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

    def _poll(window: Any) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
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
            time.sleep(LOGIN_POLL_INTERVAL_SECONDS)
        try:
            window.destroy()
        except Exception:  # pylint: disable=broad-exception-caught
            pass

    print(
        "login: opening the Aim Lab login window -- log in normally; it closes "
        "automatically once your session is captured.",
        file=message_stream,
    )
    login_window = webview.create_window("Log in to Aim Lab", start_url)
    webview.start(_poll, (login_window,))

    session_value = captured["value"]
    if not session_value:
        _print_no_capture_message(last_seen_names["names"], message_stream)
        return None

    write_env_var(env_path, ENV_SESSION_KEY, session_value)
    print(f"login: captured session cookie -> wrote {ENV_SESSION_KEY} to {Path(env_path)}", file=message_stream)
    _verify_and_report_identity(session_value, message_stream)
    return session_value


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


def _verify_and_report_identity(session_value: str, message_stream: TextIO) -> None:
    """Validate the captured cookie + show who we're logged in as (best-effort; needs network)."""
    try:
        session_json = fetch_session_json(session_value)
    except AimlabsAuthError as error:
        print(
            f"login: captured the cookie but could not verify it yet ({error}). Try `voltmeter sync`.",
            file=message_stream,
        )
        return
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
