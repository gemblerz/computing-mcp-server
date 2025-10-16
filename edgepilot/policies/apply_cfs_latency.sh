#!/usr/bin/env bash
# Apply modest CFS latency tweaks before running the target command.
# Requires permission to call sysctl (run under sudo or grant capabilities).
set -euo pipefail

sudo sysctl -w kernel.sched_min_granularity_ns=4000000 >/dev/null
sudo sysctl -w kernel.sched_wakeup_granularity_ns=5000000 >/dev/null

if [[ $# -gt 0 ]]; then
  exec "$@"
fi
