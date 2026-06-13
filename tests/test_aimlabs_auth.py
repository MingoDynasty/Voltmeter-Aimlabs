from http.cookies import SimpleCookie
import io
import json
import sys
from types import SimpleNamespace
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aimlabs_auth import (
    DEFAULT_SESSION_COOKIE,
    AimlabsAuthError,
    ReloginRequiredError,
    extract_session_cookie,
    fetch_session_json,
    get_bearer_from_session,
    load_dotenv_values,
    login_and_capture,
    read_session_cookie_file,
    resolve_session_cookie,
    write_env_var,
)
from config import AppConfig


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
                app_config=AppConfig(aimlabs_session_cookie="config-cookie"),
            )

        self.assertEqual(resolved.session_cookie, "file-cookie")
        self.assertEqual(resolved.source, str(session_path))

    def test_env_takes_precedence_over_dotenv_and_legacy_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            dotenv_path = Path(temp_dir) / ".env"
            dotenv_path.write_text('AIMLAB_SESSION="dotenv-cookie"\n', encoding="utf-8")

            resolved = resolve_session_cookie(
                env={"AIMLAB_SESSION": " env-cookie "},
                dotenv_path=dotenv_path,
                app_config=AppConfig(aimlabs_session_cookie="config-cookie"),
            )

        self.assertEqual(resolved.session_cookie, "env-cookie")
        self.assertEqual(resolved.source, "$AIMLAB_SESSION")

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

            def read(self) -> bytes:
                return json.dumps({"accessToken": "fresh-token"}).encode("utf-8")

        def fake_urlopen(request, timeout: float):
            requests.append((request, timeout))
            return FakeResponse()

        with patch("aimlabs_auth.urllib.request.urlopen", fake_urlopen):
            payload = fetch_session_json(full_cookie, timeout=12)

        self.assertEqual(payload["accessToken"], "fresh-token")
        self.assertEqual(requests[0][0].get_header("Cookie"), full_cookie)
        self.assertEqual(requests[0][1], 12)

    def test_missing_session_raises_login_message(self) -> None:
        with self.assertRaisesRegex(AimlabsAuthError, "voltmeter login"):
            resolve_session_cookie(env={}, dotenv_path=None, app_config=AppConfig())

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


class FakeLoginWindow:
    """Mimics a pywebview window whose cookie store already holds the session."""

    def __init__(self, cookies: list) -> None:
        self.cookies = cookies
        self.destroyed = False

    def get_cookies(self) -> list:
        return self.cookies

    def destroy(self) -> None:
        self.destroyed = True


def fake_webview_module(window: FakeLoginWindow) -> SimpleNamespace:
    # webview.start(poll_func, args) runs the poll loop in a thread; the fake runs it inline.
    return SimpleNamespace(
        create_window=lambda title, start_url: window,
        start=lambda poll_func, poll_args: poll_func(*poll_args),
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
            env_path = Path(temp_dir) / ".env"
            with (
                patch.dict(sys.modules, {"webview": fake_webview_module(login_window)}),
                patch(
                    "aimlabs_auth.fetch_session_json",
                    return_value={"user": {"email": "user@example.com"}, "expires": "2026-07-01T00:00:00.000Z"},
                ),
            ):
                captured_session = login_and_capture(env_path, message_stream=message_stream)

            self.assertEqual(captured_session, "captured-token")
            self.assertIn('AIMLAB_SESSION="captured-token"', env_path.read_text(encoding="utf-8"))
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
            env_path = Path(temp_dir) / ".env"
            with (
                patch.dict(sys.modules, {"webview": fake_webview_module(login_window)}),
                patch("aimlabs_auth.fetch_session_json", side_effect=AimlabsAuthError("offline")),
            ):
                captured_session = login_and_capture(env_path, message_stream=message_stream)

            self.assertEqual(captured_session, "captured-token")
            self.assertTrue(env_path.exists())
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

    def test_login_hints_when_backend_hides_the_session_cookie(self) -> None:
        login_window = FakeLoginWindow([{"csrf-token": "noise"}])
        message_stream = io.StringIO()

        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            with (
                patch.dict(sys.modules, {"webview": fake_webview_module(login_window)}),
                patch("aimlabs_auth.time.sleep"),
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
