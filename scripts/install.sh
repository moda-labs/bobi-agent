#!/usr/bin/env bash
# Install bobi
#
# One-liner:
#   curl -sL https://raw.githubusercontent.com/moda-labs/bobi-agent/main/scripts/install.sh | bash

set -euo pipefail

release_marker=""
released_tool_root=""
release_failed=0

relock_framework_roots() {
    local tool_root="$1"
    local root
    [[ -d "$tool_root/lib" ]] || return 0
    while IFS= read -r root; do
        chmod -R a-w "$root" || true
    done < <(find "$tool_root/lib" -type d \( -name bobi -o -name 'bobi-*.dist-info' \))
}

remove_release_marker() {
    if [[ -n "$release_marker" ]]; then
        if ! rm -f "$release_marker"; then
            echo "Could not close Bobi runtime guard release window: $release_marker" >&2
            return 1
        fi
    fi
}

on_exit() {
    local status=$?
    if (( release_failed )); then
        remove_release_marker || true
    fi
    if (( release_failed )) && [[ -n "$released_tool_root" ]]; then
        relock_framework_roots "$released_tool_root"
    fi
    exit "$status"
}

trap on_exit EXIT

require_supported_node() {
    local version
    local major

    if ! command -v node &>/dev/null; then
        echo "Node.js 20+ is required for Bobi's local event server, but node was not found on PATH." >&2
        echo "Install Node.js 20 or newer, ensure node is on PATH, and rerun this installer." >&2
        exit 1
    fi
    if ! version="$(node --version 2>/dev/null)"; then
        echo "Node.js 20+ is required, but 'node --version' failed." >&2
        echo "Repair or upgrade Node.js and rerun this installer." >&2
        exit 1
    fi
    major="${version#v}"
    major="${major%%.*}"
    case "$major" in
        ""|*[!0-9]*)
            echo "Node.js 20+ is required, but the installed version could not be parsed: $version" >&2
            exit 1
            ;;
    esac
    if (( major < 20 )); then
        echo "Node.js 20+ is required for Bobi's local event server; found $version." >&2
        echo "Upgrade Node.js, ensure the newer node is on PATH, and rerun this installer." >&2
        exit 1
    fi
}

require_supported_node

if ! command -v uv &>/dev/null; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

resolve_bobi_home() {
    local raw="${BOBI_HOME:-$HOME/.bobi}"
    case "$raw" in
        "~") raw="$HOME" ;;
        \~/*) raw="$HOME/${raw:2}" ;;
    esac
    if [[ "$raw" != /* ]]; then
        raw="$PWD/$raw"
    fi
    mkdir -p "$raw"
    (cd "$raw" && pwd -P)
}

json_escape() {
    local value="$1"
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    value="${value//$'\n'/\\n}"
    value="${value//$'\r'/\\r}"
    value="${value//$'\t'/\\t}"
    printf '%s' "$value"
}

write_release_marker() {
    local tool_root="$1"
    local expires_at="$2"
    local state="$3"
    local marker_tmp="$release_marker.$$"

    printf '{"prefix":"%s","expires_at":%s,"opened_by":"scripts/install.sh","pid":%s,"state":"%s"}\n' \
        "$(json_escape "$tool_root")" "$expires_at" "$$" "$state" > "$marker_tmp"
    mv "$marker_tmp" "$release_marker"
}

release_write_guard() {
    local tool_dir="$1"
    local tool_root="$tool_dir/bobi"
    local bobi_home
    local expires_at

    [[ -d "$tool_root" ]] || return 0
    bobi_home="$(resolve_bobi_home)"
    release_marker="$bobi_home/runtime-guard-released"
    released_tool_root="$tool_root"
    release_failed=1
    expires_at="$(( $(date +%s) + 900 ))"
    # Older Bobi versions ignore state, so make the transient marker expired
    # until the tree is fully writable and the open marker can be activated.
    write_release_marker "$tool_root" "0" "opening"
    chmod -R u+w "$tool_root"
    write_release_marker "$tool_root" "$expires_at" "open"
    # Repair an apply sweep that started while the marker was still opening.
    chmod -R u+w "$tool_root"
    release_failed=0
}

echo "Installing bobi..."
tool_dir="$(uv tool dir)"
release_write_guard "$tool_dir"
uv tool install --force bobi
if [[ -n "$release_marker" ]]; then
    if ! write_release_marker "$released_tool_root" "0" "opening" ||
            ! remove_release_marker; then
        relock_framework_roots "$released_tool_root"
        exit 1
    fi
fi
release_marker=""
released_tool_root=""

echo ""
echo "Done. Run 'bobi setup <name>' to create a Bobi Agent, or"
echo "'bobi agents install <source> --name <name>' to install one."
