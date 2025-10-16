"""Streamlit front-end for EdgePilot's planner/executor workflow and scheduler demo."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional

import streamlit as st

CURRENT_DIR = os.path.dirname(__file__)
ROOT_DIR = os.path.abspath(os.path.join(CURRENT_DIR, os.pardir))
if ROOT_DIR not in sys.path:
  sys.path.insert(0, ROOT_DIR)

from edgepilot.pipeline import execute_plan, plan_question, summarize
from edgepilot.scheduler_service import SchedulerClient

UPLOAD_DIR = Path(CURRENT_DIR) / "job_uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

POLICY_WRAPPERS = {
  "cfs_latency": str(Path(CURRENT_DIR) / "policies" / "apply_cfs_latency.sh"),
  "nice_boost": str(Path(CURRENT_DIR) / "policies" / "apply_nice_boost.sh"),
  "cpu_quota": str(Path(CURRENT_DIR) / "policies" / "apply_cpu_quota.sh"),
}


def run_local_job(record: Dict[str, Any]) -> Tuple[bool, Dict[str, float], Optional[str]]:
  """Execute the stored command for a job and return (success, metrics, notes)."""
  command = record.get("command")
  command_args = record.get("command_args")
  if not command and not command_args:
    return False, {}, "No command configured."

  if not command_args:
    command_args = shlex.split(command)

  wrapper = POLICY_WRAPPERS.get(record.get("policy_id"))
  if wrapper:
    command_args = [wrapper, *command_args]
  start = time.perf_counter()
  try:
    result = subprocess.run(command_args, capture_output=True, text=True)
  except Exception as exc:
    duration = time.perf_counter() - start
    return False, {"duration_s": round(duration, 3)}, f"Execution error: {exc}"
  duration = time.perf_counter() - start
  metrics = {
    "duration_s": round(duration, 3),
    "exit_code": result.returncode,
  }
  notes_parts = []
  if result.stdout:
    notes_parts.append(f"stdout: {result.stdout.strip()[:200]}")
  if result.stderr:
    notes_parts.append(f"stderr: {result.stderr.strip()[:200]}")
  if record.get("job_kind") == "shell" and result.returncode == 0:
    metrics.setdefault("p99_latency_ms", max(1.0, metrics["duration_s"] * 1000))
  success = result.returncode == 0
  notes = "\n".join(notes_parts) if notes_parts else None
  return success, metrics, notes

st.set_page_config(page_title="EdgePilot Assistant", page_icon="🛰️", layout="wide")
st.title("🛰️ EdgePilot Control Center")

with st.sidebar:
  st.header("Setup Checklist")
  st.markdown(
    "1. Launch the monitoring stack: `docker-compose up -d`.\n"
    "2. Start EdgePilot: `uvicorn edgepilot.app:app --reload --port 5057`.\n"
    "3. (Optional) Start mock scheduler: `uvicorn edgepilot.mock_scheduler:app --reload --port 5060`.\n"
    "4. Ensure `Claude API Key` is set (and `EDGE_BASE_URL` if remote).\n"
  )

assistant_tab, scheduler_tab = st.tabs(["Assistant", "Scheduler Demo"])

with assistant_tab:
  st.subheader("Chat with EdgePilot")
  if "turns" not in st.session_state:
    st.session_state.turns = []

  for turn in st.session_state.turns:
    st.chat_message("user").write(turn["question"])
    assistant_msg = st.chat_message("assistant")
    assistant_msg.write(turn["answer"])
    if turn.get("plan") and turn.get("facts"):
      with assistant_msg.expander("EdgePilot plan & facts", expanded=False):
        st.json(turn["plan"], expanded=False)
        st.json(turn["facts"], expanded=False)
    if turn.get("error"):
      st.error(turn["error"])

  prompt = st.chat_input("Ask about your edge environment…")
  if prompt:
    st.chat_message("user").write(prompt)
    with st.spinner("Planning and executing with EdgePilot…"):
      turn: Dict[str, Any] = {"question": prompt}
      try:
        plan = plan_question(prompt)
        if plan.get("action") == "clarify":
          answer = plan.get("message", "I need more detail to map this request.")
          turn["plan"] = plan
          turn["answer"] = answer
        else:
          facts = execute_plan(plan)
          answer = summarize(prompt, plan, facts)
          turn.update({"plan": plan, "facts": facts, "answer": answer})
      except Exception as exc:
        answer = f"Encountered an error: {exc}"
        turn["answer"] = answer
        turn["error"] = str(exc)
    assistant_box = st.chat_message("assistant")
    assistant_box.write(answer)
    if turn.get("plan") and turn.get("facts") and "error" not in turn:
      with assistant_box.expander("EdgePilot plan & facts", expanded=False):
        st.json(turn["plan"], expanded=False)
        st.json(turn["facts"], expanded=False)
    st.session_state.turns.append(turn)

with scheduler_tab:
  st.subheader("Scheduler Control Panel")
  client = SchedulerClient()

  if "pending_jobs" not in st.session_state:
    st.session_state.pending_jobs = []
  if "job_records" not in st.session_state:
    st.session_state.job_records = []
  if "last_assignment" not in st.session_state:
    st.session_state.last_assignment = None

  with st.form("add_job_form"):
    st.markdown("#### Queue a Job")
    new_job_id = st.text_input("Job ID", value="job-001")
    new_workload = st.text_input("Workload", value="web-frontend")
    job_kind = st.selectbox("Job Type", ["shell", "python-script"])
    script_upload = None
    if job_kind == "shell":
      command_input = st.text_input("Shell command", value="echo 'Hello EdgePilot'")
    else:
      command_input = ""
      script_upload = st.file_uploader("Python script (.py)", type=["py"])
    submitted = st.form_submit_button("Add to pending queue")
    if submitted:
      if not new_job_id or not new_workload:
        st.warning("Please supply both job id and workload.")
      else:
        job_entry: Dict[str, Any] = {
          "job_id": new_job_id,
          "workload": new_workload,
          "job_kind": job_kind,
        }
        if job_kind == "shell":
          if not command_input:
            st.warning("Please provide a shell command.")
            st.stop()
          job_entry["command"] = command_input
          job_entry["command_args"] = shlex.split(command_input)
          job_entry["display_command"] = command_input
        else:
          if not script_upload:
            st.warning("Upload a Python script before submitting.")
            st.stop()
          script_contents = script_upload.read()
          script_name = f"{new_job_id}-{int(time.time())}.py"
          script_path = UPLOAD_DIR / script_name
          script_path.write_bytes(script_contents)
          job_entry["command"] = f"{sys.executable} {script_path}"
          job_entry["command_args"] = [sys.executable, str(script_path)]
          job_entry["script_path"] = str(script_path)
          job_entry["display_command"] = job_entry["command"]
        st.session_state.pending_jobs.append(job_entry)
        st.success(f"Queued {new_job_id} ({job_kind})")

  if st.session_state.pending_jobs:
    st.markdown("#### Pending Jobs")
    pending_rows = []
    for job in st.session_state.pending_jobs:
      pending_rows.append(
        {
          "job_id": job["job_id"],
          "workload": job["workload"],
          "job_kind": job.get("job_kind"),
          "command": job.get("display_command", job.get("command")),
        }
      )
    st.table(pending_rows)
  else:
    st.info("No jobs pending assignment.")

  col_assign, col_reset = st.columns([3, 1])
  with col_assign:
    if st.button("Assign policy to pending jobs", disabled=not st.session_state.pending_jobs):
      payload = [
        {"job_id": job["job_id"], "workload": job["workload"]}
        for job in st.session_state.pending_jobs
      ]
      try:
        assignment = client.assign_jobs(payload)
        policy_id = assignment["policy"]["id"]
        now_iso = datetime.utcnow().isoformat()
        for job in st.session_state.pending_jobs:
          record = {
            "job_id": job["job_id"],
            "workload": job["workload"],
            "policy_id": policy_id,
            "status": "scheduled",
            "submitted_at": now_iso,
            "started_at": None,
            "finished_at": None,
            "metrics": {},
            "job_kind": job.get("job_kind", "shell"),
            "command": job.get("command"),
            "command_args": job.get("command_args"),
            "script_path": job.get("script_path"),
            "display_command": job.get("display_command", job.get("command")),
          }
          client.update_job(
            job_id=record["job_id"],
            workload=record["workload"],
            policy_id=record["policy_id"],
            status="scheduled",
            submitted_at=record["submitted_at"],
          )
          st.session_state.job_records.append(record)
        st.session_state.pending_jobs.clear()
        st.session_state.last_assignment = assignment
        st.success(f"Assigned policy {policy_id} to jobs")
      except Exception as exc:
        st.error(f"Failed to assign policy: {exc}")
  with col_reset:
    if st.button("Clear state"):
      st.session_state.pending_jobs.clear()
      st.session_state.job_records.clear()
      st.session_state.last_assignment = None
      st.success("Cleared scheduler state")

  if st.session_state.last_assignment:
    with st.expander("Last policy assignment details", expanded=False):
      st.json(st.session_state.last_assignment)

  if st.session_state.job_records:
    st.markdown("#### Managed Jobs")
    for record in st.session_state.job_records:
      cols = st.columns([1.5, 1.5, 1.5, 1.8, 1.2, 1.5])
      cols[0].markdown(f"**{record['job_id']}**")
      cols[1].markdown(record.get("job_kind", "shell"))
      cols[2].markdown(record["workload"])
      cols[3].markdown(record["policy_id"])
      cols[4].markdown(record["status"].upper())
      if record["status"] == "scheduled":
        if cols[5].button("Start & run", key=f"start-{record['job_id']}"):
          record["status"] = "running"
          record["started_at"] = datetime.utcnow().isoformat()
          client.update_job(
            job_id=record["job_id"],
            workload=record["workload"],
            policy_id=record["policy_id"],
            status="running",
            submitted_at=record["submitted_at"],
            started_at=record["started_at"],
          )
          success, metrics, notes = run_local_job(record)
          record["status"] = "succeeded" if success else "failed"
          record["finished_at"] = datetime.utcnow().isoformat()
          record["metrics"] = metrics
          if notes:
            record["notes"] = notes
          client.update_job(
            job_id=record["job_id"],
            workload=record["workload"],
            policy_id=record["policy_id"],
            status=record["status"],
            submitted_at=record["submitted_at"],
            started_at=record["started_at"],
            finished_at=record["finished_at"],
            metrics=metrics,
            notes=notes,
          )
          if metrics:
            client.record_policy_run(
              record["policy_id"],
              kpis=metrics,
              workload_label=record["workload"],
              notes=notes,
            )
          st.rerun()
      elif record["status"] == "running":
        with cols[5].form(f"finish-{record['job_id']}"):
          metrics_input = st.text_input("Metrics JSON", value='{"p99_latency_ms":40}')
          success = st.checkbox("Success", value=True)
          submitted_finish = st.form_submit_button("Finish")
          if submitted_finish:
            try:
              metrics = json.loads(metrics_input) if metrics_input else {}
            except json.JSONDecodeError as exc:
              st.error(f"Invalid metrics JSON: {exc}")
              st.stop()
            record["status"] = "succeeded" if success else "failed"
            record["finished_at"] = datetime.utcnow().isoformat()
            record["metrics"] = metrics
            client.update_job(
              job_id=record["job_id"],
              workload=record["workload"],
              policy_id=record["policy_id"],
              status=record["status"],
              submitted_at=record["submitted_at"],
              started_at=record["started_at"],
              finished_at=record["finished_at"],
              metrics=metrics,
            )
            if metrics:
              client.record_policy_run(
                record["policy_id"],
                kpis=metrics,
                workload_label=record["workload"],
              )
            st.rerun()
      else:
        cols[5].markdown("—")
    st.markdown("\n")
  else:
    st.info("No jobs have been assigned yet.")
