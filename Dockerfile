# FraudShield container: one CLI, one SQLite file.
#
# The log lives on the /data volume so it survives the container. The entrypoint
# pins --log to it; a later --log in the command wins if you need another path
# (argparse keeps the last value), so `docker run <img> --log /tmp/x.db demo`
# still works without touching the entrypoint.
#
# Build:   docker build -t fraudshield .
# Demo:    docker run --rm -v fraudshield-data:/data fraudshield demo
# Query:   docker run --rm -v fraudshield-data:/data fraudshield --json cases
# Console: docker run --rm -it -v fraudshield-data:/data fraudshield dashboard

FROM python:3.14-slim AS builder

# Wheels only — nothing here should ever need a compiler. If a dependency stops
# shipping a manylinux wheel, this line failing is the early warning.
COPY requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --only-binary=:all: \
       -r /tmp/requirements.txt


FROM python:3.14-slim

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FRAUDSHIELD_LOG_LEVEL=INFO

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY pyproject.toml LICENSE README.md ./
COPY sentinel/ sentinel/

# Non-root. /data holds the event log; /app/output holds the demo's report files.
RUN groupadd --gid 10001 sentinel \
    && useradd --uid 10001 --gid sentinel --no-create-home sentinel \
    && mkdir -p /data /app/output \
    && chown -R sentinel:sentinel /data /app/output
USER sentinel

VOLUME /data

# Healthy once a log exists on the volume and is contiguous and self-describing.
# start-period covers the initial build/demo, which has to write the log first.
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD ["python", "-m", "sentinel", "--log", "/data/events.db", "health"]

ENTRYPOINT ["python", "-m", "sentinel", "--log", "/data/events.db"]
CMD ["demo", "--cards", "300", "--days", "30"]
