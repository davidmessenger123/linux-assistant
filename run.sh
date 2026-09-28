#!/usr/bin/env bash
# Launch the assistant from a checkout, without installing it.
set -euo pipefail
script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$script_dir"
export ASSISTANT_APP_DIR="$script_dir"
exec python3 -m assistant "$@"
