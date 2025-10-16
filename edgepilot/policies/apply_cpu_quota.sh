#!/usr/bin/env bash
set -euo pipefail

if [[ $# -eq 0 ]]; then
  echo "apply_cpu_quota: pass a command to run" >&2
  exit 1
fi

if ! command -v systemd-run >/dev/null; then
  echo "apply_cpu_quota: systemd-run is required." >&2
  exit 1
fi

sudo systemd-run --scope -p CPUAccounting=yes -p CPUQuota=200% "$@"
