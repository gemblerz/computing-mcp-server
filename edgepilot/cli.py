"""Simple CLI to invoke EdgePilot endpoints with structured input."""

import argparse
import json
import os
from datetime import datetime
from typing import Any, Dict

import httpx

EDGE_BASE_URL = os.environ.get("EDGE_BASE_URL", "http://127.0.0.1:5057")


def call_edgepilot(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
  response = httpx.post(f"{EDGE_BASE_URL}{path}", json=payload, timeout=20.0)
  response.raise_for_status()
  return response.json()


def _parse_json(value: str, default: Any) -> Any:
  if value is None:
    return default
  try:
    return json.loads(value)
  except json.JSONDecodeError as exc:
    raise SystemExit(f"Invalid JSON payload: {value}\n{exc}") from exc


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

assign_parser = sub.add_parser("assign", help="Call /scheduler/assign for a batch of jobs")
assign_parser.add_argument("--jobs", required=True, help="JSON list of job descriptors")
assign_parser.add_argument("--profile-window", default="15m")
assign_parser.add_argument("--policy-tags", default="", help="Comma-separated workload tags to filter policies")
assign_parser.add_argument("--no-record", action="store_true", help="Do not persist assignments in the job log")

job_update_parser = sub.add_parser("job-update", help="Update job lifecycle via /jobs/run")
job_update_parser.add_argument("--job-id", required=True)
job_update_parser.add_argument("--workload", required=True)
job_update_parser.add_argument("--status", required=True)
job_update_parser.add_argument("--policy-id")
job_update_parser.add_argument("--metrics", help="JSON dict of metrics", default="{}")
job_update_parser.add_argument("--tags", help="JSON list of tags", default="[]")
job_update_parser.add_argument("--notes")
job_update_parser.add_argument("--submitted-at", help="ISO8601 timestamp, default now")
job_update_parser.add_argument("--started-at")
job_update_parser.add_argument("--finished-at")

policy_run_parser = sub.add_parser("policy-run", help="Post KPI results to /policies/{id}/runs")
policy_run_parser.add_argument("--policy-id", required=True)
policy_run_parser.add_argument("--kpis", required=True, help="JSON dict of KPI metrics")
policy_run_parser.add_argument("--workload-label")
policy_run_parser.add_argument("--notes")

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
elif args.cmd == "assign":
  jobs = _parse_json(args.jobs, [])
  if not isinstance(jobs, list) or not jobs:
    raise SystemExit("--jobs must be a non-empty JSON list")
  tags = [tag.strip() for tag in args.policy_tags.split(",") if tag.strip()]
  payload = {
    "jobs": jobs,
    "profile_window": args.profile_window,
    "record_assignments": not args.no_record,
  }
  if tags:
    payload["policy_tags"] = tags
  print(json.dumps(call_edgepilot("/scheduler/assign", payload), indent=2))
elif args.cmd == "job-update":
  metrics = _parse_json(args.metrics, {})
  tags = _parse_json(args.tags, [])
  now = datetime.utcnow().isoformat()
  payload = {
    "job_id": args.job_id,
    "workload": args.workload,
    "policy_id": args.policy_id,
    "status": args.status,
    "submitted_at": args.submitted_at or now,
    "started_at": args.started_at,
    "finished_at": args.finished_at,
    "metrics": metrics,
    "tags": tags,
    "notes": args.notes,
  }
  print(json.dumps(call_edgepilot("/jobs/run", payload), indent=2))
elif args.cmd == "policy-run":
  kpis = _parse_json(args.kpis, {})
  payload = {
    "kpis": kpis,
    "workload_label": args.workload_label,
    "notes": args.notes,
  }
  path = f"/policies/{args.policy_id}/runs"
  print(json.dumps(call_edgepilot(path, payload), indent=2))
else:
  parser.print_help()
