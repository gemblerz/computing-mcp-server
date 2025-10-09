"""Shared planner/executor/summarizer helpers for EdgePilot."""

from __future__ import annotations

import json
import os
from typing import Any, Dict

import httpx

EDGE_BASE_URL = os.getenv("EDGE_BASE_URL", "http://127.0.0.1:5057")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-3-5-haiku-20241022")
CLAUDE_MAX_TOKENS = int(os.getenv("CLAUDE_MAX_TOKENS", "512"))

_PLANNER_SYSTEM_PROMPT = (
  "Task: Convert a user question into exactly one JSON object from the allowed schema below.\n\n"
  "ALLOWED JSON SHAPES (choose exactly one; fill in values):\n"
  '{"action":"report","window":"1h","top_k":5,"filters":{}}\n'
  '{"action":"can_run","duration":"45m","requirements":{"cpu_pct":40,"mem_bytes":4e9,"disk_free_bytes":2e10},"host":null}\n'
  '{"action":"suggest_window","duration":"45m","horizon_hours":24,"requirements":{...},"host":null}\n'
  "If the question is ambiguous:\n"
  '{"action":"clarify","message":"..."}\n\n'
  "Output rules:\n"
  "- Reply with ONE minified JSON object only.\n"
  "- No code fences, no extra text, no explanations, no apologies, no pre/post-amble.\n"
  "- Use the exact key names shown.\n"
  "- If unsure which action applies, use {\"action\":\"clarify\",\"message\":\"...\"}.\n\n"
  "User question:\n{{USER_QUESTION}}"
)

_SUMMARIZER_SYSTEM_PROMPT = (
  "Given the user's question, the planner JSON, and the factual EdgePilot response,"
  " craft a concise summary highlighting metrics, blockers, and recommended next steps."
)

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


def _anthropic_headers() -> Dict[str, str]:
  api_key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY")
  if not api_key:
    raise RuntimeError("ANTHROPIC_API_KEY (or CLAUDE_API_KEY) is not set; cannot contact Claude API.")
  return {
    "x-api-key": api_key,
    "anthropic-version": os.getenv("ANTHROPIC_VERSION", "2023-06-01"),
    "content-type": "application/json",
  }


def plan_question(question: str, *, model: str | None = None) -> Dict[str, Any]:
  """Return a planner JSON dict for the given question."""
  payload = {
    "model": model or CLAUDE_MODEL,
    "system": _PLANNER_SYSTEM_PROMPT,
    "messages": [
      {
        "role": "user",
        "content": [
          {"type": "text", "text": question},
        ],
      }
    ],
    "temperature": 0,
    "max_tokens": CLAUDE_MAX_TOKENS,
  }
  response = httpx.post(_ANTHROPIC_URL, headers=_anthropic_headers(), json=payload, timeout=30.0)
  response.raise_for_status()
  data = response.json()
  text = "".join(
    block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
  ).strip()
  try:
    plan = json.loads(text)
  except json.JSONDecodeError as exc:
    raise ValueError(f"Planner returned non-JSON response: {text}") from exc
  return plan


def execute_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
  """Call the EdgePilot API according to the action in the plan."""
  action = plan.get("action")

  def _post(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    response = httpx.post(f"{EDGE_BASE_URL}{path}", json=payload, timeout=20.0)
    response.raise_for_status()
    return response.json()

  if action == "report":
    payload = {
      "report": plan.get("report", "edge_status"),
      "window": plan.get("window", "1h"),
      "top_k": plan.get("top_k", 5),
      "filters": plan.get("filters", {}),
    }
    return _post("/report/facts", payload)

  if action == "can_run":
    requirements = plan.get("requirements")
    if not isinstance(requirements, dict):
      raise ValueError("can_run action requires a requirements object")
    payload = {
      "requirements": requirements,
      "duration": plan.get("duration", "45m"),
    }
    if plan.get("host") is not None:
      payload["host"] = plan["host"]
    return _post("/advice/can_run", payload)

  if action == "suggest_window":
    requirements = plan.get("requirements")
    if not isinstance(requirements, dict):
      raise ValueError("suggest_window action requires a requirements object")
    payload = {
      "requirements": requirements,
      "duration": plan.get("duration", "45m"),
      "horizon_hours": plan.get("horizon_hours", 24),
    }
    if plan.get("host") is not None:
      payload["host"] = plan["host"]
    return _post("/advice/suggest_window", payload)

  if action == "clarify":
    return plan

  raise ValueError(f"Unsupported action: {action}")


def summarize(question: str, plan: Dict[str, Any], facts: Dict[str, Any], *, model: str | None = None) -> str:
  """Return a natural-language summary given question, plan, and facts."""
  if plan.get("action") == "clarify":
    return plan.get("message", "Clarification needed.")

  payload = json.dumps({"question": question, "plan": plan, "facts": facts})
  claude_payload = {
    "model": model or CLAUDE_MODEL,
    "system": _SUMMARIZER_SYSTEM_PROMPT,
    "messages": [
      {
        "role": "user",
        "content": [
          {"type": "text", "text": payload},
        ],
      }
    ],
    "temperature": 0.3,
    "max_tokens": CLAUDE_MAX_TOKENS,
  }
  response = httpx.post(_ANTHROPIC_URL, headers=_anthropic_headers(), json=claude_payload, timeout=30.0)
  response.raise_for_status()
  data = response.json()
  return "".join(
    block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
  ).strip()
