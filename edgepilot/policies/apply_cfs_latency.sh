#!/usr/bin/env bash
set -euo pipefail

if [[ $# -eq 0 ]]; then
  echo "apply_cfs_latency: pass a command to run" >&2
  exit 1
fi

sudo sysctl -w kernel.sched_min_granularity_ns=4000000 >/dev/null 2>&1 || true
sudo sysctl -w kernel.sched_wakeup_granularity_ns=5000000 >/dev/null 2>&1 || true

exec "$@"
