# HoursX server image.
#
# One image, two roles — the command selects it:
#   docker run hoursx serve    → API + WebSocket gateway
#   docker run hoursx worker   → background runs, ingestion, schedules
#
# Multi-stage so build tooling never reaches the runtime layer, and the final
# image runs as an unprivileged user with no package manager present.

# ---------------------------------------------------------------- build stage
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# Dependency metadata first: this layer is cached across source-only changes.
COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
    && /opt/venv/bin/pip install ".[postgres]"

# -------------------------------------------------------------- runtime stage
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    HOURSX_HOST=0.0.0.0 \
    HOURSX_PORT=8400 \
    HOURSX_WORKSPACE_ROOT=/var/lib/hoursx/workspaces \
    HOURSX_PLUGIN_DIR=/var/lib/hoursx/plugins

# curl for the healthcheck; git because agents use git tools in their sandbox.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl git tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

# Fixed UID/GID so mounted volumes have predictable ownership across hosts.
RUN groupadd --gid 10001 hoursx \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin hoursx \
    && mkdir -p /var/lib/hoursx/workspaces /var/lib/hoursx/plugins \
    && chown -R hoursx:hoursx /var/lib/hoursx

WORKDIR /var/lib/hoursx
USER hoursx

EXPOSE 8400

# tini reaps the subprocesses agent shell tools spawn; without an init, killed
# tool processes would accumulate as zombies in a long-lived worker.
ENTRYPOINT ["/usr/bin/tini", "--", "hoursx"]
CMD ["serve"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${HOURSX_PORT}/healthz" || exit 1

LABEL org.opencontainers.image.title="HoursX Server" \
      org.opencontainers.image.description="Enterprise autonomous AI agent platform" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/tedo001/hoursX-"
