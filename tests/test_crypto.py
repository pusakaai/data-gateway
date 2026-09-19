"""Signature and key-handling behaviour.

These cover the properties the protocol actually relies on: a signature that covers the
whole request, a job signature that fails the moment anything in the job changes, and a
fingerprint stable enough for a human to compare against a screen.
"""

from __future__ import annotations

import json

import pytest

from qlar_db_wrapper import crypto


@pytest.fixture
def key():
    return crypto.generate_private_key()


class TestRequestSignature:
    def test_round_trip(self, key):
        message = crypto.canonical_request("POST", "/jobs/poll", "1700000000", "abc", b'{"a":1}')
        signature = crypto.sign(key, message)
        assert crypto.verify(key.public_key(), message, signature) is True

    @pytest.mark.parametrize(
        "method,path,timestamp,nonce,body",
        [
            ("GET", "/jobs/poll", "1700000000", "abc", b'{"a":1}'),
            ("POST", "/jobs/result", "1700000000", "abc", b'{"a":1}'),
            ("POST", "/jobs/poll", "1700000001", "abc", b'{"a":1}'),
            ("POST", "/jobs/poll", "1700000000", "xyz", b'{"a":1}'),
            ("POST", "/jobs/poll", "1700000000", "abc", b'{"a":2}'),
        ],
    )
    def test_every_field_is_covered(self, key, method, path, timestamp, nonce, body):
        # Change any one component and the signature must stop verifying — otherwise that
        # component could be tampered with in transit.
        original = crypto.canonical_request("POST", "/jobs/poll", "1700000000", "abc", b'{"a":1}')
        signature = crypto.sign(key, original)

        tampered = crypto.canonical_request(method, path, timestamp, nonce, body)
        assert crypto.verify(key.public_key(), tampered, signature) is False

    def test_another_key_cannot_verify(self, key):
        message = crypto.canonical_request("POST", "/jobs/poll", "1700000000", "abc", b"{}")
        signature = crypto.sign(key, message)
        assert crypto.verify(crypto.generate_private_key().public_key(), message, signature) is False

    def test_garbage_signature_returns_false_rather_than_raising(self, key):
        message = crypto.canonical_request("POST", "/x", "1", "n", b"")
        assert crypto.verify(key.public_key(), message, "not-base64!!") is False
        assert crypto.verify(key.public_key(), message, "") is False


class TestJobSignature:
    def _signed_job(self, key) -> dict:
        job = {
            "jobId": "job-1",
            "type": "execute_query",
            "sql": "SELECT 1",
            "maxRows": 100,
            "protocol": 1,
        }
        job["signature"] = crypto.sign(key, crypto.canonical_job_bytes(job))
        return job

    def test_valid_job_verifies(self, key):
        public_pem = crypto.public_key_pem(key)
        assert crypto.verify_job(public_pem, self._signed_job(key)) is True

    def test_altered_sql_fails(self, key):
        public_pem = crypto.public_key_pem(key)
        job = self._signed_job(key)
        # The attack this defends against: a proxy inside the customer's network that
        # terminates TLS and rewrites the statement on its way in.
        job["sql"] = "SELECT * FROM salaries"
        assert crypto.verify_job(public_pem, job) is False

    def test_added_field_fails(self, key):
        public_pem = crypto.public_key_pem(key)
        job = self._signed_job(key)
        job["maxRows"] = 999999
        assert crypto.verify_job(public_pem, job) is False

    def test_missing_signature_fails(self, key):
        public_pem = crypto.public_key_pem(key)
        job = self._signed_job(key)
        del job["signature"]
        assert crypto.verify_job(public_pem, job) is False

    def test_key_order_does_not_matter(self, key):
        # The canonical form sorts keys, so a job that arrives with its fields in a
        # different order still verifies — otherwise any JSON library change would break
        # every deployed wrapper.
        public_pem = crypto.public_key_pem(key)
        job = self._signed_job(key)
        reordered = json.loads(json.dumps(dict(reversed(list(job.items())))))
        assert crypto.verify_job(public_pem, reordered) is True


class TestFingerprint:
    def test_is_stable_and_grouped(self, key):
        public_pem = crypto.public_key_pem(key)
        value = crypto.fingerprint(public_pem)

        assert value == crypto.fingerprint(public_pem)
        assert len(value.split(":")) == 32  # SHA-256 as 32 byte-pairs
        assert value == value.upper()

    def test_differs_between_keys(self, key):
        other = crypto.generate_private_key()
        assert crypto.fingerprint(crypto.public_key_pem(key)) != crypto.fingerprint(
            crypto.public_key_pem(other)
        )


class TestKeyFile:
    def test_is_created_once_and_reused(self, tmp_path):
        path = tmp_path / "wrapper-key.pem"

        first, created = crypto.load_or_create_private_key(path)
        assert created is True

        second, created_again = crypto.load_or_create_private_key(path)
        assert created_again is False
        # Identity must survive a restart: a new key would mean a new fingerprint and a
        # wrapper that silently needs re-approval.
        assert crypto.public_key_pem(first) == crypto.public_key_pem(second)

    def test_is_written_with_owner_only_permissions(self, tmp_path):
        import os
        import stat

        if os.name == "nt":
            pytest.skip("POSIX permission bits are not meaningful on Windows")

        path = tmp_path / "wrapper-key.pem"
        crypto.load_or_create_private_key(path)
        mode = path.stat().st_mode
        assert not mode & (stat.S_IRWXG | stat.S_IRWXO)
