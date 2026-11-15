#!/usr/bin/env bash
# Set values in the project's .env safely:
#
#   deploy/set-env.sh KEY=VALUE [KEY=VALUE ...]
#
# Each KEY= line is replaced where it stands (duplicates removed) or appended
# when missing. Windows line endings are normalised; .env keeps its owner and
# mode. Values are never printed (.env holds secrets). Then run ./deploy.sh.
set -euo pipefail
cd "$(dirname "$0")/.."

[ $# -gt 0 ] || { sed -n '2,8p' "$0"; exit 2; }
[ -f .env ] || { echo ".env not found - run ./deploy.sh once to create it." >&2; exit 1; }

for pair in "$@"; do
    key="${pair%%=*}"
    if [[ "$pair" != *=* ]] || ! [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
        echo "Not KEY=VALUE: $pair" >&2
        exit 2
    fi
    tmp="$(mktemp)"
    # ENVIRON (not awk -v) so backslashes in values arrive unchanged.
    KEY="$key" VALUE="${pair#*=}" awk '
        BEGIN { k = ENVIRON["KEY"]; line = k "=" ENVIRON["VALUE"]; done = 0 }
        { sub(/\r$/, "") }
        index($0, k "=") == 1 { if (!done) { print line; done = 1 }; next }
        { print }
        END { if (!done) print line }
    ' .env > "$tmp"
    cat "$tmp" > .env
    rm -f "$tmp"
    echo "$key updated"
done
