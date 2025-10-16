"""Simple policy metadata store used by the scheduler optimizer prototype."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from typing import Dict, List, Optional, Tuple


def _utc_now() -> datetime:
  return datetime.utcnow().replace(tzinfo=None)


@dataclass
class PolicyRun:
  timestamp: datetime
  workload_label: Optional[str]
  kpis: Dict[str, float]
  notes: Optional[str] = None

  def to_dict(self) -> Dict[str, object]:
    return {
      "timestamp": self.timestamp.isoformat(),
      "workload_label": self.workload_label,
      "kpis": self.kpis,
      "notes": self.notes,
    }

  @classmethod
  def from_dict(cls, data: Dict[str, object]) -> "PolicyRun":
    return cls(
      timestamp=datetime.fromisoformat(str(data["timestamp"])),
      workload_label=data.get("workload_label"),
      kpis=dict(data.get("kpis", {})),
      notes=data.get("notes"),
    )


@dataclass
class PolicyRecord:
  id: str
  name: str
  description: str
  intent: str
  target_workloads: List[str] = field(default_factory=list)
  guardrails: List[str] = field(default_factory=list)
  embedding: List[float] = field(default_factory=list)
  kpis: Dict[str, float] = field(default_factory=dict)
  notes: List[str] = field(default_factory=list)
  created_at: datetime = field(default_factory=_utc_now)
  updated_at: datetime = field(default_factory=_utc_now)
  last_verified_at: Optional[datetime] = None
  last_activation_token: Optional[str] = None
  history: List[PolicyRun] = field(default_factory=list)

  def to_dict(self) -> Dict[str, object]:
    return {
      "id": self.id,
      "name": self.name,
      "description": self.description,
      "intent": self.intent,
      "target_workloads": list(self.target_workloads),
      "guardrails": list(self.guardrails),
      "embedding": list(self.embedding),
      "kpis": dict(self.kpis),
      "notes": list(self.notes),
      "created_at": self.created_at.isoformat(),
      "updated_at": self.updated_at.isoformat(),
      "last_verified_at": self.last_verified_at.isoformat() if self.last_verified_at else None,
      "last_activation_token": self.last_activation_token,
      "history": [run.to_dict() for run in self.history],
    }

  @classmethod
  def from_dict(cls, data: Dict[str, object]) -> "PolicyRecord":
    record = cls(
      id=str(data["id"]),
      name=str(data.get("name", "")),
      description=str(data.get("description", "")),
      intent=str(data.get("intent", "")),
      target_workloads=list(data.get("target_workloads", [])),
      guardrails=list(data.get("guardrails", [])),
      embedding=list(data.get("embedding", [])),
      kpis=dict(data.get("kpis", {})),
      notes=list(data.get("notes", [])),
      created_at=datetime.fromisoformat(str(data.get("created_at", _utc_now().isoformat()))),
      updated_at=datetime.fromisoformat(str(data.get("updated_at", _utc_now().isoformat()))),
      last_verified_at=(
        datetime.fromisoformat(str(data["last_verified_at"])) if data.get("last_verified_at") else None
      ),
      last_activation_token=data.get("last_activation_token"),
      history=[PolicyRun.from_dict(item) for item in data.get("history", [])],
    )
    return record


class PolicyStore:
  """Thread-safe, file-backed store for scheduler policies."""

  def __init__(self, path: str, *, seeds_path: Optional[str] = None):
    self._path = path
    self._seeds_path = seeds_path or os.getenv("POLICY_SEEDS_PATH", "edgepilot/policy_seeds.json")
    self._lock = Lock()
    self._policies: Dict[str, PolicyRecord] = {}
    self._load()
    self._ensure_seed_policies()

  def _load(self) -> None:
    if not os.path.exists(self._path):
      return
    with open(self._path, "r", encoding="utf-8") as f:
      data = json.load(f)
    policies = {}
    for entry in data.get("policies", []):
      record = PolicyRecord.from_dict(entry)
      policies[record.id] = record
    self._policies = policies

  def _ensure_seed_policies(self) -> None:
    if not self._seeds_path or not os.path.exists(self._seeds_path):
      return
    with open(self._seeds_path, "r", encoding="utf-8") as f:
      data = json.load(f)
    changed = False
    for entry in data.get("policies", []):
      record = PolicyRecord.from_dict(entry)
      if record.id not in self._policies:
        self._policies[record.id] = record
        changed = True
    if changed:
      self._save()

  def _save(self) -> None:
    payload = {"policies": [record.to_dict() for record in self._policies.values()]}
    tmp_path = f"{self._path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
      json.dump(payload, f, indent=2)
    os.replace(tmp_path, self._path)

  def list(self) -> List[PolicyRecord]:
    with self._lock:
      return [record for record in self._policies.values()]

  def get(self, policy_id: str) -> Optional[PolicyRecord]:
    with self._lock:
      return self._policies.get(policy_id)

  def upsert(self, record: PolicyRecord) -> PolicyRecord:
    with self._lock:
      record.updated_at = _utc_now()
      self._policies[record.id] = record
      self._save()
      return record

  def create(
    self,
    *,
    policy_id: str,
    name: str,
    description: str,
    intent: str,
    target_workloads: Optional[List[str]] = None,
    guardrails: Optional[List[str]] = None,
    embedding: Optional[List[float]] = None,
    kpis: Optional[Dict[str, float]] = None,
    notes: Optional[List[str]] = None,
  ) -> PolicyRecord:
    record = PolicyRecord(
      id=policy_id,
      name=name,
      description=description,
      intent=intent,
      target_workloads=target_workloads or [],
      guardrails=guardrails or [],
      embedding=embedding or [],
      kpis=kpis or {},
      notes=notes or [],
    )
    return self.upsert(record)

  def add_run(
    self,
    policy_id: str,
    *,
    workload_label: Optional[str],
    kpis: Dict[str, float],
    notes: Optional[str],
  ) -> PolicyRecord:
    with self._lock:
      record = self._policies.get(policy_id)
      if not record:
        raise KeyError(f"Policy not found: {policy_id}")
      record.history.append(PolicyRun(timestamp=_utc_now(), workload_label=workload_label, kpis=kpis, notes=notes))
      for key, value in kpis.items():
        record.kpis[key] = value
      record.updated_at = _utc_now()
      self._save()
      return record

  def update_metadata(
    self,
    policy_id: str,
    *,
    guardrails: Optional[List[str]] = None,
    embedding: Optional[List[float]] = None,
    notes: Optional[List[str]] = None,
  ) -> PolicyRecord:
    with self._lock:
      record = self._policies.get(policy_id)
      if not record:
        raise KeyError(f"Policy not found: {policy_id}")
      if guardrails is not None:
        record.guardrails = guardrails
      if embedding is not None:
        record.embedding = embedding
      if notes is not None:
        record.notes = notes
      record.updated_at = _utc_now()
      self._save()
      return record

  def register_verification(self, policy_id: str, token: str) -> PolicyRecord:
    with self._lock:
      record = self._policies.get(policy_id)
      if not record:
        raise KeyError(f"Policy not found: {policy_id}")
      record.last_verified_at = _utc_now()
      record.last_activation_token = token
      record.updated_at = _utc_now()
      self._save()
      return record

  def search(
    self,
    query: str,
    *,
    limit: int = 5,
    tags: Optional[List[str]] = None,
    query_embedding: Optional[List[float]] = None,
  ) -> List[Tuple[PolicyRecord, float]]:
    tags_lower = {tag.lower() for tag in (tags or [])}
    query_lower = query.lower().strip()
    query_tokens = set(query_lower.split())
    records = self.list()
    scored: List[Tuple[PolicyRecord, float]] = []
    for record in records:
      if tags_lower and not tags_lower.intersection({t.lower() for t in record.target_workloads}):
        continue
      text = " ".join(
        [
          record.name,
          record.description,
          record.intent,
          " ".join(record.target_workloads),
          " ".join(record.guardrails),
        ]
      ).lower()
      text_tokens = set(text.split())
      overlap = len(query_tokens & text_tokens) if query_tokens else 0
      token_score = overlap / max(len(query_tokens), 1) if query_tokens else 0.0

      embedding_score = 0.0
      if query_embedding and record.embedding and len(query_embedding) == len(record.embedding):
        dot = sum(a * b for a, b in zip(query_embedding, record.embedding))
        norm_q = math.sqrt(sum(a * a for a in query_embedding))
        norm_r = math.sqrt(sum(a * a for a in record.embedding))
        if norm_q > 0 and norm_r > 0:
          embedding_score = dot / (norm_q * norm_r)
      score = token_score * 0.6 + embedding_score * 0.4
      scored.append((record, score))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[:limit]
