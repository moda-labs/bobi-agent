# syntax=docker/dockerfile:1
#
# modastack instance image (containerized-8 / #338).
#
# ONE image, every tenant. Identity lives entirely in the mounted volume
# (project root + $HOME) and env vars — see docs/design/CONTAINERIZED_INSTANCES.md
# §2 (the instance contract). Nothing reaches in; the manager reaches out to the
# event server over WSS only.
#
# ONE Dockerfile, three BUILD modes (MODASTACK_BUILD build-arg) — the runtime stage
# is shared, so there's nothing to keep in sync:
#   * source (default) — build the wheel from a repo checkout (`COPY . .`). Dev +
#     the repo's own CI, so unreleased branch code is tested.
#   * pypi             — install a published `modastack==$MODASTACK_VERSION` from
#     PyPI; the build context is just this file + docker/. This is what binary-mode
#     `modastack deploy` uses, so deploying needs no checkout (DEPLOY_INTERFACE.md).
#   * wheel            — install a PREBUILT local wheel staged in dist/. The release
#     pipeline (release.yml) builds the wheel once, then bakes THAT artifact so the
#     canary smokes — and the fleet runs — the exact bytes published to PyPI.
#
# Design-mandated properties (CONTAINERIZED_INSTANCES.md §5, §6.1, §10 C8):
#   * Runs the agent as a NON-ROOT user. Claude Code refuses bypassPermissions
#     as root unless IS_SANDBOX=1; we drop privileges to `modastack` first.
#   * No Node.js. The `claude` CLI is the native standalone binary (no npm).
#   * fastembed model baked into the image at build (cold-start speed; immutable).
#   * Pinned `claude` CLI; auto-updater disabled so the image version is frozen.
#   * `modastack start --foreground` as the entrypoint (C2); no tini — Fly's
#     init is PID 1 (tini-on-Fly is a known boot-failure trigger).
#
# Build:
#   docker build -t modastack:dev .                                  # source mode
#   docker build --build-arg MODASTACK_BUILD=pypi \
#     --build-arg MODASTACK_VERSION=0.22.0 -t modastack:dev .        # pypi mode
#   docker build --build-arg MODASTACK_BUILD=wheel -t modastack:dev . # wheel (dist/*.whl)

# Which builder produces /opt/venv: `source`, `pypi`, or `wheel`. modastack deploy
# passes `pypi` (+ MODASTACK_VERSION) in binary mode; the release pipeline passes
# `wheel` (with the prebuilt artifact in dist/); a plain `docker build` defaults to
# `source` so the repo's own CI keeps building from the branch.
ARG MODASTACK_BUILD=source

#####################################################################
# Builder base — build tools live here only; runtime never sees them.#
#####################################################################
FROM python:3.11-slim AS builder-base
# apsw / native deps may need a compiler if no manylinux wheel is published.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

#####################################################################
# builder-source — build the wheel FROM the repo (dev + repo CI).    #
#####################################################################
FROM builder-base AS builder-source
RUN pip install --no-cache-dir build
WORKDIR /src
# --- deps layer (cached on pyproject only) -------------------------#
# Install the project's runtime deps + the lean kb deps into the venv,
# keyed on pyproject.toml alone: an ordinary code edit doesn't touch it, so
# this (network-heavy) layer stays cached and only the thin wheel layer below
# rebuilds. Read the dep list straight from [project.dependencies] (stdlib
# tomllib) so it never drifts from pyproject. Install fastembed/sqlite-vec
# EXPLICITLY — never the `[kb]` extra, which on some releases stale-lists
# sentence-transformers → torch + ~2 GB CUDA the CPU instance never uses.
COPY pyproject.toml ./
RUN python -m venv /opt/venv \
    && python -c "import tomllib; print(chr(10).join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > /tmp/reqs.txt \
    && /opt/venv/bin/pip install --no-cache-dir -r /tmp/reqs.txt "fastembed>=0.4" "sqlite-vec>=0.1.6"
# --- wheel layer (thin; rebuilds on any code change) ---------------#
# pyproject builds the wheel FROM the sdist, so the sdist must carry the
# force-included templates + event-server (it does — see pyproject sdist include).
# --no-deps: deps are already in the venv above, so this is just modastack.
COPY . .
RUN python -m build --wheel --outdir /dist \
    && /opt/venv/bin/pip install --no-cache-dir --no-deps /dist/*.whl

#####################################################################
# builder-pypi — install a published modastack (binary-mode deploy).#
#####################################################################
FROM builder-base AS builder-pypi
# Pinned to the operator's CLI so the instance runs the same code as the binary
# that deployed it.
ARG MODASTACK_VERSION
# Install the kb deps the code actually uses (fastembed — the lightweight ONNX
# embedder — and sqlite-vec) EXPLICITLY, not via the `[kb]` extra: some published
# releases stale-list `sentence-transformers` in `[kb]`, dragging in torch + ~2 GB
# of CUDA wheels the dark CPU instance never uses (and that can blow the build
# timeout). Keep in sync with pyproject's `[project.optional-dependencies].kb`.
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir \
        "modastack==${MODASTACK_VERSION}" "fastembed>=0.4" "sqlite-vec>=0.1.6"

#####################################################################
# builder-wheel — install a PREBUILT local wheel (release pipeline). #
#####################################################################
# The release pipeline builds the wheel ONCE in CI and stages it into the build
# context at dist/ (re-included in .dockerignore). We install THAT exact artifact
# here, so the canary smokes — and the fleet runs — the same bytes we publish to
# PyPI (not a separately source-rebuilt wheel). Deps come from the same
# pyproject-keyed layer as builder-source (cached across code-only changes); the
# wheel goes in --no-deps on top.
FROM builder-base AS builder-wheel
WORKDIR /src
COPY pyproject.toml ./
RUN python -m venv /opt/venv \
    && python -c "import tomllib; print(chr(10).join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > /tmp/reqs.txt \
    && /opt/venv/bin/pip install --no-cache-dir -r /tmp/reqs.txt "fastembed>=0.4" "sqlite-vec>=0.1.6"
# The prebuilt wheel staged by the release pipeline (exactly one *.whl in dist/).
COPY dist/ /dist/
RUN /opt/venv/bin/pip install --no-cache-dir --no-deps /dist/*.whl

# Select the builder. With MODASTACK_BUILD=pypi, builder-source isn't in the graph
# (its `COPY . .` never runs), so the tiny binary context needs no source tree.
# MODASTACK_BUILD=wheel (the release pipeline) installs the prebuilt dist/ wheel.
FROM builder-${MODASTACK_BUILD} AS builder

#####################################################################
# model-baker — pre-download the fastembed model. Keyed ONLY on the  #
# pinned fastembed version, so this (the slowest layer — a multi-     #
# minute model download) stays cached across every code/framework    #
# change. Runtime COPYs the baked model in BELOW the volatile venv,   #
# so a code-only rebuild never re-bakes it. (Install fastembed alone, #
# never `[kb]` — see builder-source for the torch-bloat rationale.)   #
#####################################################################
FROM python:3.11-slim AS model-baker
ENV HF_HOME=/opt/modastack/models
RUN pip install --no-cache-dir "fastembed>=0.4" \
    && python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='sentence-transformers/all-MiniLM-L6-v2')" \
    && chmod -R a+rX /opt/modastack/models

#####################################################################
# Runtime — slim image, no build tools, no Node. (Shared by both.)  #
#####################################################################
FROM python:3.11-slim AS runtime

# Channel or exact version for the native `claude` installer. Default to the
# `stable` channel (one week behind latest, skips major regressions); pass an
# exact version (e.g. 2.1.89) for fully reproducible production builds.
ARG CLAUDE_VERSION=stable
ARG MODASTACK_UID=10001
# Pinned aichat (the general-purpose LLM gateway CLI). Bump deliberately via
# `gh api repos/sigoden/aichat/releases/latest --jq .tag_name`.
ARG AICHAT_VERSION=0.30.0

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:/home/modastack/.local/bin:${PATH}" \
    HF_HOME=/opt/modastack/models \
    DISABLE_AUTOUPDATER=1 \
    DATA_DIR=/data \
    MODASTACK_PROJECT=/data/project \
    MODASTACK_HOME=/home/modastack
# NB: HOME is the IMAGE home (above), NOT the volume — baked team tools
# (~/.claude/skills, ~/dev/gstack) are read in place. Claude's durable state
# (creds + transcripts) is redirected to the volume at runtime via
# CLAUDE_CONFIG_DIR, which the entrypoint sets (NOT a build ENV — build steps
# must write skills to the image ~/.claude, so they `env -u CLAUDE_CONFIG_DIR`).

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

# Layers are ordered stable → volatile so a code-only rebuild touches only the
# last (cheap) layers — the model download and `claude` fetch stay cached. See
# docs/design/CUSTOM_AGENT_DEPS.md §"three clocks" for the ordering rationale.

# --- stable layers (cached across code/framework changes) ----------#
# Pinned native `claude` CLI (no Node) installed as the modastack user so it
# lands in ~/.local/bin (on PATH above). Cache key is CLAUDE_VERSION alone.
USER modastack
RUN curl -fsSL https://claude.ai/install.sh | bash -s -- "${CLAUDE_VERSION}" \
    && /home/modastack/.local/bin/claude --version
USER root

# General-purpose LLM gateway CLI (aichat): lets any agent call out to other
# models over OpenAI-compatible endpoints — a one-shot model call, NOT agent
# delegation (that's codex). Baked into the base image for every team, like
# git/claude; the image stays generic — provider/model/key come from the team's
# `gateway` connection + env at runtime, never from this layer. Pinned,
# arch-detected static musl binary into /usr/local/bin (system-wide, on PATH).
# Cache key is AICHAT_VERSION alone, so a code-only rebuild never re-fetches it.
RUN arch="$(dpkg --print-architecture)" \
    && case "$arch" in \
         amd64) target=x86_64-unknown-linux-musl ;; \
         arm64) target=aarch64-unknown-linux-musl ;; \
         *) echo "aichat: unsupported arch $arch" >&2; exit 1 ;; \
       esac \
    && curl -fsSL "https://github.com/sigoden/aichat/releases/download/v${AICHAT_VERSION}/aichat-v${AICHAT_VERSION}-${target}.tar.gz" \
       | tar -xz -C /usr/local/bin aichat \
    && chmod +x /usr/local/bin/aichat \
    && aichat --version

# Baked fastembed embedding model (cold-start speed; immutable). HF_HOME points
# here at both build and run, so it's a cache hit at runtime. Copied from
# model-baker, whose only cache key is the fastembed version — so an ordinary
# code change never re-downloads the model.
COPY --from=model-baker /opt/modastack/models /opt/modastack/models

# --- team-deps hook (C24 team-flavored images) ---------------------#
# A team's baked host tools (node, codex, gstack, …) as ONE stable layer,
# rendered from its `build:` spec by modastack/build_render.py and injected via
# the TEAM_DEPS build-arg. The default is a no-op, so a plain build / a no-deps
# team is byte-identical to the generic image. Positioned as the LAST stable
# layer — just BELOW the volatile venv COPY — so a code-only framework release
# rebuilds only the wheel, never re-runs the team's apt/npm/run. The hook runs
# as root (USER is root here); it `gosu`s to modastack to bake ~-relative tools
# into the image HOME (/home/modastack/.claude/skills, ~/dev/gstack), which the
# agent reads in place at runtime — the entrypoint redirects only Claude's
# durable state to the volume (CLAUDE_CONFIG_DIR) and symlinks skills back.
# See docs/design/CUSTOM_AGENT_DEPS.md §"three clocks".
ARG TEAM_DEPS=docker/noop-deps.sh
COPY ${TEAM_DEPS} /tmp/team-deps.sh
RUN bash /tmp/team-deps.sh && rm -f /tmp/team-deps.sh

# --- volatile layer (rebuilds on any framework/code change) --------#
# The prebuilt venv (modastack + deps) is the LAST heavy layer, so a code-only
# rebuild is just this copy plus the thin layers below — seconds, not minutes.
# Root-owned, world-readable.
COPY --from=builder /opt/venv /opt/venv

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
