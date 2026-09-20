# Qlar Data Gateway
#
# Two stages so the runtime image carries no compilers and no build caches. The result is
# a process that makes outbound HTTPS calls and nothing else — note the deliberate absence
# of any EXPOSE line.

FROM python:3.12-slim AS builder

WORKDIR /build

# Build deps for the database drivers that still compile something (pymssql needs FreeTDS
# headers on platforms without a wheel). Confined to this stage.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc g++ freetds-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN python -m venv /venv \
    && /venv/bin/pip install --no-cache-dir --upgrade pip \
    && /venv/bin/pip install --no-cache-dir ".[all]"


FROM python:3.12-slim

# FreeTDS runtime for SQL Server. Oracle needs nothing here: python-oracledb runs in thin
# mode, which is pure Python and requires no Instant Client and no libaio.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libsybdb5 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --create-home --home-dir /home/qlar qlar

COPY --from=builder /venv /venv
ENV PATH="/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Identity and enrolment state live on a mounted volume: without this, replacing the
    # container would generate a new key pair, change the fingerprint, and silently require
    # the operator to enrol and be approved all over again.
    GATEWAY_KEY_FILE=/state/gateway-key.pem \
    GATEWAY_STATE_FILE=/state/gateway-state.json \
    AUDIT_LOG_FILE=/state/audit/queries.jsonl

RUN mkdir -p /state && chown -R qlar:qlar /state
VOLUME ["/state"]

USER qlar
WORKDIR /home/qlar

# No EXPOSE, no port published, no inbound listener. That is the product.
ENTRYPOINT ["qlar-gateway"]
CMD ["run"]
