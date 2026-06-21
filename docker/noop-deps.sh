#!/usr/bin/env bash
# Default TEAM_DEPS hook (C24): a no-op. A plain `docker build` and any team
# with no `build:` spec use this, so the image is byte-identical to the generic
# one. Teams that declare deps get a script rendered by modastack.build_render
# passed via --build-arg TEAM_DEPS=<rendered>.
set -euo pipefail
echo "[team-deps] no team build spec — generic image"
