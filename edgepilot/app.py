import json
import math
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx
import pytz
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

PROM_URL = os.getenv("PROM_URL", "http://localhost:9090")
with open("edgepilot/ring.edgepilot.json", "r", encoding="utf-8") as f:
  RING = json.load(f)
with open("edgepilot/blueprint.edge_status.json", "r", encoding="utf-8") as f:
  BLUEPRINT = json.load(f)

app = FastAPI(title="EdgePilot Facts API")


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
