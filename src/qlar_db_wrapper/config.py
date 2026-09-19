"""Settings and persisted enrolment state.

Two files, with deliberately different lifetimes:

* `.env` — written by the operator. Qlar's URL, the one-time enrolment code, and the
  database credentials. **The database credentials live here and only here**; they are
  never sent to Qlar.
* `wrapper-state.json` — written by the wrapper at enrolment. The wrapper id Qlar issued
  and Qlar's public key, pinned so later jobs can be verified.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED_PROVIDERS = ("postgresql", "mysql", "sqlserver", "oracle")

DEFAULT_STATEMENT_TIMEOUT_SECONDS = 30
DEFAULT_POLL_TIMEOUT_SECONDS = 25
DEFAULT_MAX_RESULT_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_CONCURRENT_QUERIES = 5


class ConfigError(Exception):
    """The wrapper cannot start with the configuration it was given."""


def load_dotenv(path: Path) -> None:
    """Loads `KEY=value` lines into the environment without overwriting real env vars.

    Hand-rolled rather than pulling in python-dotenv: the format we need is three lines of
    parsing, and every dependency in this process is one more thing a customer's security
    review has to cover. Real environment variables win, so a container can override the
    file without editing it.
    """
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class DatabaseSettings:
    provider: str
    host: str
    port: int | None
    database: str
    user: str
    password: str
    # Provider-specific connect options, e.g. sslmode for PostgreSQL or an Oracle service
    # name. Kept as a plain mapping so a customer can pass something we did not anticipate
    # without waiting for a release.
    options: dict[str, str] = field(default_factory=dict)

    statement_timeout_seconds: int = DEFAULT_STATEMENT_TIMEOUT_SECONDS
    max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES

    # Optional hardening: when set, a query may only touch these tables, whatever Qlar
    # asks for. This list lives on-premise precisely so that a compromise of Qlar cannot
    # widen it. Names are matched case-insensitively, schema-qualified where the database
    # has schemas.
    table_allowlist: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Settings:
    base_url: str
    wrapper_name: str
    enrollment_code: str | None
    key_file: Path
    state_file: Path
    audit_log_file: Path | None
    poll_timeout_seconds: int
    max_concurrent_queries: int
    verify_tls: bool
    database: DatabaseSettings


@dataclass
class EnrollmentState:
    """What the wrapper learned at enrolment, persisted across restarts."""

    wrapper_id: str
    qlar_public_key_pem: str
    enrolled_at: str
    base_url: str

    @classmethod
    def load(cls, path: Path) -> EnrollmentState | None:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            wrapper_id=data["wrapperId"],
            qlar_public_key_pem=data["qlarPublicKeyPem"],
            enrolled_at=data["enrolledAt"],
            base_url=data["baseUrl"],
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "wrapperId": self.wrapper_id,
                    "qlarPublicKeyPem": self.qlar_public_key_pem,
                    "enrolledAt": self.enrolled_at,
                    "baseUrl": self.base_url,
                },
                indent=2,
            ),
            encoding="utf-8",
        )


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from error


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _collect_db_options() -> dict[str, str]:
    """Every `DB_OPT_<NAME>` variable becomes a lower-cased driver option."""
    prefix = "DB_OPT_"
    return {
        key[len(prefix) :].lower(): value
        for key, value in os.environ.items()
        if key.startswith(prefix) and value != ""
    }


def load_settings(env_file: Path | None = None) -> Settings:
    """Builds the settings, raising ConfigError with an actionable message on any gap."""
    if env_file is not None:
        load_dotenv(env_file)

    base_url = os.environ.get("QLAR_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        raise ConfigError("QLAR_BASE_URL is required (e.g. https://plugins.qlar.ai/api/db-wrapper)")
    if not base_url.startswith("https://") and "localhost" not in base_url and "127.0.0.1" not in base_url:
        raise ConfigError("QLAR_BASE_URL must use https:// outside local testing")

    provider = os.environ.get("DB_PROVIDER", "").strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise ConfigError(
            f"DB_PROVIDER must be one of {', '.join(SUPPORTED_PROVIDERS)}, got {provider or '(unset)'}"
        )

    missing = [name for name in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD") if not os.environ.get(name)]
    if missing:
        raise ConfigError(f"missing required database settings: {', '.join(missing)}")

    port_raw = os.environ.get("DB_PORT", "").strip()
    allowlist = {
        name.strip().lower()
        for name in os.environ.get("TABLE_ALLOWLIST", "").split(",")
        if name.strip()
    }

    database = DatabaseSettings(
        provider=provider,
        host=os.environ["DB_HOST"].strip(),
        port=int(port_raw) if port_raw else None,
        database=os.environ["DB_NAME"].strip(),
        user=os.environ["DB_USER"].strip(),
        password=os.environ["DB_PASSWORD"],
        options=_collect_db_options(),
        statement_timeout_seconds=_env_int("DB_STATEMENT_TIMEOUT_SECONDS", DEFAULT_STATEMENT_TIMEOUT_SECONDS),
        max_result_bytes=_env_int("MAX_RESULT_BYTES", DEFAULT_MAX_RESULT_BYTES),
        table_allowlist=frozenset(allowlist),
    )

    audit_raw = os.environ.get("AUDIT_LOG_FILE", "./audit/queries.jsonl").strip()

    return Settings(
        base_url=base_url,
        wrapper_name=os.environ.get("QLAR_WRAPPER_NAME", "").strip() or _default_wrapper_name(),
        enrollment_code=(os.environ.get("QLAR_ENROLLMENT_CODE") or "").strip() or None,
        key_file=Path(os.environ.get("WRAPPER_KEY_FILE", "./wrapper-key.pem")).expanduser(),
        state_file=Path(os.environ.get("WRAPPER_STATE_FILE", "./wrapper-state.json")).expanduser(),
        audit_log_file=Path(audit_raw).expanduser() if audit_raw else None,
        poll_timeout_seconds=_env_int("POLL_TIMEOUT_SECONDS", DEFAULT_POLL_TIMEOUT_SECONDS),
        max_concurrent_queries=_env_int("MAX_CONCURRENT_QUERIES", DEFAULT_MAX_CONCURRENT_QUERIES),
        # Only ever disabled for testing against a local Qlar with a self-signed cert;
        # the flag is named so that it reads as a mistake in a production .env.
        verify_tls=_env_bool("QLAR_INSECURE_SKIP_TLS_VERIFY", False) is False,
        database=database,
    )


def _default_wrapper_name() -> str:
    import socket

    return f"{socket.gethostname()} wrapper"
