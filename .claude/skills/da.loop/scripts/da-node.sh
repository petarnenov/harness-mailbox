#!/bin/sh
# Resolve a Node.js >= 18 and run one of the da.loop scripts.
#
# The skill runs on hosts whose non-interactive shell has no node on PATH (remote ssh sessions on this
# repository's machines), so PATH alone is not enough: fall back to the highest nvm/homebrew/local build.
# Usage: sh <skill-dir>/scripts/da-node.sh <script.mjs|name> [args...]
set -eu

dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
script=${1:-}
[ -n "$script" ] || { echo "usage: da-node.sh <script.mjs> [args...]" >&2; exit 2; }
shift || true
[ -f "$script" ] || script="$dir/$script"

find_node() {
    for candidate in node /opt/homebrew/bin/node /usr/local/bin/node /usr/bin/node; do
        if command -v "$candidate" >/dev/null 2>&1; then
            command -v "$candidate"
            return 0
        fi
    done
    for candidate in \
        $(ls -d "$HOME"/.nvm/versions/node/*/bin/node 2>/dev/null | sort -Vr) \
        $(ls -d /home/*/.nvm/versions/node/*/bin/node 2>/dev/null | sort -Vr) \
        $(ls -d /root/.nvm/versions/node/*/bin/node 2>/dev/null | sort -Vr); do
        if [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

node=$(find_node || true)
[ -n "$node" ] || { echo "LESSONS=FAIL no node >= 18 found (install node or add it to PATH)" >&2; exit 127; }
exec "$node" "$script" "$@"
