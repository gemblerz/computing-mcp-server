"""Toy scheduler loop that exercises SchedulerClient end to end."""

from __future__ import annotations

import random
import time
from datetime import datetime, timedelta
from typing import Dict, List

from .scheduler_service import SchedulerClient


def _now_iso() -> str:
  return datetime.utcnow().isoformat()


def _generate_jobs(batch_id: int) -> List[Dict[str, str]]:
  workloads = ["web-frontend", "batch-mixed", "shared-cluster"]
  jobs: List[Dict[str, str]] = []
  for idx in range(2):
    job_id = f"demo-{batch_id}-{idx+1}"
    workload = workloads[(batch_id + idx) % len(workloads)]
    jobs.append({"job_id": job_id, "workload": workload})
  return jobs


def run_demo_batches(total_batches: int = 3, delay_seconds: float = 2.0) -> None:
  client = SchedulerClient()
  print(f"[demo] starting {total_batches} batches\n")
  for batch_idx in range(total_batches):
    jobs = _generate_jobs(batch_idx)
    assignment = client.assign_jobs(jobs)
    policy = assignment["policy"]
    policy_id = policy["id"]

    print(f"[demo] batch {batch_idx}: selected policy {policy_id}")

    batch_metrics: List[Dict[str, float]] = []
    for job in jobs:
      client.update_job(
        job_id=job["job_id"],
        workload=job["workload"],
        policy_id=policy_id,
        status="running",
        started_at=_now_iso(),
      )
      time.sleep(delay_seconds / 2)
      metrics = {
        "p99_latency_ms": round(random.uniform(35, 60), 2),
        "throughput_rps": round(random.uniform(800, 1300), 1),
      }
      client.update_job(
        job_id=job["job_id"],
        workload=job["workload"],
        policy_id=policy_id,
        status="succeeded",
        finished_at=_now_iso(),
        metrics=metrics,
      )
      batch_metrics.append(metrics)
      print(f"[demo] job {job['job_id']} completed with metrics {metrics}")

    aggregated = {}
    if batch_metrics:
      aggregated["p99_latency_ms"] = round(
        sum(m["p99_latency_ms"] for m in batch_metrics) / len(batch_metrics),
        2,
      )
      aggregated["throughput_rps"] = round(
        sum(m["throughput_rps"] for m in batch_metrics) / len(batch_metrics),
        1,
      )
    client.record_policy_run(
      policy_id,
      kpis=aggregated,
      workload_label="+".join(job["workload"] for job in jobs),
      notes="demo batch run",
    )
    print(f"[demo] recorded policy run for {policy_id}\n")
    time.sleep(delay_seconds)

  print("[demo] all batches completed.")


if __name__ == "__main__":
  run_demo_batches()
