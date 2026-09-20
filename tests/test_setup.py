"""The setup prompts, driven by a scripted console instead of a human.

Two things are being protected here. The first is the happy path an operator meets once:
answer six questions, get a working `.env`, see the connection proved. The second is that
automation never meets it at all — a container has no terminal, and a wrapper that stops
to ask a question nobody can see would look exactly like a wrapper that has hung.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qlar_db_wrapper import cli, wizard
from qlar_db_wrapper.executor import ExecutionResult

MANAGED_KEYS = (
    "QLAR_BASE_URL",
    "QLAR_ENROLLMENT_CODE",
    "QLAR_WRAPPER_NAME",
    "DB_PROVIDER",
    "DB_HOST",
    "DB_PORT",
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
    "TABLE_ALLOWLIST",
    "AUDIT_LOG_FILE",
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """Settings come from the environment, so every test needs an empty one."""
    for key in MANAGED_KEYS:
        monkeypatch.delenv(key, raising=False)


class Console:
    """A scripted terminal: answers are consumed in the order they were queued."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def input(self, prompt: str = "") -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"nothing scripted for prompt {prompt!r}")
        return self.answers.pop(0)

    def install(self, monkeypatch, *, connection_ok: bool = True, writable: bool | None = False):
        monkeypatch.setattr("builtins.input", self.input)
        monkeypatch.setattr(wizard.getpass, "getpass", self.input)
        monkeypatch.setattr(wizard, "can_prompt", lambda: True)
        monkeypatch.setattr(wizard, "test_connection", _connection(connection_ok))
        monkeypatch.setattr(wizard, "account_can_write", lambda _settings: writable)
        return self


def _connection(ok: bool):
    def fake_test_connection(_settings):
        if ok:
            return ExecutionResult(status="ok", rows=[["PostgreSQL 16.4"]], row_count=1, duration_ms=7)
        return ExecutionResult(
            status="error",
            duration_ms=3,
            error={"category": "connection", "messageText": "could not connect", "hint": None},
        )

    return fake_test_connection


def env_values(env_file: Path) -> dict[str, str]:
    from qlar_db_wrapper.config import unquote_env_value

    values = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _, value = stripped.partition("=")
            values[key.strip()] = unquote_env_value(value.strip())
    return values


class TestFirstRun:
    def test_answers_become_a_working_env_file(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        Console(
            "",  # database type: the offered default
            "db.internal",  # host
            "",  # port: the provider's default
            "warehouse",  # database name
            "qlar_readonly",  # username
            "s3cret pass",  # password, via getpass
            "",  # Qlar endpoint: the offered default
        ).install(monkeypatch)

        settings, connection_ok = wizard.run_setup(env_file)

        assert connection_ok is True
        assert settings.database.provider == "postgresql"
        assert settings.database.port == 5432
        assert settings.database.password == "s3cret pass"
        assert settings.base_url == wizard.DEFAULT_BASE_URL

        saved = env_values(env_file)
        assert saved["DB_HOST"] == "db.internal"
        assert saved["DB_PORT"] == "5432"
        assert saved["DB_NAME"] == "warehouse"
        assert saved["DB_USER"] == "qlar_readonly"
        assert saved["DB_PASSWORD"] == "s3cret pass"

    def test_a_pasted_url_fills_in_the_rest(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        console = Console(
            "mysql",
            "mysql://ana:pw%40word@db.internal:3307/warehouse",
            "",  # port, offered as 3307 from the URL
            "",  # database name, offered as warehouse
            "",  # username, offered as ana
            "",  # password: keep the one from the URL
            "",  # Qlar endpoint
        ).install(monkeypatch)

        settings, _ = wizard.run_setup(env_file)

        assert settings.database.provider == "mysql"
        assert settings.database.host == "db.internal"
        assert settings.database.port == 3307
        assert settings.database.database == "warehouse"
        assert settings.database.user == "ana"
        assert settings.database.password == "pw@word"
        # The parsed values are shown as defaults, not applied silently.
        assert any("3307" in prompt for prompt in console.prompts)

    def test_a_bare_host_and_port_is_understood_too(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        Console("1", "db.internal:6432", "", "warehouse", "reader", "pw", "").install(monkeypatch)

        settings, _ = wizard.run_setup(env_file)

        assert settings.database.host == "db.internal"
        assert settings.database.port == 6432


class TestAnsweringAgain:
    def test_saved_values_are_offered_as_defaults(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "\n".join(
                [
                    "# hand-written, keep me",
                    "QLAR_BASE_URL=https://qlar.example.com/api/db-wrapper",
                    "DB_PROVIDER=postgresql",
                    "DB_HOST=old.internal",
                    "DB_PORT=5432",
                    "DB_NAME=warehouse",
                    "DB_USER=qlar_readonly",
                    "DB_PASSWORD=old-secret",
                    "MAX_CONCURRENT_QUERIES=9",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        # Every answer is Enter except the host: the one field being changed.
        Console("", "new.internal", "", "", "", "", "").install(monkeypatch)

        settings, _ = wizard.run_setup(env_file)

        assert settings.database.host == "new.internal"
        assert settings.database.password == "old-secret"
        assert settings.base_url == "https://qlar.example.com/api/db-wrapper"

        text = env_file.read_text(encoding="utf-8")
        assert "# hand-written, keep me" in text
        assert "MAX_CONCURRENT_QUERIES=9" in text

    def test_a_failed_connection_offers_another_attempt(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        console = Console(
            "", "db.internal", "", "warehouse", "reader", "pw", "",
            "n",  # no, do not enter them again
        ).install(monkeypatch, connection_ok=False)

        settings, connection_ok = wizard.run_setup(env_file)

        assert connection_ok is False
        # The answers are still saved: they are usually nearly right, and an operator who
        # fixes one line by hand should not have to retype the other six.
        assert env_values(env_file)["DB_HOST"] == "db.internal"
        assert settings.database.host == "db.internal"
        assert console.answers == []

    def test_declining_at_the_prompt_can_be_retried(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        Console(
            "", "typo.internal", "", "warehouse", "reader", "pw", "",
            "y",  # yes, ask again
            "", "db.internal", "", "warehouse", "reader", "pw", "",
            "n",
        ).install(monkeypatch, connection_ok=False)

        settings, connection_ok = wizard.run_setup(env_file)

        assert connection_ok is False
        assert settings.database.host == "db.internal"


class TestWithoutATerminal:
    def test_missing_configuration_still_fails_the_old_way(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(wizard, "can_prompt", lambda: False)
        monkeypatch.setattr(cli, "can_prompt", lambda: False)

        exit_code = cli.main(["--env-file", str(tmp_path / ".env"), "test-db"])

        assert exit_code == 2
        assert "configuration error" in capsys.readouterr().err

    def test_init_says_why_it_cannot_ask(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(cli, "can_prompt", lambda: False)

        exit_code = cli.main(["--env-file", str(tmp_path / ".env"), "run", "--init"])

        assert exit_code == 2
        assert "needs a terminal" in capsys.readouterr().err


class TestTheInitFlag:
    @pytest.mark.parametrize("flag", ["--init", "-init"])
    def test_both_spellings_re_ask_over_a_complete_env_file(self, tmp_path, monkeypatch, flag):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "\n".join(
                [
                    "QLAR_BASE_URL=https://qlar.example.com/api/db-wrapper",
                    "DB_PROVIDER=postgresql",
                    "DB_HOST=old.internal",
                    "DB_PORT=5432",
                    "DB_NAME=warehouse",
                    "DB_USER=qlar_readonly",
                    "DB_PASSWORD=old-secret",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        Console("", "new.internal", "", "", "", "", "").install(monkeypatch)
        monkeypatch.setattr(cli, "can_prompt", lambda: True)

        exit_code = cli.main(["--env-file", str(env_file), "test-db", flag])

        assert exit_code == 0
        assert env_values(env_file)["DB_HOST"] == "new.internal"

    def test_without_the_flag_a_complete_env_file_asks_nothing(self, tmp_path, monkeypatch, capsys):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "\n".join(
                [
                    "QLAR_BASE_URL=https://qlar.example.com/api/db-wrapper",
                    "DB_PROVIDER=postgresql",
                    "DB_HOST=db.internal",
                    "DB_PORT=5432",
                    "DB_NAME=warehouse",
                    "DB_USER=qlar_readonly",
                    "DB_PASSWORD=secret",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        def refuse(_prompt: str = "") -> str:
            raise AssertionError("a filled-in .env must not be questioned")

        monkeypatch.setattr("builtins.input", refuse)
        monkeypatch.setattr(wizard, "test_connection", _connection(True))
        monkeypatch.setattr(wizard, "account_can_write", lambda _settings: False)

        assert cli.main(["--env-file", str(env_file), "test-db"]) == 0
        assert "OK in 7 ms" in capsys.readouterr().out
