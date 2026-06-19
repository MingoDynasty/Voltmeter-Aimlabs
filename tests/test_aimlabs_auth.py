from http.cookies import SimpleCookie
import os
import io
import json
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

import aimlabs_auth
from aimlabs_auth import (
    DEFAULT_SESSION_COOKIE,
    AimlabsAuthError,
    ReloginRequiredError,
    SessionFetchResult,
    SessionStateCorruptError,
    read_session_state_cookie,
    resolve_bearer,
    extract_session_cookie,
    fetch_session_json,
    get_bearer_from_session,
    load_dotenv_values,
    login_and_capture,
    read_session_cookie_file,
    resolve_session_cookie,
    write_env_var,
)


def write_lock_file(lock_path: Path, *, pid_value: int, start_time: float) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps({"pid": pid_value, "start_time": start_time, "created_at": time.time()}),
        encoding="utf-8",
    )


class AimlabsAuthTests(unittest.TestCase):
    def test_session_file_warns_on_loose_posix_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.cookie"
            session_path.write_text("file-cookie\n", encoding="utf-8")
            warning_stream = io.StringIO()

            with (
                patch("aimlabs_auth.os.name", "posix"),
                patch.object(
                    Path,
                    "stat",
                    return_value=SimpleNamespace(st_mode=0o100644),
                ),
            ):
                session_cookie = read_session_cookie_file(session_path, warning_stream=warning_stream)

        self.assertEqual(session_cookie, "file-cookie")
        self.assertIn("group/world-readable", warning_stream.getvalue())
        self.assertIn("session.cookie", warning_stream.getvalue())

    def test_session_file_takes_precedence_and_reads_first_non_empty_line(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "session.cookie"
            session_path.write_text("\n  file-cookie  \nsecond-line\n", encoding="utf-8")
            dotenv_path = Path(temp_dir) / ".env"
            dotenv_path.write_text('AIMLAB_SESSION="dotenv-cookie"\n', encoding="utf-8")

            resolved = resolve_session_cookie(
                session_file=session_path,
                env={"AIMLAB_SESSION": "env-cookie"},
                dotenv_path=dotenv_path,
            )

        self.assertEqual(resolved.session_cookie, "file-cookie")
        self.assertEqual(resolved.source, str(session_path))

    def test_env_takes_precedence_over_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            dotenv_path = Path(temp_dir) / ".env"
            dotenv_path.write_text('AIMLAB_SESSION="dotenv-cookie"\n', encoding="utf-8")

            resolved = resolve_session_cookie(
                state_path=None,
                env={"AIMLAB_SESSION": " env-cookie "},
                dotenv_path=dotenv_path,
            )

        self.assertEqual(resolved.session_cookie, "env-cookie")
        self.assertEqual(resolved.source, "$AIMLAB_SESSION")

    def test_state_file_takes_precedence_over_env_and_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            state_path = temp_path / "data" / "session.json"
            state_path.parent.mkdir()
            state_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "session_cookie": "state-cookie",
                        "expires": "2026-07-01T00:00:00.000Z",
                        "updated_at": "2026-06-18T00:00:00.000Z",
                    }
                ),
                encoding="utf-8",
            )
            dotenv_path = temp_path / ".env"
            dotenv_path.write_text('AIMLAB_SESSION="dotenv-cookie"\n', encoding="utf-8")

            resolved = resolve_session_cookie(
                state_path=state_path,
                env={"AIMLAB_SESSION": "env-cookie"},
                dotenv_path=dotenv_path,
            )

        self.assertEqual(resolved.session_cookie, "state-cookie")
        self.assertEqual(resolved.source, str(state_path))

    def test_dotenv_values_parse_simple_quotes_without_mutating_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            dotenv_path = Path(temp_dir) / ".env"
            dotenv_path.write_text(
                "# comment\nAIMLAB_SESSION='dotenv-cookie'\nEMPTY=\nBROKEN\n",
                encoding="utf-8",
            )

            values = load_dotenv_values(dotenv_path)

        self.assertEqual(values["AIMLAB_SESSION"], "dotenv-cookie")
        self.assertEqual(values["EMPTY"], "")
        self.assertNotIn("BROKEN", values)

    def test_fetch_session_json_sends_full_cookie_header_verbatim(self) -> None:
        full_cookie = "__Secure-next-auth.session-token.0=abc; __Secure-next-auth.session-token.1=def"
        requests = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            @property
            def headers(self):
                return SimpleNamespace(
                    get_all=lambda header_name, default=None: (
                        [
                            f"{DEFAULT_SESSION_COOKIE}.1=bbb; Path=/; HttpOnly",
                            f"{DEFAULT_SESSION_COOKIE}.0=aaa; Path=/; HttpOnly",
                        ]
                        if header_name == "Set-Cookie"
                        else default
                    )
                )

            def read(self) -> bytes:
                return json.dumps({"accessToken": "fresh-token"}).encode("utf-8")

        def fake_urlopen(request, timeout: float):
            requests.append((request, timeout))
            return FakeResponse()

        with patch("aimlabs_auth.urllib.request.urlopen", fake_urlopen):
            result = fetch_session_json(full_cookie, timeout=12)

        self.assertEqual(result.session_json["accessToken"], "fresh-token")
        self.assertEqual(
            result.rotated_session_cookie,
            f"{DEFAULT_SESSION_COOKIE}.0=aaa; {DEFAULT_SESSION_COOKIE}.1=bbb",
        )
        self.assertEqual(requests[0][0].get_header("Cookie"), full_cookie)
        self.assertEqual(requests[0][1], 12)

    def test_missing_session_raises_login_message(self) -> None:
        with self.assertRaisesRegex(AimlabsAuthError, "voltmeter login"):
            resolve_session_cookie(state_path=None, env={}, dotenv_path=None)

    def test_bearer_exchange_uses_access_token(self) -> None:
        bearer = get_bearer_from_session(
            "session-cookie",
            session_fetcher=lambda session_cookie, timeout: {"accessToken": "fresh-token"},
        )

        self.assertEqual(bearer, "fresh-token")

    def test_refresh_error_requires_relogin(self) -> None:
        with self.assertRaisesRegex(ReloginRequiredError, "voltmeter login"):
            get_bearer_from_session(
                "session-cookie",
                session_fetcher=lambda session_cookie, timeout: {
                    "accessTokenError": "RefreshAccessTokenError",
                },
            )

    def test_managed_mint_persists_rotation_and_control_without_persist_fails(self) -> None:
        endpoint = RotatingSessionEndpoint(initial_cookie="cookie-0")

        self.assertEqual(
            get_bearer_from_session("cookie-0", session_fetcher=endpoint),
            "bearer-1",
        )
        with self.assertRaises(ReloginRequiredError):
            get_bearer_from_session("cookie-0", session_fetcher=endpoint)

        endpoint = RotatingSessionEndpoint(initial_cookie="cookie-0")
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            state_path = temp_path / "data" / "session.json"
            lock_path = temp_path / "data" / "session.lock"
            first_bearer = resolve_bearer(
                state_path=state_path,
                lock_path=lock_path,
                env={"AIMLAB_SESSION": "cookie-0"},
                dotenv_path=None,
                session_fetcher=endpoint,
            )
            second_bearer = resolve_bearer(
                state_path=state_path,
                lock_path=lock_path,
                env={"AIMLAB_SESSION": "cookie-0"},
                dotenv_path=None,
                session_fetcher=endpoint,
            )

            self.assertEqual(first_bearer, "bearer-1")
            self.assertEqual(second_bearer, "bearer-2")
            self.assertEqual(read_session_state_cookie(state_path), "cookie-2")
            self.assertEqual(endpoint.calls, ["cookie-0", "cookie-1"])

    def test_failed_refresh_does_not_overwrite_last_good_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            state_path = temp_path / "data" / "session.json"
            lock_path = temp_path / "data" / "session.lock"
            state_path.parent.mkdir()
            state_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "session_cookie": "last-good-cookie",
                        "expires": "2026-07-01T00:00:00.000Z",
                        "updated_at": "2026-06-18T00:00:00.000Z",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ReloginRequiredError):
                resolve_bearer(
                    state_path=state_path,
                    lock_path=lock_path,
                    env={"AIMLAB_SESSION": "spent-cookie"},
                    dotenv_path=None,
                    session_fetcher=lambda session_cookie, timeout: SessionFetchResult(
                        {"accessTokenError": "RefreshAccessTokenError"},
                        rotated_session_cookie="dead-cookie",
                    ),
                )

            self.assertEqual(read_session_state_cookie(state_path), "last-good-cookie")

    def test_corrupt_state_fails_closed_on_minting_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            state_path = temp_path / "data" / "session.json"
            lock_path = temp_path / "data" / "session.lock"
            state_path.parent.mkdir()
            state_path.write_text("{", encoding="utf-8")
            calls = []

            with self.assertRaisesRegex(SessionStateCorruptError, "voltmeter login"):
                resolve_bearer(
                    state_path=state_path,
                    lock_path=lock_path,
                    env={"AIMLAB_SESSION": "env-cookie"},
                    dotenv_path=None,
                    session_fetcher=lambda session_cookie, timeout: calls.append(session_cookie),
                )

            self.assertEqual(calls, [])

    def test_concurrent_managed_mints_serialize_and_re_resolve_state(self) -> None:
        endpoint = SerializedRotatingSessionEndpoint(initial_cookie="cookie-0")
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            state_path = temp_path / "data" / "session.json"
            lock_path = temp_path / "data" / "session.lock"
            results = []
            errors = []

            def worker() -> None:
                try:
                    results.append(
                        resolve_bearer(
                            state_path=state_path,
                            lock_path=lock_path,
                            env={"AIMLAB_SESSION": "cookie-0"},
                            dotenv_path=None,
                            session_fetcher=endpoint,
                        )
                    )
                except Exception as error:  # pylint: disable=broad-exception-caught
                    errors.append(error)

            first_thread = threading.Thread(target=worker)
            second_thread = threading.Thread(target=worker)
            first_thread.start()
            self.assertTrue(endpoint.first_call_entered.wait(timeout=1.0))
            second_thread.start()
            endpoint.release_first_call.set()
            first_thread.join(timeout=2.0)
            second_thread.join(timeout=2.0)

            self.assertFalse(first_thread.is_alive())
            self.assertFalse(second_thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(results, ["bearer-1", "bearer-2"])
            self.assertEqual(endpoint.calls, ["cookie-0", "cookie-1"])
            self.assertFalse(endpoint.concurrent_fetch_detected)
            self.assertEqual(read_session_state_cookie(state_path), "cookie-2")

    def test_session_lock_is_stale_when_lease_expires(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "data" / "session.lock"
            write_lock_file(lock_path, pid_value=os.getpid(), start_time=1.0)
            stale_time = time.time() - 120.0
            os.utime(lock_path, (stale_time, stale_time))

            self.assertTrue(aimlabs_auth._session_lock_is_stale(lock_path, stale_after_seconds=60.0))

    def test_session_lock_is_stale_when_recorded_process_is_dead(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "data" / "session.lock"
            write_lock_file(lock_path, pid_value=12345, start_time=10.0)

            with patch("aimlabs_auth._process_exists", return_value=False):
                self.assertTrue(aimlabs_auth._session_lock_is_stale(lock_path, stale_after_seconds=60.0))

    def test_session_lock_keeps_live_matching_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "data" / "session.lock"
            write_lock_file(lock_path, pid_value=12345, start_time=10.0)

            with (
                patch("aimlabs_auth._process_exists", return_value=True),
                patch("aimlabs_auth._process_start_time", return_value=10.0),
            ):
                self.assertFalse(aimlabs_auth._session_lock_is_stale(lock_path, stale_after_seconds=60.0))

    def test_session_lock_is_stale_on_pid_reuse_start_time_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "data" / "session.lock"
            write_lock_file(lock_path, pid_value=12345, start_time=10.0)

            with (
                patch("aimlabs_auth._process_exists", return_value=True),
                patch("aimlabs_auth._process_start_time", return_value=12.5),
            ):
                self.assertTrue(aimlabs_auth._session_lock_is_stale(lock_path, stale_after_seconds=60.0))

    def test_stale_lock_steal_restores_when_recheck_finds_live_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "data" / "session.lock"
            write_lock_file(lock_path, pid_value=12345, start_time=10.0)

            with patch("aimlabs_auth._session_lock_is_stale", side_effect=[True, False]):
                self.assertFalse(aimlabs_auth._steal_stale_session_lock(lock_path, stale_after_seconds=60.0))

            self.assertTrue(lock_path.exists())
            self.assertEqual(json.loads(lock_path.read_text(encoding="utf-8"))["pid"], 12345)

    def test_stale_lock_steal_removes_confirmed_stale_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "data" / "session.lock"
            write_lock_file(lock_path, pid_value=12345, start_time=10.0)

            with patch("aimlabs_auth._session_lock_is_stale", return_value=True):
                self.assertTrue(aimlabs_auth._steal_stale_session_lock(lock_path, stale_after_seconds=60.0))

            self.assertFalse(lock_path.exists())


class RotatingSessionEndpoint:  # pylint: disable=too-few-public-methods
    def __init__(self, *, initial_cookie: str) -> None:
        self.current_cookie = initial_cookie
        self.calls: list[str] = []

    def __call__(self, session_cookie: str, timeout: float) -> SessionFetchResult:
        del timeout
        self.calls.append(session_cookie)
        if session_cookie != self.current_cookie:
            return SessionFetchResult(
                {"accessTokenError": "RefreshAccessTokenError"},
                rotated_session_cookie="dead-cookie",
            )
        call_number = len(self.calls)
        next_cookie = f"cookie-{call_number}"
        self.current_cookie = next_cookie
        return SessionFetchResult(
            {"accessToken": f"bearer-{call_number}", "expires": "2026-07-01T00:00:00.000Z"},
            rotated_session_cookie=next_cookie,
        )


class SerializedRotatingSessionEndpoint(RotatingSessionEndpoint):
    def __init__(self, *, initial_cookie: str) -> None:
        super().__init__(initial_cookie=initial_cookie)
        self.first_call_entered = threading.Event()
        self.release_first_call = threading.Event()
        self.concurrent_fetch_detected = False
        self._active_fetches = 0
        self._lock = threading.Lock()

    def __call__(self, session_cookie: str, timeout: float) -> SessionFetchResult:
        with self._lock:
            self._active_fetches += 1
            if self._active_fetches > 1:
                self.concurrent_fetch_detected = True
            is_first_call = not self.calls
            if is_first_call:
                self.first_call_entered.set()

        if is_first_call:
            self.release_first_call.wait(timeout=2.0)

        try:
            return super().__call__(session_cookie, timeout)
        finally:
            with self._lock:
                self._active_fetches -= 1


class FakeLoginWindow:
    """Mimics a pywebview window whose cookie store already holds the session."""

    def __init__(self, cookies: list) -> None:
        self.cookies = cookies
        self.cookies_requested = threading.Event()
        self.destroyed = False
        self.destroyed_event = threading.Event()

    def get_cookies(self) -> list:
        self.cookies_requested.set()
        return self.cookies

    def destroy(self) -> None:
        self.destroyed = True
        self.destroyed_event.set()


class BlockingLoginWindow:
    """Mimics a backend where get_cookies never returns after the window closes."""

    def __init__(self) -> None:
        self.get_cookies_entered = threading.Event()
        self.release_get_cookies = threading.Event()
        self.destroyed_event = threading.Event()

    def get_cookies(self) -> list:
        self.get_cookies_entered.set()
        self.release_get_cookies.wait()
        return []

    def destroy(self) -> None:
        self.destroyed_event.set()


def fake_webview_module(window: FakeLoginWindow) -> SimpleNamespace:
    def start(starter_func, starter_args):
        starter_func(*starter_args)
        if not window.destroyed_event.wait(timeout=1.0):
            raise AssertionError("login poll thread did not finish")

    return SimpleNamespace(
        create_window=lambda title, start_url: window,
        start=start,
    )


class LoginCaptureTests(unittest.TestCase):
    def test_extract_session_cookie_from_simple_cookie_morsels(self) -> None:
        simple_cookie: SimpleCookie = SimpleCookie()
        simple_cookie[DEFAULT_SESSION_COOKIE] = "captured-token"
        simple_cookie["csrf-token"] = "noise"

        self.assertEqual(extract_session_cookie([simple_cookie]), "captured-token")

    def test_extract_session_cookie_from_cookiejar_objects(self) -> None:
        cookiejar_cookie = SimpleNamespace(name="custom-session-token", value="abc")

        self.assertEqual(extract_session_cookie([cookiejar_cookie]), "custom-session-token=abc")

    def test_extract_session_cookie_joins_chunked_cookies_in_numeric_order(self) -> None:
        chunked_cookies = {
            f"{DEFAULT_SESSION_COOKIE}.1": "bbb",
            f"{DEFAULT_SESSION_COOKIE}.0": "aaa",
        }

        self.assertEqual(
            extract_session_cookie([chunked_cookies]),
            f"{DEFAULT_SESSION_COOKIE}.0=aaa; {DEFAULT_SESSION_COOKIE}.1=bbb",
        )

    def test_extract_session_cookie_from_bare_dict_not_wrapped_in_a_list(self) -> None:
        # Some backends return a single {name: value} dict, not a list of them.
        self.assertEqual(extract_session_cookie({DEFAULT_SESSION_COOKIE: "token"}), "token")

    def test_extract_session_cookie_from_bare_simple_cookie_not_wrapped_in_a_list(self) -> None:
        simple_cookie: SimpleCookie = SimpleCookie()
        simple_cookie[DEFAULT_SESSION_COOKIE] = "captured-token"

        self.assertEqual(extract_session_cookie(simple_cookie), "captured-token")

    def test_extract_session_cookie_returns_none_without_session_token(self) -> None:
        self.assertIsNone(extract_session_cookie([{"csrf-token": "noise"}]))
        self.assertIsNone(extract_session_cookie(None))

    def test_write_env_var_replaces_existing_line_and_preserves_others(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text('OTHER=1\nAIMLAB_SESSION="old"\n# comment\n', encoding="utf-8")

            write_env_var(env_path, "AIMLAB_SESSION", "new")

            self.assertEqual(
                env_path.read_text(encoding="utf-8"),
                'OTHER=1\nAIMLAB_SESSION="new"\n# comment\n',
            )

    def test_write_env_var_creates_file_and_appends_missing_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"

            write_env_var(env_path, "AIMLAB_SESSION", "fresh")
            write_env_var(env_path, "OTHER", "2")

            self.assertEqual(
                env_path.read_text(encoding="utf-8"),
                'AIMLAB_SESSION="fresh"\nOTHER="2"\n',
            )

    def test_login_without_pywebview_prints_install_hint_and_returns_none(self) -> None:
        message_stream = io.StringIO()
        # A None entry makes `import webview` raise ImportError even if installed.
        with patch.dict(sys.modules, {"webview": None}):
            captured_session = login_and_capture(message_stream=message_stream)

        self.assertIsNone(captured_session)
        self.assertIn("pywebview", message_stream.getvalue())

    def test_login_capture_writes_env_and_verifies_identity(self) -> None:
        simple_cookie: SimpleCookie = SimpleCookie()
        simple_cookie[DEFAULT_SESSION_COOKIE] = "captured-token"
        login_window = FakeLoginWindow([simple_cookie])
        message_stream = io.StringIO()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            env_path = temp_path / ".env"
            state_path = temp_path / "data" / "session.json"
            lock_path = temp_path / "data" / "session.lock"
            state_path.parent.mkdir()
            state_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "session_cookie": "stale-cookie",
                        "expires": "2026-07-01T00:00:00.000Z",
                        "updated_at": "2026-06-18T00:00:00.000Z",
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.dict(sys.modules, {"webview": fake_webview_module(login_window)}),
                patch(
                    "aimlabs_auth.fetch_session_json",
                    return_value=SessionFetchResult(
                        {
                            "accessToken": "bearer-1",
                            "user": {"email": "user@example.com"},
                            "expires": "2026-07-01T00:00:00.000Z",
                        },
                        rotated_session_cookie="rotated-token",
                    ),
                ),
            ):
                captured_session = login_and_capture(
                    env_path,
                    state_path=state_path,
                    lock_path=lock_path,
                    message_stream=message_stream,
                )

            self.assertEqual(captured_session, "captured-token")
            self.assertIn('AIMLAB_SESSION="captured-token"', env_path.read_text(encoding="utf-8"))
            self.assertEqual(read_session_state_cookie(state_path), "rotated-token")
        self.assertTrue(login_window.destroyed)
        messages = message_stream.getvalue()
        self.assertIn("verified login as user@example.com", messages)
        self.assertNotIn("captured-token", messages)  # the value is never logged

    def test_login_capture_reports_unverifiable_cookie_but_keeps_it(self) -> None:
        simple_cookie: SimpleCookie = SimpleCookie()
        simple_cookie[DEFAULT_SESSION_COOKIE] = "captured-token"
        login_window = FakeLoginWindow([simple_cookie])
        message_stream = io.StringIO()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            env_path = temp_path / ".env"
            state_path = temp_path / "data" / "session.json"
            lock_path = temp_path / "data" / "session.lock"
            with (
                patch.dict(sys.modules, {"webview": fake_webview_module(login_window)}),
                patch("aimlabs_auth.fetch_session_json", side_effect=AimlabsAuthError("offline")),
            ):
                captured_session = login_and_capture(
                    env_path,
                    state_path=state_path,
                    lock_path=lock_path,
                    message_stream=message_stream,
                )

            self.assertEqual(captured_session, "captured-token")
            self.assertTrue(env_path.exists())
            self.assertFalse(state_path.exists())
        self.assertIn("could not verify it yet", message_stream.getvalue())

    def test_login_timeout_writes_nothing_and_returns_none(self) -> None:
        login_window = FakeLoginWindow([])
        message_stream = io.StringIO()

        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            with patch.dict(sys.modules, {"webview": fake_webview_module(login_window)}):
                captured_session = login_and_capture(env_path, timeout=0.0, message_stream=message_stream)

            self.assertIsNone(captured_session)
            self.assertFalse(env_path.exists())
        self.assertTrue(login_window.destroyed)
        self.assertIn("no session captured", message_stream.getvalue())

    def test_login_close_stops_poll_thread_between_cookie_reads(self) -> None:
        login_window = FakeLoginWindow([])
        message_stream = io.StringIO()
        created_threads = []
        real_thread = threading.Thread

        def recording_thread(*, target, args, daemon):
            poll_thread = real_thread(target=target, args=args, daemon=daemon)
            created_threads.append(poll_thread)
            return poll_thread

        def start(starter_func, starter_args):
            starter_func(*starter_args)
            self.assertTrue(login_window.cookies_requested.wait(timeout=1.0))

        fake_webview = SimpleNamespace(
            create_window=lambda title, start_url: login_window,
            start=start,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            with (
                patch.dict(sys.modules, {"webview": fake_webview}),
                patch("aimlabs_auth.threading.Thread", side_effect=recording_thread),
                patch("aimlabs_auth.LOGIN_POLL_INTERVAL_SECONDS", 30.0),
            ):
                captured_session = login_and_capture(env_path, timeout=60.0, message_stream=message_stream)

            self.assertIsNone(captured_session)
            self.assertFalse(env_path.exists())

        self.assertTrue(login_window.destroyed_event.wait(timeout=1.0))
        self.assertEqual(len(created_threads), 1)
        created_threads[0].join(timeout=1.0)
        self.assertFalse(created_threads[0].is_alive())
        self.assertIn("no session captured", message_stream.getvalue())

    def test_login_close_returns_even_if_get_cookies_is_blocked(self) -> None:
        login_window = BlockingLoginWindow()
        message_stream = io.StringIO()
        created_threads = []
        real_thread = threading.Thread

        def recording_thread(*, target, args, daemon):
            poll_thread = real_thread(target=target, args=args, daemon=daemon)
            created_threads.append(poll_thread)
            return poll_thread

        def start(starter_func, starter_args):
            starter_func(*starter_args)
            self.assertTrue(login_window.get_cookies_entered.wait(timeout=1.0))

        fake_webview = SimpleNamespace(
            create_window=lambda title, start_url: login_window,
            start=start,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            try:
                with (
                    patch.dict(sys.modules, {"webview": fake_webview}),
                    patch("aimlabs_auth.threading.Thread", side_effect=recording_thread),
                ):
                    captured_session = login_and_capture(env_path, timeout=60.0, message_stream=message_stream)

                self.assertIsNone(captured_session)
                self.assertFalse(env_path.exists())
                self.assertEqual(len(created_threads), 1)
                self.assertTrue(created_threads[0].daemon)
                self.assertTrue(created_threads[0].is_alive())
                self.assertIn("no session captured", message_stream.getvalue())
            finally:
                login_window.release_get_cookies.set()
                for poll_thread in created_threads:
                    poll_thread.join(timeout=1.0)

        self.assertTrue(login_window.destroyed_event.is_set())

    def test_login_hints_when_backend_hides_the_session_cookie(self) -> None:
        login_window = FakeLoginWindow([{"csrf-token": "noise"}])
        message_stream = io.StringIO()

        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            with (
                patch.dict(sys.modules, {"webview": fake_webview_module(login_window)}),
                patch("aimlabs_auth.LOGIN_POLL_INTERVAL_SECONDS", 0.0),
                patch("aimlabs_auth.time.time", side_effect=[0.0, 0.5, 2.0]),
            ):
                captured_session = login_and_capture(env_path, timeout=1.0, message_stream=message_stream)

            self.assertIsNone(captured_session)
            self.assertFalse(env_path.exists())
        messages = message_stream.getvalue()
        self.assertIn("may be hiding the httpOnly session cookie", messages)
        self.assertIn("csrf-token", messages)


if __name__ == "__main__":
    unittest.main()
