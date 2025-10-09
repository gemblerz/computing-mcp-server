"""Simple CLI to invoke EdgePilot endpoints with structured input."""

import argparse
import json
import os
from typing import Any, Dict

import httpx

EDGE_BASE_URL = os.environ.get("EDGE_BASE_URL", "http://127.0.0.1:5057")


def call_edgepilot(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
  response = httpx.post(f"{EDGE_BASE_URL}{path}", json=payload, timeout=20.0)
  response.raise_for_status()
  return response.json()


parser = argparse.ArgumentParser(description="EdgePilot CLI helper")
sub = parser.add_subparsers(dest="cmd")

report_parser = sub.add_parser("report", help="Call /report/facts")
report_parser.add_argument("--window", default="1h")
report_parser.add_argument("--top-k", type=int, default=5)
report_parser.add_argument("--report", default="edge_status")
report_parser.add_argument("--filters", default="{}", help="JSON object string for filters")

run_parser = sub.add_parser("can-run", help="Call /advice/can_run")
run_parser.add_argument("--cpu-pct", type=float, required=True)
run_parser.add_argument("--mem-bytes", type=float, required=True)
run_parser.add_argument("--disk-free-bytes", type=float, required=True)
run_parser.add_argument("--duration", default="45m")
run_parser.add_argument("--host")

window_parser = sub.add_parser("suggest", help="Call /advice/suggest_window")
window_parser.add_argument("--cpu-pct", type=float, required=True)
window_parser.add_argument("--mem-bytes", type=float, required=True)
window_parser.add_argument("--disk-free-bytes", type=float, required=True)
window_parser.add_argument("--duration", default="45m")
window_parser.add_argument("--horizon-hours", type=int, default=24)
window_parser.add_argument("--host")

args = parser.parse_args()

if args.cmd == "report":
  filters = json.loads(args.filters)
  payload = {
    "report": args.report,
    "window": args.window,
    "top_k": args.top_k,
    "filters": filters,
  }
  print(json.dumps(call_edgepilot("/report/facts", payload), indent=2))
elif args.cmd == "can-run":
  payload = {
    "requirements": {
      "cpu_pct": args.cpu_pct,
      "mem_bytes": args.mem_bytes,
      "disk_free_bytes": args.disk_free_bytes,
    },
    "duration": args.duration,
  }
  if args.host:
    payload["host"] = args.host
  print(json.dumps(call_edgepilot("/advice/can_run", payload), indent=2))
elif args.cmd == "suggest":
  payload = {
    "requirements": {
      "cpu_pct": args.cpu_pct,
      "mem_bytes": args.mem_bytes,
      "disk_free_bytes": args.disk_free_bytes,
    },
    "duration": args.duration,
    "horizon_hours": args.horizon_hours,
  }
  if args.host:
    payload["host"] = args.host
  print(json.dumps(call_edgepilot("/advice/suggest_window", payload), indent=2))
else:
  parser.print_help()
