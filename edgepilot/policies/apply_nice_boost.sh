#!/usr/bin/env bash
# Run the target command with higher CPU priority using nice.
set -euo pipefail

if [[ $# -eq 0 ]]; then
  echo "apply_nice_boost: no command provided" >&2
  exit 1
fi

exec nice -n -5 "$@"
