import json
import math
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytz
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from uuid import uuid4

from .job_store import JobRun, JobStore
from .policy_store import PolicyRecord, PolicyStore

PROM_URL = os.getenv("PROM_URL", "http://localhost:9090")
with open("edgepilot/ring.edgepilot.json", "r", encoding="utf-8") as f:
  RING = json.load(f)
with open("edgepilot/blueprint.edge_status.json", "r", encoding="utf-8") as f:
  BLUEPRINT = json.load(f)

app = FastAPI(title="EdgePilot Facts API")
POLICY_STORE_PATH = os.getenv("POLICY_STORE_PATH", "edgepilot/policies.json")
POLICY_SEEDS_PATH = os.getenv("POLICY_SEEDS_PATH", "edgepilot/policy_seeds.json")
POLICIES = PolicyStore(POLICY_STORE_PATH, seeds_path=POLICY_SEEDS_PATH)
JOB_STORE_PATH = os.getenv("JOB_STORE_PATH", "edgepilot/job_history.json")
JOBS = JobStore(JOB_STORE_PATH)


def serialize_policy(record: PolicyRecord) -> Dict[str, Any]:
  """Return a JSON-serializable representation of a policy record."""
  return record.to_dict()


def serialize_job(job: JobRun) -> Dict[str, Any]:
  """Prepare a job run record for API responses."""
  payload = {
    "job_id": job.job_id,
    "workload": job.workload,
    "policy_id": job.policy_id,
    "status": job.status,
    "submitted_at": job.submitted_at.isoformat(),
    "started_at": job.started_at.isoformat() if job.started_at else None,
    "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    "metrics": job.metrics,
    "tags": job.tags,
    "notes": job.notes,
  }
  if job.started_at and job.finished_at:
    payload["duration_seconds"] = (job.finished_at - job.started_at).total_seconds()
  return payload


def _strip_tz(dt: Optional[datetime]) -> Optional[datetime]:
  if dt is None:
    return None
  return dt.replace(tzinfo=None)


def _utc_now() -> datetime:
  return datetime.utcnow().replace(tzinfo=None)


def find_entity(name: str) -> Dict[str, Any]:
  for entity in RING["entities"]:
    if entity["name"] == name:
      return entity
  raise KeyError(f"Entity not found: {name}")


def find_attribute(entity: Dict[str, Any], attr_name: str) -> Dict[str, Any]:
  for attribute in entity["attributes"]:
    if attribute["name"] == attr_name:
      return attribute
  raise KeyError(f"Attribute not found: {entity['name']}.{attr_name}")


async def prom_query(query: str) -> List[Dict[str, Any]]:
  try:
    async with httpx.AsyncClient(timeout=15.0) as client:
      response = await client.get(
        f"{PROM_URL}/api/v1/query",
        params={"query": query},
      )
      response.raise_for_status()
      data = response.json()
  except httpx.HTTPError as exc:
    raise HTTPException(502, f"Prometheus request failed: {exc}") from exc
  if data.get("status") != "success":
    raise HTTPException(500, f"Prometheus error: {data}")
  return data["data"]["result"]


def avg_vector(result: List[Dict[str, Any]]) -> float:
  values = []
  for row in result:
    raw_value = row.get("value", [None, None])[1]
    try:
      candidate = float(raw_value)
    except (TypeError, ValueError):
      continue
    if math.isfinite(candidate):
      values.append(candidate)
  return (sum(values) / len(values)) if values else float("nan")


def pretty_container_name(labels: Dict[str, Any]) -> str:
  if labels.get("container"):
    return labels["container"]
  identifier = labels.get("id", "")
  return (identifier.split("/")[-1] if identifier else "unknown")[:12]


async def scalar_avg_over_time(base_query: str, window: str, resolution: str = "1m") -> float:
  query = f"avg_over_time(({base_query})[{window}:{resolution}])"
  result = await prom_query(query)
  return avg_vector(result)


async def topk_over_time(query: str, window: str, k: int, resolution: str = "1m") -> List[Dict[str, Any]]:
  full_query = f"topk({k}, avg_over_time(({query})[{window}:{resolution}]))"
  return await prom_query(full_query)


async def fetch_scalar_map(query: str) -> Dict[str, float]:
  results = await prom_query(query)
  output: Dict[str, float] = {}
  for row in results:
    instance = row["metric"].get("instance", "_")
    try:
      value = float(row["value"][1])
    except (TypeError, ValueError):
      continue
    if not math.isfinite(value):
      continue
    output[instance] = value
  return output


class ReportRequest(BaseModel):
  report: str = "edge_status"
  window: str = "1h"
  top_k: int = 5
  filters: Dict[str, Any] = {}


@app.post("/report/facts")
async def report_facts(req: ReportRequest):
  if req.report != "edge_status":
    raise HTTPException(400, "Only 'edge_status' implemented in this minimal API.")
  facts: List[str] = []
  for plan in BLUEPRINT["plans"]:
    entity = find_entity(plan["entity"])
    attribute = find_attribute(entity, plan["attribute"])
    base_query = attribute["promql"]

    if plan["template"] == "TREND":
      query = f"avg_over_time(({base_query})[{req.window}:1m])"
      result = await prom_query(query)
      value = avg_vector(result)
      facts.append(plan["statement"].format(range=req.window, value=value))

    elif plan["template"] == "RANKING":
      k_value = plan.get("k", req.top_k) or req.top_k
      query = f"topk({k_value}, {base_query})"
      result = await prom_query(query)
      items = [
        f"{pretty_container_name(row['metric'])}={float(row['value'][1]):.4f}"
        for row in result
      ]
      facts.append(plan["statement"].format(k=k_value, items=", ".join(items) or "none"))

    elif plan["template"] == "THRESHOLD":
      query = f"{base_query} {plan['operator']} {plan['value']}"
      result = await prom_query(query)
      if not result:
        facts.append(plan["statement"].replace("{items}", "none"))
      else:
        items = []
        for row in result:
          metric = row["metric"]
          label = "/".join(
            [metric.get("instance", ""), metric.get("mountpoint", ""), metric.get("device", "")]
          ).strip("/")
          items.append(label)
        facts.append(plan["statement"].format(items=", ".join(items)))
    else:
      raise HTTPException(400, f"Unknown template: {plan['template']}")
  return {"status": "ok", "facts": facts, "window": req.window, "top_k": req.top_k}


@app.post("/report/text")
async def report_text(req: ReportRequest):
  response = await report_facts(req)
  return {
    "status": "ok",
    "facts": response["facts"],
    "text": f"Edge status for the last {req.window}. " + " ".join(response["facts"]),
  }


class WorkloadProfileRequest(BaseModel):
  window: str = "15m"
  top_containers: int = 5
  cpu_hot_pct: float = 80.0
  mem_hot_pct: float = 80.0
  disk_hot_pct: float = 85.0
  include_network: bool = True


async def build_workload_profile(req: WorkloadProfileRequest) -> Dict[str, Any]:
  cpu_query = '100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{job="node-exporter",mode="idle"}[5m])))'
  mem_query = (
    '100 * (1 - node_memory_MemAvailable_bytes{job="node-exporter"} / node_memory_MemTotal_bytes{job="node-exporter"})'
  )
  load_query = 'node_load1{job="node-exporter"}'
  disk_query = (
    '100 * (1 - node_filesystem_free_bytes{job="node-exporter",fstype!~"tmpfs|overlay"}'
    ' / node_filesystem_size_bytes{job="node-exporter",fstype!~"tmpfs|overlay"})'
  )

  cpu_util = await scalar_avg_over_time(cpu_query, req.window)
  mem_used = await scalar_avg_over_time(mem_query, req.window)
  load_avg = await scalar_avg_over_time(load_query, req.window)
  disk_used = await scalar_avg_over_time(disk_query, req.window)

  contention: List[Dict[str, Any]] = []
  if math.isfinite(cpu_util) and cpu_util >= req.cpu_hot_pct:
    contention.append(
      _contention_signal(
        "cpu_utilization_pct",
        "Sustained CPU utilization exceeds target headroom.",
        round(cpu_util, 2),
        req.cpu_hot_pct,
      )
    )
  if math.isfinite(mem_used) and mem_used >= req.mem_hot_pct:
    contention.append(
      _contention_signal(
        "mem_used_pct",
        "Available memory is tight; consider spreading or throttling jobs.",
        round(mem_used, 2),
        req.mem_hot_pct,
      )
    )
  if math.isfinite(disk_used) and disk_used >= req.disk_hot_pct:
    contention.append(
      _contention_signal(
        "disk_used_pct",
        "Disk usage is approaching the configured guardrail.",
        round(disk_used, 2),
        req.disk_hot_pct,
      )
    )

  top_containers = await _collect_top_containers(req.window, req.top_containers)
  network_samples: List[Dict[str, Any]] = []
  if req.include_network:
    network_samples = await _collect_top_network(req.window, req.top_containers)

  recent_jobs = JOBS.recent(limit=10)
  status_counts = JOBS.status_counts()
  job_policy_usage = JOBS.policy_usage()
  policy_summary: List[Dict[str, Any]] = []
  for policy_id, runs in sorted(job_policy_usage.items(), key=lambda item: item[1], reverse=True):
    record = POLICIES.get(policy_id)
    policy_summary.append(
      {
        "policy_id": policy_id,
        "policy_name": record.name if record else None,
        "runs": runs,
        "last_verified_at": record.last_verified_at.isoformat() if record and record.last_verified_at else None,
      }
    )

  profile = {
    "window": req.window,
    "cluster_metrics": {
      "cpu_utilization_pct": cpu_util,
      "mem_used_pct": mem_used,
      "avg_load1": load_avg,
      "disk_used_pct": disk_used,
    },
    "top_cpu_containers": top_containers,
    "top_network_containers": network_samples,
    "contention_signals": contention,
    "job_history": {
      "total_runs": sum(status_counts.values()),
      "status_counts": status_counts,
      "recent_jobs": [serialize_job(job) for job in recent_jobs],
      "policy_usage": policy_summary,
    },
  }
  return profile


class PolicyCreateRequest(BaseModel):
  policy_id: Optional[str] = None
  name: str
  description: str
  intent: str
  target_workloads: List[str] = Field(default_factory=list)
  guardrails: List[str] = Field(default_factory=list)
  embedding: Optional[List[float]] = None
  kpis: Dict[str, float] = Field(default_factory=dict)
  notes: List[str] = Field(default_factory=list)


class PolicySearchRequest(BaseModel):
  query: str = ""
  limit: int = 5
  tags: List[str] = Field(default_factory=list)
  query_embedding: Optional[List[float]] = None


class PolicyRunRequest(BaseModel):
  workload_label: Optional[str] = None
  kpis: Dict[str, float] = Field(default_factory=dict)
  notes: Optional[str] = None


class PolicyVerifyRequest(BaseModel):
  metrics: Dict[str, float]
  guardrail_overrides: Dict[str, float] = Field(default_factory=dict)
  dry_run_notes: Optional[str] = None


class JobRunRecordRequest(BaseModel):
  job_id: str
  workload: str
  policy_id: Optional[str] = None
  status: str = "completed"
  submitted_at: datetime
  started_at: Optional[datetime] = None
  finished_at: Optional[datetime] = None
  metrics: Dict[str, float] = Field(default_factory=dict)
  tags: List[str] = Field(default_factory=list)
  notes: Optional[str] = None


class JobRecentQuery(BaseModel):
  limit: int = Field(default=10, ge=1, le=100)


class JobDescriptor(BaseModel):
  job_id: str
  workload: str
  tags: List[str] = Field(default_factory=list)
  hints: Dict[str, float] = Field(default_factory=dict)


class ScheduleBatchRequest(BaseModel):
  jobs: List[JobDescriptor]
  profile_window: str = "15m"
  policy_tags: List[str] = Field(default_factory=list)
  record_assignments: bool = True


def _contention_signal(metric: str, description: str, observed: float, threshold: float) -> Dict[str, Any]:
  return {
    "metric": metric,
    "description": description,
    "observed_value": observed,
    "threshold": threshold,
  }


async def _collect_top_containers(window: str, limit: int) -> List[Dict[str, Any]]:
  query = (
    "sum by (instance,id) (rate(container_cpu_usage_seconds_total{job=\"cadvisor\",id=~\"/docker/.+\"}[1m]))"
  )
  rows = await topk_over_time(query, window, limit)
  top_containers: List[Dict[str, Any]] = []
  for row in rows:
    try:
      value = float(row["value"][1])
    except (KeyError, ValueError, TypeError):
      continue
    if not math.isfinite(value):
      continue
    metric = row.get("metric", {})
    top_containers.append(
      {
        "container": pretty_container_name(metric),
        "instance": metric.get("instance"),
        "cpu_seconds_per_s": value,
      }
    )
  return top_containers


async def _collect_top_network(window: str, limit: int) -> List[Dict[str, Any]]:
  query = (
    "sum by (instance,id) (rate(container_network_receive_bytes_total{job=\"cadvisor\",id=~\"/docker/.+\"}[1m])"
    " + rate(container_network_transmit_bytes_total{job=\"cadvisor\",id=~\"/docker/.+\"}[1m]))"
  )
  rows = await topk_over_time(query, window, limit)
  samples: List[Dict[str, Any]] = []
  for row in rows:
    try:
      value = float(row["value"][1])
    except (KeyError, ValueError, TypeError):
      continue
    if not math.isfinite(value):
      continue
    metric = row.get("metric", {})
    samples.append(
      {
        "container": pretty_container_name(metric),
        "instance": metric.get("instance"),
        "bytes_per_s": value,
      }
    )
  return samples


def _parse_guardrail(guardrail: str) -> Optional[Tuple[str, str, float]]:
  cleaned = guardrail.replace(" ", "")
  for operator in ("<=", ">=", "<", ">"):
    if operator in cleaned:
      metric, value = cleaned.split(operator, 1)
      try:
        threshold = float(value)
      except ValueError:
        return None
      return metric, operator, threshold
  return None


def _guardrail_failed(actual: float, operator: str, threshold: float) -> bool:
  if not math.isfinite(actual):
    return True
  if operator == "<=":
    return actual > threshold
  if operator == ">=":
    return actual < threshold
  if operator == "<":
    return actual >= threshold
  if operator == ">":
    return actual <= threshold
  return True


def evaluate_guardrails(
  record: PolicyRecord,
  *,
  metrics: Dict[str, float],
  overrides: Dict[str, float],
) -> List[Dict[str, Any]]:
  violations: List[Dict[str, Any]] = []
  for guardrail in record.guardrails:
    parsed = _parse_guardrail(guardrail)
    if not parsed:
      violations.append(
        {
          "guardrail": guardrail,
          "reason": "Could not parse guardrail expression.",
        }
      )
      continue
    metric, operator, threshold = parsed
    threshold = overrides.get(metric, threshold)
    actual = metrics.get(metric)
    if actual is None:
      violations.append(
        {
          "guardrail": guardrail,
          "metric": metric,
          "threshold": threshold,
          "reason": "Metric not supplied by verifier.",
        }
      )
      continue
    if _guardrail_failed(actual, operator, threshold):
      violations.append(
        {
          "guardrail": guardrail,
          "metric": metric,
          "threshold": threshold,
          "actual": actual,
          "operator": operator,
          "reason": "Metric breached guardrail.",
        }
      )
  return violations


def guardrail_penalties_for_metrics(
  record: PolicyRecord,
  metrics: Dict[str, float],
) -> Tuple[int, List[Dict[str, Any]]]:
  penalties: List[Dict[str, Any]] = []
  count = 0
  for guardrail in record.guardrails:
    parsed = _parse_guardrail(guardrail)
    if not parsed:
      continue
    metric, operator, threshold = parsed
    actual = metrics.get(metric)
    if actual is None:
      continue
    if _guardrail_failed(actual, operator, threshold):
      count += 1
      penalties.append(
        {
          "guardrail": guardrail,
          "metric": metric,
          "operator": operator,
          "threshold": threshold,
          "actual": actual,
        }
      )
  return count, penalties


def score_policy_for_batch(
  record: PolicyRecord,
  *,
  workload_tags: set[str],
  metrics: Dict[str, float],
) -> Dict[str, Any]:
  overlap = len(workload_tags & set(record.target_workloads))
  base_score = 1.0 + (overlap * 2.0)
  history_bonus = min(len(record.history), 5) * 0.25
  base_score += history_bonus
  if record.last_verified_at:
    base_score += 0.5
  penalty_count, penalties = guardrail_penalties_for_metrics(record, metrics)
  score = base_score - (penalty_count * 1.5)
  return {
    "policy": record,
    "score": score,
    "overlap": overlap,
    "history_bonus": history_bonus,
    "guardrail_penalties": penalties,
  }


@app.post("/workloads/profile")
async def workloads_profile(req: WorkloadProfileRequest):
  profile = await build_workload_profile(req)
  return {"status": "ok", "profile": profile}


@app.get("/policies")
async def policies_list():
  return {"status": "ok", "policies": [serialize_policy(record) for record in POLICIES.list()]}


@app.get("/policies/{policy_id}")
async def policies_get(policy_id: str):
  record = POLICIES.get(policy_id)
  if not record:
    raise HTTPException(404, f"Policy not found: {policy_id}")
  return {"status": "ok", "policy": serialize_policy(record)}


@app.post("/policies")
async def policies_create(req: PolicyCreateRequest):
  policy_id = req.policy_id or f"policy-{uuid4().hex[:8]}"
  record = POLICIES.create(
    policy_id=policy_id,
    name=req.name,
    description=req.description,
    intent=req.intent,
    target_workloads=req.target_workloads,
    guardrails=req.guardrails,
    embedding=req.embedding,
    kpis=req.kpis,
    notes=req.notes,
  )
  return {"status": "ok", "policy": serialize_policy(record)}


@app.post("/policies/search")
async def policies_search(req: PolicySearchRequest):
  results = POLICIES.search(
    req.query,
    limit=req.limit,
    tags=req.tags,
    query_embedding=req.query_embedding,
  )
  return {
    "status": "ok",
    "results": [
      {"policy": serialize_policy(record), "score": score}
      for record, score in results
    ],
  }


@app.post("/policies/{policy_id}/runs")
async def policies_add_run(policy_id: str, req: PolicyRunRequest):
  if not req.kpis:
    raise HTTPException(400, "kpis field is required to record a run.")
  try:
    record = POLICIES.add_run(
      policy_id,
      workload_label=req.workload_label,
      kpis=req.kpis,
      notes=req.notes,
    )
  except KeyError as exc:
    raise HTTPException(404, str(exc)) from exc
  return {"status": "ok", "policy": serialize_policy(record)}


@app.post("/policies/{policy_id}/verify")
async def policies_verify(policy_id: str, req: PolicyVerifyRequest):
  record = POLICIES.get(policy_id)
  if not record:
    raise HTTPException(404, f"Policy not found: {policy_id}")
  if not req.metrics:
    raise HTTPException(400, "metrics payload is required for verification.")
  overrides = req.guardrail_overrides or {}
  metrics = req.metrics
  violations = evaluate_guardrails(record, metrics=metrics, overrides=overrides)
  if violations:
    return {
      "status": "rejected",
      "violations": violations,
      "metrics": metrics,
    }
  token = f"verify-{uuid4().hex}"
  updated = POLICIES.register_verification(policy_id, token)
  if req.dry_run_notes:
    notes = list(updated.notes)
    notes.append(req.dry_run_notes)
    updated = POLICIES.update_metadata(policy_id, notes=notes)
  return {
    "status": "ok",
    "activation_token": token,
    "policy": serialize_policy(updated),
  }


@app.post("/jobs/run")
async def jobs_record(req: JobRunRecordRequest):
  job = JOBS.upsert(
    job_id=req.job_id,
    workload=req.workload,
    policy_id=req.policy_id,
    status=req.status,
    submitted_at=_strip_tz(req.submitted_at),
    started_at=_strip_tz(req.started_at),
    finished_at=_strip_tz(req.finished_at),
    metrics=req.metrics,
    tags=req.tags,
    notes=req.notes,
  )
  return {"status": "ok", "job": serialize_job(job)}


@app.post("/jobs/recent")
async def jobs_recent(req: JobRecentQuery):
  jobs = JOBS.recent(limit=req.limit)
  return {"status": "ok", "jobs": [serialize_job(job) for job in jobs]}


@app.get("/jobs/{job_id}")
async def jobs_get(job_id: str):
  job = JOBS.get(job_id)
  if not job:
    raise HTTPException(404, f"Job not found: {job_id}")
  return {"status": "ok", "job": serialize_job(job)}


@app.post("/scheduler/assign")
async def scheduler_assign(req: ScheduleBatchRequest):
  if not req.jobs:
    raise HTTPException(400, "jobs list cannot be empty.")

  profile_request = WorkloadProfileRequest(window=req.profile_window)
  profile = await build_workload_profile(profile_request)
  metrics = profile.get("cluster_metrics", {})
  workload_tags = {job.workload for job in req.jobs}

  candidates = []
  for record in POLICIES.list():
    if req.policy_tags and not set(req.policy_tags).intersection(set(record.target_workloads)):
      continue
    candidate = score_policy_for_batch(record, workload_tags=workload_tags, metrics=metrics)
    candidates.append(candidate)

  if not candidates:
    raise HTTPException(404, "No policies available for selection.")

  candidates.sort(key=lambda item: item["score"], reverse=True)
  best = candidates[0]
  selected_policy = best["policy"]

  assignments: List[Dict[str, Any]] = []
  if req.record_assignments:
    now = _utc_now()
    for descriptor in req.jobs:
      job_record = JOBS.upsert(
        job_id=descriptor.job_id,
        workload=descriptor.workload,
        policy_id=selected_policy.id,
        status="scheduled",
        submitted_at=now,
        started_at=None,
        finished_at=None,
        metrics=descriptor.hints,
        tags=descriptor.tags,
        notes=None,
      )
      assignments.append(serialize_job(job_record))
  else:
    for descriptor in req.jobs:
      assignments.append(
        {
          "job_id": descriptor.job_id,
          "workload": descriptor.workload,
          "policy_id": selected_policy.id,
        }
      )

  # Prepare alternative suggestions (top 3)
  alternatives = []
  for candidate in candidates[1:4]:
    record = candidate["policy"]
    alternatives.append(
      {
        "policy_id": record.id,
        "policy_name": record.name,
        "score": candidate["score"],
        "overlap": candidate["overlap"],
        "guardrail_penalties": candidate["guardrail_penalties"],
      }
    )

  response = {
    "status": "ok",
    "policy": serialize_policy(selected_policy),
    "score": best["score"],
    "overlap": best["overlap"],
    "guardrail_penalties": best["guardrail_penalties"],
    "assignments": assignments,
    "alternatives": alternatives,
    "workload_profile": profile,
  }
  return response


class CanRunRequest(BaseModel):
  host: Optional[str] = None
  duration: str = "45m"
  requirements: Dict[str, float]


def offpeak_1am_local() -> str:
  timezone = pytz.timezone("America/Chicago")
  now = datetime.now(timezone)
  candidate = now.replace(hour=1, minute=0, second=0, microsecond=0)
  if now.hour >= 1:
    candidate += timedelta(days=1)
  return candidate.isoformat()


@app.post("/advice/can_run")
async def advice_can_run(req: CanRunRequest):
  cpu_headroom_query = (
    '100 - (100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m]))))'
  )
  mem_free_query = "node_memory_MemAvailable_bytes"
  disk_free_query = (
    'max by (instance) (node_filesystem_free_bytes{fstype!~"tmpfs|overlay"})'
  )
  cpu = await fetch_scalar_map(cpu_headroom_query)
  mem = await fetch_scalar_map(mem_free_query)
  disk = await fetch_scalar_map(disk_free_query)

  instances = [req.host] if req.host else sorted(set(cpu) | set(mem) | set(disk))
  need_cpu = req.requirements.get("cpu_pct", 0)
  need_mem = req.requirements.get("mem_bytes", 0)
  need_disk = req.requirements.get("disk_free_bytes", 0)

  results = []
  for instance in instances:
    reasons: List[str] = []
    can_run = True
    if cpu.get(instance, 0) < need_cpu:
      can_run = False
      reasons.append(f"CPU headroom {cpu.get(instance, 0):.1f}% < need {need_cpu}%")
    if mem.get(instance, 0) < need_mem:
      can_run = False
      reasons.append(f"Mem free {int(mem.get(instance, 0))} < need {int(need_mem)}")
    if disk.get(instance, 0) < need_disk:
      can_run = False
      reasons.append(f"Disk free {int(disk.get(instance, 0))} < need {int(need_disk)}")
    results.append(
      {
        "instance": instance,
        "can_run_now": can_run,
        "reasons": reasons,
        "headroom_now": {
          "cpu_pct": cpu.get(instance),
          "mem_bytes": mem.get(instance),
          "disk_free_bytes": disk.get(instance),
        },
      }
    )
  return {"status": "ok", "results": results}


class SuggestWindowRequest(BaseModel):
  host: Optional[str] = None
  duration: str = "45m"
  horizon_hours: int = 24
  requirements: Dict[str, float]


@app.post("/advice/suggest_window")
async def advice_suggest_window(req: SuggestWindowRequest):
  cpu_busy_p95_query = (
    "quantile_over_time(0.95, (100 * (1 - avg by (instance) "
    "(rate(node_cpu_seconds_total{mode=\"idle\"}[5m]))))[1h:1m])"
  )
  mem_free_p05_query = 'quantile_over_time(0.05, node_memory_MemAvailable_bytes[1h])'
  disk_free_p05_query = (
    'quantile_over_time(0.05, node_filesystem_free_bytes{fstype!~"tmpfs|overlay"}[1h])'
  )
  cpu_busy = await fetch_scalar_map(cpu_busy_p95_query)
  mem_p05 = await fetch_scalar_map(mem_free_p05_query)
  disk_p05 = await fetch_scalar_map(disk_free_p05_query)

  need_cpu = req.requirements.get("cpu_pct", 0)
  need_mem = req.requirements.get("mem_bytes", 0)
  need_disk = req.requirements.get("disk_free_bytes", 0)
  cpu_headroom = {key: max(0.0, 100.0 - value) for key, value in cpu_busy.items()}

  instances = [req.host] if req.host else sorted(set(cpu_headroom) | set(mem_p05) | set(disk_p05))
  results = []
  for instance in instances:
    windows = []
    if (
      cpu_headroom.get(instance, 0) >= need_cpu
      and mem_p05.get(instance, 0) >= need_mem
      and disk_p05.get(instance, 0) >= need_disk
    ):
      windows.append(
        {
          "start": "now",
          "duration": req.duration,
          "reason": "p95 headroom sufficient",
        }
      )
    windows.append(
      {
        "start": offpeak_1am_local(),
        "duration": req.duration,
        "reason": "typical off-peak (1–4am)",
      }
    )
    results.append({"instance": instance, "windows": windows})
  return {"status": "ok", "results": results}
