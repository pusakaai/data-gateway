# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The **protocol version** is tracked separately from the release version — see
[docs/PROTOCOL.md](docs/PROTOCOL.md §7). A release that does not change the wire format
does not change the protocol version.

## [Unreleased]

## [0.1.0] - 2026-09-19

First public release. Protocol version 1.

### Added

- **Outbound-only operation.** The wrapper long-polls Qlar for jobs over HTTPS and never
  listens on a port, so no inbound firewall rule, public DNS name or TLS certificate is
  needed on the customer's side.
- **Database credentials stay on-premise.** They live only in the operator's `.env`; Qlar
  never receives or stores them.
- **Mutual payload signatures** (ECDSA P-256 / SHA-256). The wrapper signs every request
  over method, path, timestamp, nonce and a body hash; Qlar signs every job, which the
  wrapper verifies against a public key pinned at enrolment — so a TLS-terminating proxy
  inside the customer's network cannot inject or rewrite SQL.
- **Enrolment with a one-time code**, plus a human fingerprint approval step in the CMS.
  The key pair is generated on the customer's machine and the private key never leaves it.
- **SQL guard**: exactly one parsed `SELECT`/`WITH` per job, dialect-aware refusal of file,
  shell and network escape hatches, and an optional table allowlist that lives in the
  customer's own config file.
- **`READ ONLY` transactions** and a server-side statement timeout on every job.
- **Four providers**: PostgreSQL, MySQL/MariaDB, SQL Server and Oracle, each installable as
  a separate extra so an installation only pulls the driver it uses.
- **Columnar result encoding** with lossless type rules — decimals and integers beyond
  2^53 as strings, ISO-8601 timestamps, `base64:`-prefixed binary — plus row and byte
  ceilings.
- **Error envelope carrying the raw driver code** (SQLSTATE or error number), so Qlar can
  classify failures by code rather than by message text.
- **Local JSONL audit log** recording every statement with the agent, user and
  conversation that caused it. It never leaves the customer's premises.
- `test-db`, `enroll`, `fingerprint`, `run` and `version` commands, a Docker image, and
  systemd/Kubernetes deployment guides.

[Unreleased]: https://github.com/pusakaai/db-wrapper/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/pusakaai/db-wrapper/releases/tag/v0.1.0
