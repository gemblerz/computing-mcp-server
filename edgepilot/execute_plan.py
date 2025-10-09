"""Execute an EdgePilot action plan passed as JSON."""

from __future__ import annotations

import argparse
import json

from edgepilot.pipeline import execute_plan


def main() -> None:
  parser = argparse.ArgumentParser(description="Execute an EdgePilot plan JSON string")
  parser.add_argument("plan", help="JSON plan from planner")
  args = parser.parse_args()
  plan = json.loads(args.plan)
  result = execute_plan(plan)
  print(json.dumps(result, indent=2))


if __name__ == "__main__":
  main()
