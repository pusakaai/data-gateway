"""Signed HTTP transport to Qlar.

Every call is a plain HTTPS POST with four extra headers: who is calling, when, a nonce,
and a signature over a canonical string built from all of it plus a hash of the body.

One subtlety worth stating, because getting it wrong produces signature failures that are
miserable to debug: **the signed path is the endpoint path relative to the configured base
URL** (`/jobs/poll`), not the absolute path of the request (`/api/db-wrapper/jobs/poll`).
Qlar sits behind an API gateway that may rewrite the prefix, and a signature that broke
because of a gateway rule would look, from inside the customer's network, exactly like a
wrong key.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey

from . import PROTOCOL_VERSION
from .crypto import (
    NONCE_HEADER,
    PROTOCOL_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    WRAPPER_ID_HEADER,
    canonical_request,
    new_nonce,
    sign,
)


class QlarUnreachable(Exception):
    """Qlar could not be reached — a network problem, not a rejection."""


class QlarRejected(Exception):
    """Qlar answered with a refusal. `status` carries the HTTP code."""

    def __init__(self, status: int, message: str, body: dict[str, Any] | None = None) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.body = body or {}


@dataclass
class QlarClient:
    base_url: str
    private_key: EllipticCurvePrivateKey
    wrapper_id: str | None
    verify_tls: bool = True

    def post(
        self, path: str, payload: dict[str, Any], *, timeout: float = 30.0
    ) -> tuple[int, dict[str, Any]]:
        """POSTs a signed JSON request, returning the status and decoded body.

        Raises QlarUnreachable for transport failures and QlarRejected for 4xx/5xx, so a
        caller can tell "the network is down" from "Qlar says no" — they need very
        different handling, and only one of them should stop the wrapper.
        """
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        timestamp = str(int(time.time()))
        nonce = new_nonce()

        message = canonical_request("POST", path, timestamp, nonce, body)
        headers = {
            "Content-Type": "application/json",
            PROTOCOL_HEADER: str(PROTOCOL_VERSION),
            TIMESTAMP_HEADER: timestamp,
            NONCE_HEADER: nonce,
            SIGNATURE_HEADER: sign(self.private_key, message),
        }
        if self.wrapper_id:
            headers[WRAPPER_ID_HEADER] = self.wrapper_id

        url = f"{self.base_url.rstrip('/')}{path}"

        try:
            with httpx.Client(verify=self.verify_tls, timeout=timeout, follow_redirects=False) as client:
                response = client.post(url, content=body, headers=headers)
        except httpx.HTTPError as error:
            raise QlarUnreachable(str(error)) from error

        decoded: dict[str, Any] = {}
        if response.content:
            try:
                parsed = response.json()
                decoded = parsed if isinstance(parsed, dict) else {"value": parsed}
            except ValueError:
                decoded = {"raw": response.text[:500]}

        if response.status_code >= 400:
            raise QlarRejected(
                response.status_code,
                str(decoded.get("message") or decoded.get("title") or response.reason_phrase),
                decoded,
            )

        return response.status_code, decoded
