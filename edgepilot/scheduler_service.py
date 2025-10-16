"""Lightweight client helpers for integrating a scheduler with EdgePilot APIs."""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

import httpx


class SchedulerClient:
  """Convenience wrapper so a scheduler can drive EdgePilot automatically."""

  def __init__(self, base_url: Optional[str] = None, *, timeout: float = 20.0):
    self._base_url = base_url or os.getenv("EDGE_BASE_URL", "http://127.0.0.1:5057")
    self._timeout = timeout

  def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    response = httpx.post(
      f"{self._base_url}{path}",
      json=payload,
      timeout=self._timeout,
    )
    response.raise_for_status()
    return response.json()

  def assign_jobs(
    self,
    jobs: Iterable[Dict[str, Any]],
    *,
    profile_window: str = "15m",
    policy_tags: Optional[Iterable[str]] = None,
    record_assignments: bool = True,
  ) -> Dict[str, Any]:
    """Call /scheduler/assign and return the policy recommendation."""
    payload: Dict[str, Any] = {
      "jobs": list(jobs),
      "profile_window": profile_window,
      "record_assignments": record_assignments,
    }
    if policy_tags:
      payload["policy_tags"] = list(policy_tags)
    return self._post("/scheduler/assign", payload)

  def update_job(
    self,
    *,
    job_id: str,
    workload: str,
    status: str,
    policy_id: Optional[str] = None,
    submitted_at: Optional[str] = None,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
    metrics: Optional[Dict[str, float]] = None,
    tags: Optional[List[str]] = None,
    notes: Optional[str] = None,
  ) -> Dict[str, Any]:
    """Record a lifecycle event via /jobs/run (submit/start/finish)."""
    if not submitted_at:
      submitted_at = datetime.utcnow().isoformat()
    payload = {
      "job_id": job_id,
      "workload": workload,
      "policy_id": policy_id,
      "status": status,
      "submitted_at": submitted_at,
      "started_at": started_at,
      "finished_at": finished_at,
      "metrics": metrics or {},
      "tags": tags or [],
      "notes": notes,
    }
    return self._post("/jobs/run", payload)

  def record_policy_run(
    self,
    policy_id: str,
    *,
    kpis: Dict[str, float],
    workload_label: Optional[str] = None,
    notes: Optional[str] = None,
  ) -> Dict[str, Any]:
    """Push post-run KPIs to /policies/{id}/runs."""
    payload = {"kpis": kpis}
    if workload_label:
      payload["workload_label"] = workload_label
    if notes:
      payload["notes"] = notes
    return self._post(f"/policies/{policy_id}/runs", payload)


def example_flow() -> None:
  """Minimal demonstration suitable for unit tests or local smoke runs."""
  client = SchedulerClient()
  batch_jobs = [
    {"job_id": "example-build", "workload": "web-frontend"},
    {"job_id": "example-report", "workload": "batch-mixed"},
  ]
  assignment = client.assign_jobs(batch_jobs)
  policy_id = assignment["policy"]["id"]

  # Mark jobs as running
  for job in batch_jobs:
    client.update_job(
      job_id=job["job_id"],
      workload=job["workload"],
      status="running",
      policy_id=policy_id,
      started_at=datetime.utcnow().isoformat(),
    )

  # Finish the first job with metrics
  client.update_job(
    job_id="example-build",
    workload="web-frontend",
    status="succeeded",
    policy_id=policy_id,
    finished_at=datetime.utcnow().isoformat(),
    metrics={"p99_latency_ms": 41.0, "throughput_rps": 1180.0},
  )

  # Finish the second job with different metrics
  client.update_job(
    job_id="example-report",
    workload="batch-mixed",
    status="succeeded",
    policy_id=policy_id,
    finished_at=datetime.utcnow().isoformat(),
    metrics={"throughput_jobs_per_min": 95.0},
  )

  # Aggregate metrics back into the policy repository
  client.record_policy_run(
    policy_id,
    kpis={"p99_latency_ms": 41.0, "throughput_rps": 1180.0, "throughput_jobs_per_min": 95.0},
    workload_label="web-frontend+batch-mixed",
    notes="Automated example flow",
  )

  print(json.dumps(assignment, indent=2))


if __name__ == "__main__":
  example_flow()
