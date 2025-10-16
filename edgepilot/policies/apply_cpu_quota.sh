#!/usr/bin/env bash
# Run the target command inside a CPU throttled cgroup (requires libcgroup tools).
set -euo pipefail

GROUP_NAME="edgepilot-quota"
CGROUP_ROOT="/sys/fs/cgroup"

if ! command -v cgexec >/dev/null; then
  echo "apply_cpu_quota: cgexec not found; install libcgroup tools." >&2
  exit 1
fi

sudo mkdir -p "${CGROUP_ROOT}/${GROUP_NAME}"
echo 200000 | sudo tee "${CGROUP_ROOT}/${GROUP_NAME}/cpu.max" >/dev/null
echo 50000  | sudo tee "${CGROUP_ROOT}/${GROUP_NAME}/cpu.weight" >/dev/null

if [[ $# -eq 0 ]]; then
  echo "apply_cpu_quota: no command provided" >&2
  exit 1
fi

exec cgexec -g cpu:${GROUP_NAME} "$@"
