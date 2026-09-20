# Installation

Three ways to run the wrapper. Docker is the least intrusive on a server you do not
control the Python version of; systemd is the most conventional for a long-lived Linux
service; Kubernetes if that is where your workloads already live.

Whichever you choose, the sequence is the same: configure → check the database → enrol →
have it approved → run.

The first step can answer itself. Started with no usable configuration and a terminal to
ask on, the wrapper collects the database details, writes them to `.env` with mode 0600,
and runs a test query — so "configure" and "check the database" are one step, and there is
no `.env.example` to find first. `--init` repeats it later. Everything below also works
the way it always did, with a `.env` you wrote yourself; and without a terminal — an
unattended container, a systemd unit — nothing prompts and a missing setting is still a
configuration error on stderr.

---

## Before you start

- Outbound HTTPS (443) to your Qlar endpoint. **No inbound rule is needed** — if someone
  asks you which port to open, the answer is none.
- A database account with `SELECT` on the tables you want reachable, and nothing more.
- A place to keep two files: `wrapper-key.pem` (the wrapper's identity) and
  `wrapper-state.json`. Back the key up; losing it means enrolling and being approved
  again.

### Creating a read-only account

PostgreSQL:

```sql
CREATE ROLE qlar_readonly LOGIN PASSWORD '...';
GRANT CONNECT ON DATABASE warehouse TO qlar_readonly;
GRANT USAGE ON SCHEMA public TO qlar_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO qlar_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO qlar_readonly;
```

MySQL:

```sql
CREATE USER 'qlar_readonly'@'%' IDENTIFIED BY '...';
GRANT SELECT ON warehouse.* TO 'qlar_readonly'@'%';
```

SQL Server:

```sql
CREATE LOGIN qlar_readonly WITH PASSWORD = '...';
CREATE USER qlar_readonly FOR LOGIN qlar_readonly;
ALTER ROLE db_datareader ADD MEMBER qlar_readonly;
```

Oracle:

```sql
CREATE USER qlar_readonly IDENTIFIED BY "...";
GRANT CREATE SESSION TO qlar_readonly;
GRANT SELECT ON warehouse.shipment_order TO qlar_readonly;  -- per table, or a role
```

SQL Server has no `READ ONLY` transaction, so there the account's permissions are doing
more of the work than on the other three. Do not skip it.

---

## Option A — Docker

```bash
mkdir -p /opt/qlar-db-wrapper/state && cd /opt/qlar-db-wrapper
curl -o .env https://raw.githubusercontent.com/pusakaai/db-wrapper/main/.env.example
chmod 600 .env
# edit .env
```

Editing the file is the better fit here: `--env-file` is a Docker flag, so the host's
`.env` becomes the container's environment and the prompts have nothing to ask about. To
be asked instead, give the container a terminal with `-it` and a file it may write — the
state volume, which the image already owns:

```bash
chown -R 10001:10001 state     # the uid the image runs as
docker run --rm -it -v "$PWD/state:/state" \
  ghcr.io/pusakaai/db-wrapper:0.1.2 --env-file /state/.env test-db --init
```

That writes `state/.env`, which every later command then names the same way
(`--env-file /state/.env` *after* the image, not Docker's own `--env-file` before it).
Either way, enrol next:

```bash
docker run --rm --env-file .env -v "$PWD/state:/state" \
  ghcr.io/pusakaai/db-wrapper:0.1.2 enroll
```

`enroll` prints a fingerprint. Approve it in the Qlar CMS, then start the service:

```bash
docker run -d --name qlar-db-wrapper --restart unless-stopped \
  --env-file .env -v "$PWD/state:/state" \
  ghcr.io/pusakaai/db-wrapper:0.1.2 run
```

The image defaults `WRAPPER_KEY_FILE` and `WRAPPER_STATE_FILE` into `/state`, so mounting
that directory is what makes the identity survive a container replacement. There is no
`-p` flag anywhere on this page, and that is the point.

`docker-compose.example.yml` in the repository root is the same thing as a compose file.

---

## Option B — systemd

```bash
sudo useradd --system --home /opt/qlar-db-wrapper --shell /usr/sbin/nologin qlar
sudo mkdir -p /opt/qlar-db-wrapper && cd /opt/qlar-db-wrapper
sudo python3 -m venv venv
sudo ./venv/bin/pip install "qlar-db-wrapper[postgresql] @ \
  https://github.com/pusakaai/db-wrapper/releases/download/v0.1.2/qlar_db_wrapper-0.1.2-py3-none-any.whl"
sudo chown -R qlar:qlar /opt/qlar-db-wrapper
```

Answer the setup prompts as the service user — as that user, so the `.env` it writes is
readable by the service and by nobody else:

```bash
sudo -u qlar ./venv/bin/qlar-db-wrapper test-db --init
sudo -u qlar ./venv/bin/qlar-db-wrapper enroll
```

(Or put a hand-written `.env` in `/opt/qlar-db-wrapper`, `chmod 600`, owned by `qlar`, and
run `test-db` without the flag.) The unit itself runs unattended and never prompts: it has
no terminal, so a missing setting fails the start with a configuration error, which is what
you want from a service.

Approve the fingerprint in the CMS, then `/etc/systemd/system/qlar-db-wrapper.service`:

```ini
[Unit]
Description=Qlar DB Wrapper
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=qlar
WorkingDirectory=/opt/qlar-db-wrapper
ExecStart=/opt/qlar-db-wrapper/venv/bin/qlar-db-wrapper run
Restart=always
RestartSec=5

# The wrapper needs to read its own directory and reach the network. Nothing else.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/qlar-db-wrapper

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now qlar-db-wrapper
journalctl -u qlar-db-wrapper -f
```

---

## Option C — Kubernetes

A `Deployment` with one replica, the config in a `Secret` and the identity in a small
`PersistentVolumeClaim`:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: qlar-db-wrapper
spec:
  replicas: 1          # see the note below before raising this
  selector:
    matchLabels: { app: qlar-db-wrapper }
  template:
    metadata:
      labels: { app: qlar-db-wrapper }
    spec:
      containers:
        - name: wrapper
          image: ghcr.io/pusakaai/db-wrapper:0.1.2
          args: ["run"]
          envFrom:
            - secretRef: { name: qlar-db-wrapper-env }
          volumeMounts:
            - { name: state, mountPath: /state }
          resources:
            requests: { cpu: 50m, memory: 128Mi }
            limits:   { cpu: 500m, memory: 512Mi }
          securityContext:
            runAsNonRoot: true
            readOnlyRootFilesystem: true
            allowPrivilegeEscalation: false
      volumes:
        - name: state
          persistentVolumeClaim: { claimName: qlar-db-wrapper-state }
```

Enrol once with a `kubectl run --rm -it` pod sharing the same secret and volume, approve
the fingerprint, then scale the deployment up.

The `Secret` is the configuration here, so there is nothing for the setup prompts to write:
a pod has no terminal and will not ask. Use `qlar-db-wrapper test-db` in that same
throwaway pod to check the credentials in the `Secret` before the deployment depends on
them.

**On replicas:** more than one is supported — each polls independently and Qlar hands each
job to whichever asks first — but every replica must mount the *same* key, since the key is
the wrapper's identity. If you want genuinely independent wrappers, enrol them separately
and approve each.

---

## Verifying

```bash
qlar-db-wrapper version     # release and protocol version
qlar-db-wrapper fingerprint # compare with the CMS
tail -f audit/queries.jsonl # every statement Qlar has run
```

In the CMS the wrapper should show as online within a few seconds of starting.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `enrolment failed: Qlar rejected the enrolment code` | codes are single use and expire after 15 minutes | generate a fresh one in the CMS |
| `401` on every poll | clock skew beyond ±120 s — by far the most common cause | fix NTP on the host |
| Stays "pending approval" | nobody approved the fingerprint yet | CMS → wrapper → compare fingerprint → Approve |
| `this wrapper has been revoked` then exits | revoked in the CMS | delete `wrapper-state.json` and enrol again |
| `configuration error: DB_PROVIDER must be one of …` | typo, or the variable is unset | `postgresql`, `mysql`, `sqlserver`, `oracle` |
| Asked for the database details on every start | the `.env` it writes is not where the next start reads it — a container without the file mounted, or a different working directory | mount `.env`, or pass `--env-file /path/to/.env` |
| `--init needs a terminal to ask on` | no TTY: `docker run` without `-it`, a systemd unit, CI | add `-it`, or edit `.env` directly |
| Setup says the connection failed | the details are saved anyway | fix `.env` by hand, or `qlar-db-wrapper run --init` |
| `the postgresql driver is not installed` | installed without the extra | re-run the pip line above, with the extra in the brackets |
| `is accessible to group/other` | key file permissions | `chmod 600 wrapper-key.pem` |
| Queries fail with `table … is not in this wrapper's TABLE_ALLOWLIST` | working as designed | add the table to `TABLE_ALLOWLIST`, or clear it to allow all |
| `Qlar unreachable` repeatedly | egress firewall or proxy | allow outbound 443 to your Qlar host; the wrapper honours `HTTPS_PROXY` |
| Fingerprint changed after a redeploy | the key file was not persisted | mount the state volume; re-enrol and approve |

Still stuck: run with `LOG_LEVEL=DEBUG`, and include the version, provider and the exact
message when you open an issue. **Never paste `.env` or `wrapper-key.pem` into an issue.**
