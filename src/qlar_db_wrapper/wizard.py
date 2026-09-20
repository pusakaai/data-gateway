"""First-run setup: ask for the database details, write them to `.env`, prove they work.

Installing the wrapper and starting it should be the whole job. Copying `.env.example`,
remembering which variable names the loader expects, and discovering a typo hours later
when the first query fails is work that a handful of prompts can do instead.

So a start with no usable configuration asks, writes the answers to `.env`, and runs one
query against the database before going any further. The answers are written to the file
precisely so that this happens exactly once: the next start — a restart, a systemd unit, a
replaced container — reads the file and never asks again. `--init` asks anyway, for the
day the password rotates or the database moves.

None of this is mandatory. A `.env` written by hand, or environment variables set by a
container, still take precedence and the prompts never appear; and without a terminal to
ask on, the old configuration error is printed exactly as before. The prompts are a
convenience for a human at a console, never a new requirement for automation.
"""

from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .config import (
    SUPPORTED_PROVIDERS,
    ConfigError,
    Settings,
    load_dotenv,
    load_settings,
    unquote_env_value,
    write_env_values,
)
from .executor import account_can_write, test_connection
from .providers import default_port

# No default. The endpoint differs per Qlar deployment, the CMS wrapper panel prints the
# right one, and a plausible-looking guess is worse than a question: it fails at enrolment
# with a 404 that reads like a rejected code.
BASE_URL_HINT = "ends in /api/db-wrapper — the CMS wrapper panel shows it"

# Names people actually type, mapped to the four `DB_PROVIDER` values.
PROVIDER_ALIASES = {
    "postgres": "postgresql",
    "postgre": "postgresql",
    "pg": "postgresql",
    "psql": "postgresql",
    "mariadb": "mysql",
    "mssql": "sqlserver",
    "sqlsrv": "sqlserver",
    "sql server": "sqlserver",
    "ora": "oracle",
}


class SetupAborted(Exception):
    """The operator cancelled the prompts, or there was no terminal to ask on."""


def can_prompt() -> bool:
    """True when there is a human at a console to answer.

    A container started without a TTY, a systemd unit and a cron job all answer False here
    and get the configuration error they have always got. Prompting into a log file that
    nobody is reading would turn a clear failure into a process that appears to hang.
    """
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):  # pragma: no cover - detached stdin
        return False


def run_setup(env_file: Path) -> tuple[Settings, bool]:
    """Asks for the database details, saves them, and tests them.

    Returns the settings and whether the test query succeeded. A failed test is not fatal:
    the operator is told, offered another go, and — if they decline — left with a saved
    `.env` they can fix by hand or with `--init`. Refusing to continue would be worse; a
    database that is merely down at this moment is a normal thing for a service to survive.
    """
    # Whatever is already configured becomes the default answer, so re-running `--init`
    # to change one field is a row of Enter presses and one new value.
    try:
        load_dotenv(env_file)
    except ConfigError as error:
        # An unreadable file has nothing to offer as defaults, but it is not a reason to
        # refuse to set the wrapper up - that is exactly what the operator is here for.
        print(f"Ignoring the existing file: {error}", file=sys.stderr)

    _banner(env_file)

    while True:
        answers = _collect_answers()
        backup = write_env_values(env_file, answers)
        if backup is not None:
            print(f"\nThe previous {env_file} could not be read; it is kept as {backup}.")

        # The file is for the *next* start; this process is already past the point where
        # it read the environment, so the answers go into it directly.
        os.environ.update(answers)

        print()
        print(f"Saved to {env_file} (mode 0600). The next start will not ask again.")
        print()

        try:
            # Not `load_settings(env_file)`: the file has just been loaded, and real
            # environment variables win there, which would hide the answers just given.
            settings = load_settings(None)
        except ConfigError as error:
            print(f"That configuration cannot be used: {error}", file=sys.stderr)
            if _ask_yes_no("Enter the details again?", default=True):
                continue
            raise SetupAborted(str(error)) from error

        if check_connection(settings):
            return settings, True

        print()
        if _ask_yes_no("Enter the details again?", default=True):
            continue

        print(
            f"Leaving the details as saved in {env_file}. "
            "Fix them there, or run `qlar-db-wrapper run --init` to be asked again.",
            file=sys.stderr,
        )
        return settings, False


def ask_enrollment_code() -> str:
    """Asks for the one-time code, rather than sending the operator back to edit a file.

    This is the one answer the setup prompts used to leave out, and it is the one most
    likely to be got wrong: it arrives by copy and paste from a web page, into a file, on a
    machine whose shell may not quote the way the instructions assumed. A prompt has no
    shell in it at all.

    Not written to `.env`. The code is single-use and spent the moment enrolment succeeds,
    so keeping it would leave a dead credential on disk and one more thing to explain.
    """
    print()
    print("The one-time enrolment code is shown in the Qlar CMS:")
    print("  your agent → Plugins → SQL Database Reader → Connect via wrapper")
    print("It expires 15 minutes after it is generated, and works once.")

    while True:
        answer = _ask("  Enrolment code")
        # Forgiving about how it arrived: pasted with the quotes from a shell snippet, in
        # lower case, or with stray spaces. The alphabet the CMS generates is upper case.
        code = unquote_env_value(answer.strip()).strip().upper()
        if code:
            return code


def check_connection(settings: Settings) -> bool:
    """Runs one query against the database and reports what happened, in words.

    Used at every start, not only during setup: the most common support question about any
    on-premise agent is "is it actually talking to my database?", and the honest place to
    answer it is the first line of the log rather than the first failed query an hour later.
    """
    database = settings.database
    port = database.port or default_port(database.provider)
    print(
        f"Testing the database connection: {database.provider}://{database.user}@"
        f"{database.host}:{port}/{database.database}"
    )

    result = test_connection(database)

    if result.status != "ok":
        error = result.error or {}
        print(
            f"  FAILED ({error.get('category')}): {error.get('messageText')}",
            file=sys.stderr,
        )
        if error.get("hint"):
            print(f"  hint: {error['hint']}", file=sys.stderr)
        return False

    version = (result.rows or [[""]])[0][0]
    print(f"  OK in {result.duration_ms} ms")
    print(f"  server: {_one_line(str(version))}")

    # Advisory, never fatal. An operator who deliberately granted more is not blocked, but
    # nobody should discover months later that the "read-only" wrapper could drop tables.
    if account_can_write(database) is True:
        print(
            "\n  WARNING: this database account appears able to modify data.\n"
            "           The wrapper only ever issues read-only transactions, but a read-only\n"
            "           account is the guarantee that does not depend on our code being right.",
            file=sys.stderr,
        )
    return True


def _banner(env_file: Path) -> None:
    print()
    print("=" * 72)
    print(" Qlar DB Wrapper — setup")
    print("=" * 72)
    print(f"Answer a few questions and they will be saved to {env_file}.")
    print("Press Enter to accept the value in [brackets]. Ctrl-C cancels.")
    print()


def _collect_answers() -> dict[str, str]:
    """The questions themselves, in the order someone reads them off a connection string."""
    print("Database")

    provider = _ask_provider(_current("DB_PROVIDER").lower() or SUPPORTED_PROVIDERS[0])

    # Nobody keeps the five parts of a connection separate in their head; they have a URL
    # from their DBA. Accept it whole and use its parts as the defaults below, still shown
    # one by one so that what was understood is visible before anything is saved.
    host_answer = _ask("  Host or connection URL", _current("DB_HOST") or None)
    pasted = _parse_connection_url(host_answer)
    host = pasted.get("host") or host_answer

    port = _ask_port(pasted.get("port") or _current("DB_PORT") or str(default_port(provider) or ""))
    database = _ask("  Database name", pasted.get("database") or _current("DB_NAME") or None)
    user = _ask("  Username", pasted.get("user") or _current("DB_USER") or None)

    url_password = pasted.get("password")
    saved_password = _current("DB_PASSWORD", strip=False)
    keep_label = "taken from the URL" if url_password else ("unchanged" if saved_password else None)
    password = _ask_secret("  Password", keep_label=keep_label) or (url_password or saved_password)

    print()
    print("Qlar")
    print(f"  ({BASE_URL_HINT})")
    base_url = _ask("  Qlar API endpoint", _current("QLAR_BASE_URL") or None).rstrip("/")

    return {
        "QLAR_BASE_URL": base_url,
        "DB_PROVIDER": provider,
        "DB_HOST": host,
        "DB_PORT": port,
        "DB_NAME": database,
        "DB_USER": user,
        "DB_PASSWORD": password,
    }


def _current(name: str, *, strip: bool = True) -> str:
    value = os.environ.get(name, "")
    return value.strip() if strip else value


def _ask(question: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            answer = input(f"{question}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            raise SetupAborted("cancelled at the prompt") from None
        if answer:
            return answer
        if default:
            return default
        print("    this one is required")


def _ask_secret(question: str, *, keep_label: str | None) -> str | None:
    """Reads a password without echoing it. Returns None to mean "keep what we had"."""
    suffix = f" [{keep_label}]" if keep_label else ""
    while True:
        try:
            answer = getpass.getpass(f"{question}{suffix}: ")
        except (EOFError, KeyboardInterrupt):
            print()
            raise SetupAborted("cancelled at the prompt") from None
        if answer:
            return answer
        if keep_label:
            return None
        print("    a password is required (the wrapper does not support passwordless login)")


def _ask_provider(default: str) -> str:
    print("  " + "  ".join(f"{index}) {name}" for index, name in enumerate(SUPPORTED_PROVIDERS, 1)))
    while True:
        answer = _ask("  Which one", default).strip().lower()
        if answer in SUPPORTED_PROVIDERS:
            return answer
        if answer in PROVIDER_ALIASES:
            return PROVIDER_ALIASES[answer]
        if answer.isdigit() and 1 <= int(answer) <= len(SUPPORTED_PROVIDERS):
            return SUPPORTED_PROVIDERS[int(answer) - 1]
        print(f"    choose a number, or one of: {', '.join(SUPPORTED_PROVIDERS)}")


def _ask_port(default: str) -> str:
    while True:
        answer = _ask("  Port", default or None)
        if answer.isdigit() and 1 <= int(answer) <= 65535:
            return answer
        print("    a port is a whole number between 1 and 65535")


def _ask_yes_no(question: str, *, default: bool) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            answer = input(f"{question} {suffix}: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False


def _parse_connection_url(answer: str) -> dict[str, str]:
    """Pulls host, port, database, user and password out of a pasted connection string.

    Understands `postgresql://user:pass@host:5432/db`, the `jdbc:` prefixed form of the
    same, and a bare `host:port`. Anything it does not recognise comes back empty, and the
    answer is then treated as a plain hostname — which is what it almost always is.
    """
    text = answer.strip()
    if not text:
        return {}

    if text.lower().startswith("jdbc:"):
        text = text[len("jdbc:") :]

    if "://" not in text:
        host, separator, port = text.partition(":")
        if separator and port.strip().isdigit():
            return {"host": host.strip(), "port": port.strip()}
        return {}

    try:
        parsed = urlsplit(text)
    except ValueError:
        return {}

    parts: dict[str, str] = {}
    if parsed.hostname:
        parts["host"] = parsed.hostname
    try:
        if parsed.port:
            parts["port"] = str(parsed.port)
    except ValueError:  # a non-numeric port in the pasted text
        pass
    database = parsed.path.lstrip("/").split("?", 1)[0]
    if database:
        parts["database"] = unquote(database)
    if parsed.username:
        parts["user"] = unquote(parsed.username)
    if parsed.password:
        parts["password"] = unquote(parsed.password)
    return parts


def _one_line(text: str, limit: int = 100) -> str:
    """Server banners are multi-line and long; one readable line is the useful part."""
    flattened = " ".join(text.split())
    return flattened if len(flattened) <= limit else flattened[: limit - 1] + "…"
