#!/usr/bin/env bash
set -euo pipefail

VERSION="${1:?usage: smoke-homebrew-bottles.sh <version>}"
MAX_ATTEMPTS="${BOBI_HOMEBREW_SMOKE_ATTEMPTS:-60}"
SLEEP_SECONDS="${BOBI_HOMEBREW_SMOKE_SLEEP:-30}"
EXPECTED_ROOT_URL="https://github.com/moda-labs/homebrew-bobi/releases/download/bobi-${VERSION}"

fetch_formula() {
  if [ -n "${BOBI_HOMEBREW_FORMULA_FILE:-}" ]; then
    cat "$BOBI_HOMEBREW_FORMULA_FILE"
    return
  fi

  gh api repos/moda-labs/homebrew-bobi/contents/Formula/bobi.rb --jq .content | base64 -d
}

parse_formula() {
  local formula="$1"

  FORMULA="$formula" VERSION="$VERSION" EXPECTED_ROOT_URL="$EXPECTED_ROOT_URL" python3 - <<'PY'
import os
import re
import sys

formula = os.environ["FORMULA"]
version = os.environ["VERSION"]
expected_root_url = os.environ["EXPECTED_ROOT_URL"]

if f"bobi-{version}.tar.gz" not in formula:
    print(f"source formula is not at {version} yet")
    sys.exit(10)

block_match = re.search(r"(?ms)^  bottle do\n(?P<block>.*?)^  end$", formula)
if not block_match:
    print(f"bottle block for {version} is not ready yet")
    sys.exit(11)

block = block_match.group("block")
root_match = re.search(r'^\s*root_url\s+"([^"]+)"\s*$', block, re.MULTILINE)
if not root_match:
    print(f"bottle root_url for {version} is not ready yet")
    sys.exit(11)

root_url = root_match.group(1)
if root_url != expected_root_url:
    if root_url.startswith(expected_root_url + ".") or f"bobi-{version}" in root_url:
        print(
            f"Homebrew bottle root_url is malformed: expected {expected_root_url}, got {root_url}",
            file=sys.stderr,
        )
        sys.exit(12)
    print(f"bottle root_url for {version} is not ready yet: {root_url}")
    sys.exit(11)

tags = re.findall(
    r'^\s*sha256\s+cellar:\s*[^,\n]+,\s*([A-Za-z0-9_]+):\s*"[0-9a-fA-F]+"\s*$',
    block,
    re.MULTILINE,
)
if not tags:
    print(f"Homebrew formula for {version} has no bottle sha256 entries", file=sys.stderr)
    sys.exit(13)

print(root_url)
for tag in tags:
    print(tag)
PY
}

for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
  if ! formula="$(fetch_formula)"; then
    echo "Waiting for Homebrew formula fetch to succeed (${attempt}/${MAX_ATTEMPTS})"
    sleep "$SLEEP_SECONDS"
    continue
  fi

  parse_output_file="$(mktemp)"
  if parse_formula "$formula" >"$parse_output_file" 2>&1; then
    mapfile -t parsed <"$parse_output_file"
    rm -f "$parse_output_file"

    root_url="${parsed[0]}"
    bottle_tags=("${parsed[@]:1}")

    for bottle_tag in "${bottle_tags[@]}"; do
      bottle_url="${root_url}/bobi-${VERSION}.${bottle_tag}.bottle.tar.gz"
      echo "Checking ${bottle_url}"
      if [ "${BOBI_HOMEBREW_SKIP_HEAD:-}" = "1" ]; then
        continue
      fi
      curl --retry 3 --retry-delay 2 --retry-all-errors -IfS "$bottle_url" >/dev/null
    done

    exit 0
  else
    status=$?
    message="$(cat "$parse_output_file")"
    rm -f "$parse_output_file"
    if [ "$status" -eq 12 ] || [ "$status" -eq 13 ]; then
      echo "::error::${message}"
      exit 1
    fi
    echo "Waiting for Homebrew formula and bottles for ${VERSION}: ${message} (${attempt}/${MAX_ATTEMPTS})"
    sleep "$SLEEP_SECONDS"
  fi
done

echo "::error::Timed out waiting for Homebrew formula and bottles for ${VERSION}"
exit 1
