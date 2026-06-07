import tempfile
import unittest
from pathlib import Path

from aimlabs_auth import (
    AimlabsAuthError,
    ReloginRequiredError,
    get_bearer_from_session,
    load_dotenv_values,
    resolve_session_cookie,
)
from config import AppConfig


class AimlabsAuthTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
