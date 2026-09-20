# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The **protocol version** is tracked separately from the release version — see
[docs/PROTOCOL.md](docs/PROTOCOL.md §7). A release that does not change the wire format
does not change the protocol version.

## [Unreleased]

## [0.1.1] - 2026-09-20

The first published release. 0.1.0 was tagged in the changelog but never cut, so the
artifacts and images below are the first that exist.

### Added

- **Setup prompts on first run.** `run`, `test-db` and `enroll` started without usable
  configuration now ask for the database details — provider, host, port, database, user,
  password, and the Qlar endpoint — and write them to `.env` with mode 0600, so installing
  the wrapper and starting it is the whole job. A connection URL pasted at the host
  question fills in the rest. Only when there is a terminal to ask on: a container without
  `-it`, a systemd unit and CI get exactly the configuration error they got before.
- **`--init`** on those three commands, which asks again over a complete `.env` — for a
  rotated password or a moved database. The file is rewritten in place, so hand-added
  settings and comments survive.
- **A test query at every start**, not only in `test-db`. A wrapper that polls happily
  while every query fails looks healthy in the CMS, which is the most confusing way for an
  installation to be broken. `run` reports the database and its server version on line one,
  and keeps running if it is merely down for the moment.

### Changed

- `.env` values may be quoted, and a quoted value now loses exactly one surrounding pair
  rather than every quote character — so a password with a leading space, a `#` or a
  trailing quote survives the round trip.
- **`QLAR_BASE_URL` no longer has a default.** The endpoint differs per Qlar deployment and
  the CMS wrapper panel prints the right one, so the setup prompts ask for it instead of
  offering a guess. `https://plugins.qlar.ai/api/db-wrapper`, which appeared throughout the
  documentation as though it were known, serves a website and never was an API endpoint.

### Fixed

- **A wrong `QLAR_BASE_URL` is now reported as a wrong URL.** Web servers answer a signed
  POST cheerfully: a static site returns `405` with an HTML page, a host where the feature
  is not deployed returns an empty `404`. Enrolment turned the second into "Qlar rejected
  the enrolment code — generate a fresh one", sending operators back to the CMS for codes
  that failed identically, and `run` logged the first as `Qlar rejected the poll` every few
  seconds forever. An HTML body at any status, or a `404` carrying no JSON, is now
  `QlarNotAnEndpoint` and says which URL answered, what it answered with, and where the
  right value is written down.

## [0.1.0] - 2026-09-19

Never published: no tag, no image, no release artifacts. Kept here because it is what the
code was before 0.1.1, and because deleting history to tidy it up is its own kind of lie.
Protocol version 1.

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

[Unreleased]: https://github.com/pusakaai/db-wrapper/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/pusakaai/db-wrapper/releases/tag/v0.1.1
