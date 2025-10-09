"""Streamlit front-end for EdgePilot's planner/executor workflow."""

from __future__ import annotations

from typing import Any, Dict

import os
import sys

import streamlit as st

CURRENT_DIR = os.path.dirname(__file__)
ROOT_DIR = os.path.abspath(os.path.join(CURRENT_DIR, os.pardir))
if ROOT_DIR not in sys.path:
  sys.path.insert(0, ROOT_DIR)

from edgepilot.pipeline import execute_plan, plan_question, summarize

st.set_page_config(page_title="EdgePilot Assistant", page_icon="🛰️", layout="wide")
st.title("🛰️ EdgePilot Assistant")

with st.sidebar:
  st.header("Setup Checklist")
  st.markdown(
    "1. Launch the monitoring stack: `docker-compose up -d`.\n"
    "2. Start EdgePilot: `uvicorn edgepilot.app:app --reload --port 5057`.\n"
    "3. Ensure `Claude API Key` is set (and `EDGE_BASE_URL` if remote).\n"
  )
  st.markdown("Once running, ask any question about your edge environment below.")

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
