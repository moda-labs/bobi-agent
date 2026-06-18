#!/usr/bin/env bash
#
# Container liveness probe. The manager health server (C2) binds 127.0.0.1 on a
# free port and writes it to state/manager-health.port; read it and probe
# /health. Works for `docker run` HEALTHCHECK and Fly script checks alike
# (both execute inside the machine, where localhost is reachable).
set -euo pipefail

PROJECT_DIR="${MODASTACK_PROJECT:-/data/project}"
PORT_FILE="${PROJECT_DIR}/.modastack/state/manager-health.port"

[ -f "${PORT_FILE}" ] || { echo "health: no port file at ${PORT_FILE}"; exit 1; }

PORT="$(cat "${PORT_FILE}")"
[ -n "${PORT}" ] || { echo "health: empty port file"; exit 1; }

curl -fsS --max-time 4 "http://127.0.0.1:${PORT}/health" >/dev/null \
  || { echo "health: /health probe failed on port ${PORT}"; exit 1; }
