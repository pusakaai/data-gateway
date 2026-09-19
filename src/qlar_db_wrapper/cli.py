"""Command line: `qlar-db-wrapper <command>`.

Five commands, in the order an operator meets them:

    test-db      check the database credentials before involving Qlar at all
    enroll       register with Qlar using the one-time code from the CMS
    fingerprint  print this wrapper's key fingerprint, to compare with the CMS
    run          the long-running process: poll, execute, answer
    version      what is installed, and which protocol it speaks
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
from .executor import account_can_write, test_connection
from .poll import PollLoop

DEFAULT_ENV_FILE = ".env"


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
    subparsers.add_parser("run", help="poll Qlar for jobs and execute them (the main process)")
    subparsers.add_parser("enroll", help="register this wrapper with Qlar using a one-time code")
    subparsers.add_parser("test-db", help="verify the database settings without contacting Qlar")
    subparsers.add_parser("fingerprint", help="print this wrapper's public key fingerprint")
    subparsers.add_parser("version", help="print version and protocol information")

    args = parser.parse_args(argv)
    _configure_logging(args.log_level)

    if args.command == "version":
        print(f"qlar-db-wrapper {__version__}")
        print(f"protocol {PROTOCOL_VERSION}")
        return 0

    try:
        settings = load_settings(Path(args.env_file))
    except ConfigError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2

    if args.command == "test-db":
        return _command_test_db(settings)
    if args.command == "fingerprint":
        return _command_fingerprint(settings)
    if args.command == "enroll":
        return _command_enroll(settings)
    if args.command == "run":
        return _command_run(settings)

    return 2


def _command_test_db(settings: Settings) -> int:
    database = settings.database
    print(f"connecting to {database.provider}://{database.host}/{database.database} ...")
    result = test_connection(settings.database)

    if result.status != "ok":
        error = result.error or {}
        print(f"FAILED ({error.get('category')}): {error.get('messageText')}", file=sys.stderr)
        return 1

    version = (result.rows or [[""]])[0][0]
    print(f"OK in {result.duration_ms} ms")
    print(f"server: {version}")

    # Advisory, never fatal. An operator who deliberately granted more is not blocked, but
    # nobody should discover months later that the "read-only" wrapper could drop tables.
    if account_can_write(settings.database) is True:
        print(
            "\nWARNING: this database account appears able to modify data.\n"
            "         The wrapper only ever issues read-only transactions, but a read-only\n"
            "         account is the guarantee that does not depend on our code being right.",
            file=sys.stderr,
        )
    return 0


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


def _command_run(settings: Settings) -> int:
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
