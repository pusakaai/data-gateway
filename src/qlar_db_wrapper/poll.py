"""The run loop: ask Qlar for work, do it, send the answer back.

Outbound only. The wrapper opens a long-poll request that Qlar holds for up to ~25 seconds
and answers either with a job or with `204 No Content`; either way the wrapper immediately
asks again. Nothing listens on a port, nothing needs a certificate, and nothing has to be
reachable from the internet.

Because there is no session, a Qlar restart is not an outage here: the hanging poll fails,
the loop backs off for a second or two and asks again. Enrolment and approval are stored
server-side and survive, so no human has to do anything.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC
from typing import Any

from . import PROTOCOL_VERSION, __version__
from .audit import AuditLog
from .client import QlarClient, QlarNotAnEndpoint, QlarRejected, QlarUnreachable
from .config import EnrollmentState, Settings
from .crypto import load_or_create_private_key, verify_job
from .executor import execute, test_connection

logger = logging.getLogger("qlar_db_wrapper.poll")

POLL_PATH = "/jobs/poll"
RESULT_PATH = "/jobs/{job_id}/result"

MIN_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 15.0

# How many times a finished result is re-sent if the network drops on the way back.
# Posting a result is idempotent on Qlar's side (keyed by job id), so a retry is safe and
# is much better than throwing away work the database already paid for.
RESULT_ATTEMPTS = 4


class Revoked(Exception):
    """Qlar says this wrapper is revoked. The loop stops; a human must re-enrol it."""


class PollLoop:
    def __init__(self, settings: Settings, state: EnrollmentState) -> None:
        self.settings = settings
        self.state = state
        private_key, _ = load_or_create_private_key(settings.key_file)
        self.client = QlarClient(
            base_url=settings.base_url,
            private_key=private_key,
            wrapper_id=state.wrapper_id,
            verify_tls=settings.verify_tls,
        )
        self.audit = AuditLog(settings.audit_log_file)
        self._stopping = threading.Event()
        self._in_flight = 0
        self._in_flight_lock = threading.Lock()
        # Reported on every poll so the CMS can show the data source as reachable or not
        # without waiting for someone to ask a question that fails.
        self._db_status = "unknown"
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, settings.max_concurrent_queries),
            thread_name_prefix="qlar-job",
        )

    def stop(self) -> None:
        self._stopping.set()

    def run_forever(self) -> None:
        """Polls until stopped or revoked."""
        backoff = MIN_BACKOFF_SECONDS
        logger.info(
            "wrapper %s starting, polling %s (protocol %d, version %s)",
            self.state.wrapper_id, self.settings.base_url, PROTOCOL_VERSION, __version__,
        )

        while not self._stopping.is_set():
            # Saturated: every worker is busy, so asking for more work would only let jobs
            # queue up locally and time out. Wait for a slot instead — Qlar will hold the
            # job and hand it to the next poll.
            if self._current_in_flight() >= self.settings.max_concurrent_queries:
                time.sleep(0.25)
                continue

            try:
                job = self._poll_once()
                backoff = MIN_BACKOFF_SECONDS
                if job is not None:
                    self._dispatch(job)
            except Revoked:
                logger.error(
                    "this wrapper has been revoked in the Qlar CMS; stopping. "
                    "Delete %s and enrol again to reconnect.", self.settings.state_file,
                )
                break
            except QlarUnreachable as error:
                logger.warning("Qlar unreachable (%s); retrying in %.1fs", error, backoff)
                self._sleep_with_jitter(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
            except QlarNotAnEndpoint as wrong_address:
                # Retried like any other failure — the address may be a proxy having a bad
                # day — but named for what it is, because "rejected the poll" every 15
                # seconds is not a clue anyone can act on.
                logger.error(
                    "%s. That is not Qlar's API: check QLAR_BASE_URL (%s), which must end in "
                    "/api/db-wrapper. Retrying in %.1fs",
                    wrong_address, self.settings.base_url, backoff,
                )
                self._sleep_with_jitter(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
            except QlarRejected as rejection:
                logger.error("Qlar rejected the poll: %s", rejection)
                self._sleep_with_jitter(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

        self._pool.shutdown(wait=True, cancel_futures=False)
        logger.info("wrapper stopped")

    def _poll_once(self) -> dict[str, Any] | None:
        payload = {
            "wrapperId": self.state.wrapper_id,
            "version": __version__,
            "protocol": PROTOCOL_VERSION,
            "dbStatus": self._db_status,
            "maxWaitSeconds": self.settings.poll_timeout_seconds,
        }

        try:
            status, body = self.client.post(
                POLL_PATH,
                payload,
                # Comfortably longer than the server's hold, so a normal empty poll is not
                # mistaken for a network failure and does not trigger backoff.
                timeout=self.settings.poll_timeout_seconds + 15,
            )
        except QlarRejected as rejection:
            if rejection.status == 403 and str(rejection.body.get("reason", "")).lower() == "revoked":
                raise Revoked from rejection
            raise

        if status == 204 or not body:
            return None

        job = body.get("job") if "job" in body else body
        if not isinstance(job, dict) or not job.get("jobId"):
            return None

        # The job is signed by Qlar itself. Verifying it here means a compromised proxy at
        # the customer's own edge cannot inject SQL, even though it terminates the TLS.
        if not verify_job(self.state.qlar_public_key_pem, job):
            logger.error("job %s failed signature verification and was discarded", job.get("jobId"))
            return None

        if int(job.get("protocol", PROTOCOL_VERSION)) > PROTOCOL_VERSION:
            logger.error(
                "job %s needs protocol %s but this wrapper speaks %d - upgrade the wrapper",
                job.get("jobId"), job.get("protocol"), PROTOCOL_VERSION,
            )
            self._send_result(
                str(job["jobId"]),
                {
                    "status": "error",
                    "error": {
                        "category": "rejected",
                        "driverCode": None,
                        "messageText": f"wrapper speaks protocol {PROTOCOL_VERSION}; job requires "
                                       f"{job.get('protocol')}. Upgrade the on-premise wrapper.",
                        "hint": None,
                        "position": None,
                    },
                    "durationMs": 0,
                },
            )
            return None

        return job

    def _dispatch(self, job: dict[str, Any]) -> None:
        with self._in_flight_lock:
            self._in_flight += 1
        self._pool.submit(self._run_job, job)

    def _run_job(self, job: dict[str, Any]) -> None:
        job_id = str(job.get("jobId"))
        job_type = str(job.get("type", "execute_query"))
        sql = str(job.get("sql") or "")

        try:
            expires_at = job.get("expiresAt")
            if _is_expired(expires_at):
                # Qlar has already given up waiting; running it would cost the customer's
                # database for an answer nobody will read.
                logger.info("job %s expired before it started; skipping", job_id)
                self._send_result(job_id, {
                    "status": "error",
                    "error": {
                        "category": "expired", "driverCode": None,
                        "messageText": "job expired before execution", "hint": None, "position": None,
                    },
                    "durationMs": 0,
                })
                return

            if job_type == "test_connection":
                result = test_connection(self.settings.database)
            else:
                result = execute(
                    sql,
                    self.settings.database,
                    max_rows=int(job.get("maxRows") or 1000),
                    catalog_only=(job_type == "introspect"),
                )

            payload = result.to_payload()
            self._db_status = "ok" if result.status == "ok" or (
                result.error or {}
            ).get("category") not in ("connection",) else "unreachable"

            self.audit.record(
                job_id=job_id,
                job_type=job_type,
                sql=sql,
                outcome=result.status,
                row_count=result.row_count,
                duration_ms=result.duration_ms,
                agent_id=job.get("agentId"),
                user_id=job.get("userId"),
                conversation_id=job.get("conversationId"),
                error=result.error,
            )

            self._send_result(job_id, payload)

        except Exception as error:  # noqa: BLE001 - a worker must never die silently
            logger.exception("job %s failed unexpectedly", job_id)
            self._send_result(job_id, {
                "status": "error",
                "error": {
                    "category": "sql_error", "driverCode": None,
                    "messageText": f"{type(error).__name__}: {error}", "hint": None, "position": None,
                },
                "durationMs": 0,
            })
        finally:
            with self._in_flight_lock:
                self._in_flight -= 1

    def _send_result(self, job_id: str, payload: dict[str, Any]) -> None:
        path = RESULT_PATH.format(job_id=job_id)
        delay = 0.5

        for attempt in range(1, RESULT_ATTEMPTS + 1):
            try:
                self.client.post(path, payload, timeout=30.0)
                return
            except QlarRejected as rejection:
                # Qlar answered and said no: the job is gone or already answered. Retrying
                # would not change that.
                logger.warning("result for job %s refused: %s", job_id, rejection)
                return
            except QlarUnreachable as error:
                if attempt == RESULT_ATTEMPTS:
                    logger.error("giving up sending result for job %s: %s", job_id, error)
                    return
                self._sleep_with_jitter(delay)
                delay = min(delay * 2, MAX_BACKOFF_SECONDS)

    def _current_in_flight(self) -> int:
        with self._in_flight_lock:
            return self._in_flight

    def _sleep_with_jitter(self, seconds: float) -> None:
        # Jitter so that a fleet of wrappers reconnecting after a Qlar deployment does not
        # arrive as one synchronised wave.
        self._stopping.wait(seconds * (0.7 + random.random() * 0.6))  # noqa: S311


def _is_expired(expires_at: Any) -> bool:
    if not expires_at:
        return False
    from datetime import datetime

    try:
        deadline = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return datetime.now(UTC) > deadline
