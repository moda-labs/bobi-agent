# syntax=docker/dockerfile:1
#
# modastack instance image (containerized-8 / #338).
#
# One image, every tenant. Identity lives entirely in the mounted volume
# (project root + $HOME) and env vars — see docs/design/CONTAINERIZED_INSTANCES.md
# §2 (the instance contract). Nothing reaches in; the manager reaches out to the
# event server over WSS only.
#
# Design-mandated properties (CONTAINERIZED_INSTANCES.md §5, §6.1, §10 C8):
#   * Runs the agent as a NON-ROOT user. Claude Code refuses bypassPermissions
#     as root unless IS_SANDBOX=1; we sidestep that by dropping privileges to
#     `modastack` before exec'ing the manager.
#   * No Node.js. The `claude` CLI is the native standalone binary (no npm),
#     and the local event server (Node) is never started in deployed instances
#     (a remote event_server_url is configured — C6).
#   * fastembed model baked into the image at build (cold-start speed; immutable).
#   * Pinned `claude` CLI; auto-updater disabled so the image version is frozen.
#   * `modastack start --foreground` as the entrypoint (C2); no tini — Fly's
#     init is PID 1 (tini-on-Fly is a known boot-failure trigger).
#   * Both Anthropic auth modes via MODASTACK_AUTH=api_key|subscription (§6.1).
#
# Build:
#   docker build -t modastack:dev .
#   docker build -t modastack:dev --build-arg CLAUDE_VERSION=2.1.89 .   # pin exact
#
# Run (api_key mode):
#   docker run --rm -v modastack-a:/data \
#     -e MODASTACK_TEAM=eng-team -e MODASTACK_EVENT_SERVER=https://... \
#     -e ANTHROPIC_API_KEY=sk-ant-... -e SLACK_BOT_TOKEN=... -e GITHUB_TOKEN=... \
#     modastack:dev

#####################################################################
# Stage 1 — builder: build the wheel and install it into a venv.     #
# Build tools live here only; the runtime image never sees them.     #
#####################################################################
FROM python:3.11-slim AS builder

# apsw / native deps may need a compiler if no manylinux wheel is published.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir build

WORKDIR /src
COPY . .

# pyproject builds the wheel FROM the sdist, so the sdist must carry the
# force-included templates + event-server (it does — see pyproject sdist include).
RUN python -m build --wheel --outdir /dist

# Self-contained venv with modastack + the kb extra (fastembed, sqlite-vec).
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir "$(ls /dist/*.whl)[kb]"

#####################################################################
# Stage 2 — runtime: slim image, no build tools, no Node.            #
#####################################################################
FROM python:3.11-slim AS runtime

# Channel or exact version for the native `claude` installer. Default to the
# `stable` channel (one week behind latest, skips major regressions); pass an
# exact version (e.g. 2.1.89) for fully reproducible production builds.
ARG CLAUDE_VERSION=stable
ARG MODASTACK_UID=10001

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:/home/modastack/.local/bin:${PATH}" \
    HF_HOME=/opt/modastack/models \
    DISABLE_AUTOUPDATER=1 \
    DATA_DIR=/data \
    MODASTACK_PROJECT=/data/project \
    MODASTACK_HOME=/data/home

# Runtime packages only:
#   curl, ca-certificates — fetch the claude installer; TLS
#   gosu                  — drop privileges from root setup to the modastack user
#   git                   — agents clone/operate on repos
# NB: no tini. Fly Machines (the deploy target) inject their own PID-1 init that
# reaps zombies + forwards signals, and layering tini on top is a documented
# cause of "failed to spawn ... No such file or directory" boot failures there.
# For other container runtimes, run with an init (e.g. `docker run --init`).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl ca-certificates gosu git \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user (see header: bypassPermissions-as-root guard).
RUN useradd --create-home --uid ${MODASTACK_UID} --shell /bin/bash modastack

# Bring in the prebuilt venv (modastack + deps). Root-owned, world-readable.
COPY --from=builder /opt/venv /opt/venv

# Install the pinned native `claude` CLI (no Node) as the modastack user so it
# lands in that user's ~/.local/bin, which is on PATH above.
USER modastack
RUN curl -fsSL https://claude.ai/install.sh | bash -s -- "${CLAUDE_VERSION}" \
    && /home/modastack/.local/bin/claude --version
USER root

# Bake the fastembed embedding model into the image (cold-start speed; immutable).
# HF_HOME points here at both build and run, so this is a cache hit at runtime.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='sentence-transformers/all-MiniLM-L6-v2')" \
    && chmod -R a+rX /opt/modastack/models

COPY docker/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY docker/healthcheck.sh /usr/local/bin/healthcheck.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh /usr/local/bin/healthcheck.sh

# Persistent state: project root + $HOME both live on this volume (§2).
VOLUME ["/data"]
# WORKDIR must NOT be under /data: a volume mounted there shadows the
# build-time dir, so the container's cwd ceases to exist at runtime and the
# platform init (e.g. Fly Machines) fails to spawn the entrypoint with ENOENT.
# The entrypoint cd's into ${MODASTACK_PROJECT} itself after creating it.
WORKDIR /

# Liveness: read the manager's health port from the volume and probe /health.
# start-period is generous — first boot installs a team and warms a session.
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
    CMD ["/usr/local/bin/healthcheck.sh"]

# The entrypoint is PID 1 (under Fly's injected init): it does root-only volume
# setup, then `exec gosu`s to the modastack user running `modastack start
# --foreground`, so SIGTERM reaches the manager directly for graceful shutdown.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
