"""Persistent job run history used for workload profiling."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from typing import Dict, List, Optional


def _utc_now() -> datetime:
  return datetime.utcnow().replace(tzinfo=None)


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
  if not value:
    return None
  try:
    return datetime.fromisoformat(value)
  except ValueError:
    return None


@dataclass
class JobRun:
  job_id: str
  workload: str
  policy_id: Optional[str]
  status: str
  submitted_at: datetime
  started_at: Optional[datetime] = None
  finished_at: Optional[datetime] = None
  metrics: Dict[str, float] = field(default_factory=dict)
  tags: List[str] = field(default_factory=list)
  notes: Optional[str] = None

  def to_dict(self) -> Dict[str, object]:
    return {
      "job_id": self.job_id,
      "workload": self.workload,
      "policy_id": self.policy_id,
      "status": self.status,
      "submitted_at": self.submitted_at.isoformat(),
      "started_at": self.started_at.isoformat() if self.started_at else None,
      "finished_at": self.finished_at.isoformat() if self.finished_at else None,
      "metrics": dict(self.metrics),
      "tags": list(self.tags),
      "notes": self.notes,
    }

  @classmethod
  def from_dict(cls, data: Dict[str, object]) -> "JobRun":
    return cls(
      job_id=str(data.get("job_id")),
      workload=str(data.get("workload", "")),
      policy_id=data.get("policy_id"),
      status=str(data.get("status", "unknown")),
      submitted_at=_parse_timestamp(str(data.get("submitted_at"))) or _utc_now(),
      started_at=_parse_timestamp(data.get("started_at")),
      finished_at=_parse_timestamp(data.get("finished_at")),
      metrics=dict(data.get("metrics", {})),
      tags=list(data.get("tags", [])),
      notes=data.get("notes"),
    )


class JobStore:
  """Thread-safe, file-backed job run history."""

  def __init__(self, path: str):
    self._path = path
    self._lock = Lock()
    self._jobs: Dict[str, JobRun] = {}
    self._load()

  def _load(self) -> None:
    if not os.path.exists(self._path):
      self._jobs = {}
      return
    with open(self._path, "r", encoding="utf-8") as f:
      data = json.load(f)
    jobs = {}
    for entry in data.get("jobs", []):
      job = JobRun.from_dict(entry)
      jobs[job.job_id] = job
    self._jobs = jobs

  def _save(self) -> None:
    payload = {"jobs": [job.to_dict() for job in self._jobs.values()]}
    tmp_path = f"{self._path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
      json.dump(payload, f, indent=2)
    os.replace(tmp_path, self._path)

  def record(self, job: JobRun) -> JobRun:
    with self._lock:
      self._jobs[job.job_id] = job
      self._save()
      return job

  def upsert(
    self,
    *,
    job_id: str,
    workload: str,
    policy_id: Optional[str],
    status: str,
    submitted_at: datetime,
    started_at: Optional[datetime],
    finished_at: Optional[datetime],
    metrics: Optional[Dict[str, float]] = None,
    tags: Optional[List[str]] = None,
    notes: Optional[str] = None,
  ) -> JobRun:
    job = JobRun(
      job_id=job_id,
      workload=workload,
      policy_id=policy_id,
      status=status,
      submitted_at=submitted_at,
      started_at=started_at,
      finished_at=finished_at,
      metrics=metrics or {},
      tags=tags or [],
      notes=notes,
    )
    return self.record(job)

  def get(self, job_id: str) -> Optional[JobRun]:
    with self._lock:
      return self._jobs.get(job_id)

  def list(self) -> List[JobRun]:
    with self._lock:
      return list(self._jobs.values())

  def recent(self, limit: int = 20) -> List[JobRun]:
    def _sort_key(job: JobRun) -> datetime:
      return job.finished_at or job.started_at or job.submitted_at

    with self._lock:
      jobs = sorted(self._jobs.values(), key=_sort_key, reverse=True)
    return jobs[:limit]

  def status_counts(self) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    with self._lock:
      for job in self._jobs.values():
        counts[job.status] = counts.get(job.status, 0) + 1
    return counts

  def policy_usage(self) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    with self._lock:
      for job in self._jobs.values():
        if job.policy_id:
          counts[job.policy_id] = counts.get(job.policy_id, 0) + 1
    return counts
