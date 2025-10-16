"""Minimal scheduler service that delegates policy decisions to EdgePilot."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .scheduler_service import SchedulerClient


class JobSubmission(BaseModel):
  job_id: str
  workload: str
  tags: List[str] = Field(default_factory=list)


class SubmitJobsRequest(BaseModel):
  jobs: List[JobSubmission]
  profile_window: str = "15m"
  policy_tags: List[str] = Field(default_factory=list)


class JobLifecycleUpdate(BaseModel):
  metrics: Dict[str, float] = Field(default_factory=dict)
  notes: Optional[str] = None


class JobRecord(BaseModel):
  job_id: str
  workload: str
  policy_id: str
  status: str
  submitted_at: datetime
  started_at: Optional[datetime] = None
  finished_at: Optional[datetime] = None
  metrics: Dict[str, float] = Field(default_factory=dict)
  notes: Optional[str] = None


app = FastAPI(title="Mock Scheduler Service", description="Demonstrates SchedulerClient integration.")
client = SchedulerClient()
JOB_STATE: Dict[str, JobRecord] = {}


def _utc_now() -> datetime:
  return datetime.utcnow()


@app.post("/jobs/submit", response_model=Dict[str, JobRecord])
async def submit_jobs(req: SubmitJobsRequest):
  if not req.jobs:
    raise HTTPException(400, "jobs list cannot be empty.")

  job_payload = [{"job_id": job.job_id, "workload": job.workload, "tags": job.tags} for job in req.jobs]
  assignment = client.assign_jobs(
    job_payload,
    profile_window=req.profile_window,
    policy_tags=req.policy_tags,
    record_assignments=True,
  )
  policy_id = assignment["policy"]["id"]
  now = _utc_now()

  recorded: Dict[str, JobRecord] = {}
  for job in req.jobs:
    JOB_STATE[job.job_id] = JobRecord(
      job_id=job.job_id,
      workload=job.workload,
      policy_id=policy_id,
      status="scheduled",
      submitted_at=now,
    )
    recorded[job.job_id] = JOB_STATE[job.job_id]
  return recorded


@app.post("/jobs/{job_id}/start", response_model=JobRecord)
async def start_job(job_id: str):
  record = JOB_STATE.get(job_id)
  if not record:
    raise HTTPException(404, f"Unknown job_id {job_id}")
  if record.started_at:
    raise HTTPException(409, "Job already started.")
  record.started_at = _utc_now()
  record.status = "running"
  JOB_STATE[job_id] = record
  client.update_job(
    job_id=record.job_id,
    workload=record.workload,
    policy_id=record.policy_id,
    status="running",
    started_at=record.started_at.isoformat(),
    submitted_at=record.submitted_at.isoformat(),
  )
  return record


@app.post("/jobs/{job_id}/finish", response_model=JobRecord)
async def finish_job(job_id: str, update: JobLifecycleUpdate, *, success: bool = True):
  record = JOB_STATE.get(job_id)
  if not record:
    raise HTTPException(404, f"Unknown job_id {job_id}")
  record.finished_at = _utc_now()
  record.status = "succeeded" if success else "failed"
  record.metrics = update.metrics
  record.notes = update.notes
  JOB_STATE[job_id] = record

  client.update_job(
    job_id=record.job_id,
    workload=record.workload,
    policy_id=record.policy_id,
    status=record.status,
    submitted_at=record.submitted_at.isoformat(),
    started_at=record.started_at.isoformat() if record.started_at else None,
    finished_at=record.finished_at.isoformat(),
    metrics=update.metrics,
    notes=update.notes,
  )

  if update.metrics:
    client.record_policy_run(
      record.policy_id,
      kpis=update.metrics,
      workload_label=record.workload,
      notes=update.notes,
    )
  return record


@app.get("/jobs", response_model=List[JobRecord])
async def list_jobs():
  return list(JOB_STATE.values())


@app.delete("/jobs/reset")
async def reset_jobs():
  JOB_STATE.clear()
  return {"status": "ok", "message": "job state cleared"}
