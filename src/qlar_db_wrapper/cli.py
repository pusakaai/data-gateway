"""Command line: `qlar-db-wrapper <command>`.

Five commands, in the order an operator meets them:

    test-db      check the database credentials before involving Qlar at all
    enroll       register with Qlar using the one-time code from the CMS
    fingerprint  print this wrapper's key fingerprint, to compare with the CMS
    run          the long-running process: poll, execute, answer
    version      what is installed, and which protocol it speaks

`run`, `test-db` and `enroll` need database settings. When those are missing and there is
a terminal to ask on, the setup prompts in `wizard.py` collect them and write `.env`
instead of printing a configuration error — so a fresh install is `pip install` then
`run`, with nothing to read first. `--init` runs those prompts even when `.env` is already
complete. Without a terminal — a container, a systemd unit — nothing changes: the same
configuration error as before, on stderr, with the same exit code.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from pathlib import Path

from . import PROTOCOL_VERSION, __version__
from .config import ConfigError, EnrollmentState, Settings, load_settings
from .crypto import fingerprint, load_or_create_private_key, public_key_pem
from .enroll import EnrollmentError, enroll
from .poll import PollLoop
from .wizard import SetupAborted, can_prompt, check_connection, run_setup

DEFAULT_ENV_FILE = ".env"

# The commands that need database settings, and so may offer the setup prompts.
SETUP_COMMANDS = ("run", "test-db", "enroll")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="qlar-db-wrapper",
        description="On-premise SQL access wrapper for Qlar. Outbound-only: no inbound port is opened.",
    )
    parser.add_argument(
        "--env-file", default=DEFAULT_ENV_FILE, help="path to the settings file (default: .env)"
    )
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    parser.add_argument(
        "--version",
        action="version",
        version=f"qlar-db-wrapper {__version__} (protocol {PROTOCOL_VERSION})",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    _with_init_flag(
        subparsers.add_parser("run", help="poll Qlar for jobs and execute them (the main process)")
    )
    _with_init_flag(
        subparsers.add_parser("enroll", help="register this wrapper with Qlar using a one-time code")
    )
    _with_init_flag(
        subparsers.add_parser("test-db", help="verify the database settings without contacting Qlar")
    )
    subparsers.add_parser("fingerprint", help="print this wrapper's public key fingerprint")
    subparsers.add_parser("version", help="print version and protocol information")

    args = parser.parse_args(argv)
    _configure_logging(args.log_level)

    if args.command == "version":
        print(f"qlar-db-wrapper {__version__}")
        print(f"protocol {PROTOCOL_VERSION}")
        return 0

    settings, connection_ok = _settings_for(args)
    if settings is None:
        return 2

    if args.command == "test-db":
        return _command_test_db(settings, connection_ok)
    if args.command == "fingerprint":
        return _command_fingerprint(settings)
    if args.command == "enroll":
        return _command_enroll(settings)
    if args.command == "run":
        return _command_run(settings, connection_ok)

    return 2


def _with_init_flag(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument(
        "--init",
        "-init",
        action="store_true",
        help="ask for the database details again, even when .env is already filled in",
    )
    return parser


def _settings_for(args: argparse.Namespace) -> tuple[Settings | None, bool | None]:
    """Loads the settings, running the setup prompts when that is the helpful thing to do.

    Returns the settings and whether the database connection has already been tested:
    True or False when the prompts tested it, None when nothing has been checked yet. That
    third state is what stops `run` from opening two connections to say the same thing.
    """
    env_file = Path(args.env_file)

    if getattr(args, "init", False):
        if not can_prompt():
            print(
                "--init needs a terminal to ask on. Edit .env directly, or run the container "
                "interactively (docker run -it ... run --init).",
                file=sys.stderr,
            )
            return None, None
        return _run_setup(env_file)

    try:
        return load_settings(env_file), None
    except ConfigError as error:
        if args.command not in SETUP_COMMANDS or not can_prompt():
            print(f"configuration error: {error}", file=sys.stderr)
            return None, None

        # First run, most likely: no .env at all, or one that was never filled in. Ask
        # rather than explain, since everything the explanation would say is a question.
        print(f"This wrapper is not configured yet ({error}).")
        return _run_setup(env_file)


def _run_setup(env_file: Path) -> tuple[Settings | None, bool | None]:
    try:
        return run_setup(env_file)
    except SetupAborted as error:
        print(f"setup cancelled: {error}", file=sys.stderr)
        return None, None


def _command_test_db(settings: Settings, connection_ok: bool | None) -> int:
    # The check itself lives with the setup prompts: it is the same paragraph of output
    # there and here, and an operator comparing the two should not have to wonder whether
    # they mean the same thing.
    if connection_ok is None:
        connection_ok = check_connection(settings)
    return 0 if connection_ok else 1


def _command_fingerprint(settings: Settings) -> int:
    private_key, created = load_or_create_private_key(settings.key_file)
    if created:
        print(f"generated a new key pair at {settings.key_file}")
    print(fingerprint(public_key_pem(private_key)))
    return 0


def _command_enroll(settings: Settings) -> int:
    try:
        state, key_fingerprint = enroll(settings)
    except EnrollmentError as error:
        print(f"enrolment failed: {error}", file=sys.stderr)
        return 1

    print(f"enrolled as {state.wrapper_id}")
    print(f"state written to {settings.state_file}")
    print()
    print("Fingerprint of this wrapper's key:")
    print(f"  {key_fingerprint}")
    print()
    print("Open the Qlar CMS, check that the fingerprint shown there matches the line above,")
    print("and approve this wrapper. Then start it with:  qlar-db-wrapper run")
    return 0


def _command_run(settings: Settings, connection_ok: bool | None) -> int:
    # Before anything else: does this actually reach the database? A wrapper that polls
    # happily while every query fails looks healthy in the CMS, and that is the most
    # confusing way for an installation to be broken.
    if connection_ok is None:
        connection_ok = check_connection(settings)
    if not connection_ok:
        print(
            "Starting anyway — the database may simply be down at this moment, and the wrapper\n"
            "reports its state to Qlar on every poll. Use `qlar-db-wrapper run --init` to\n"
            "re-enter the connection details.",
            file=sys.stderr,
        )

    state = EnrollmentState.load(settings.state_file)
    if state is None:
        print(
            f"not enrolled yet (no {settings.state_file}). Run: qlar-db-wrapper enroll",
            file=sys.stderr,
        )
        return 2

    if state.base_url != settings.base_url:
        print(
            f"QLAR_BASE_URL ({settings.base_url}) does not match the URL this wrapper enrolled "
            f"against ({state.base_url}). Re-enrol if the Qlar endpoint really changed.",
            file=sys.stderr,
        )
        return 2

    loop = PollLoop(settings, state)

    def _handle_signal(signum, _frame):  # noqa: ANN001 - signal handler signature
        logging.getLogger("qlar_db_wrapper").info("received signal %s, finishing in-flight jobs", signum)
        loop.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    loop.run_forever()
    return 0


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
