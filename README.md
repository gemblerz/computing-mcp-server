# EdgePilot Architecture Overview

![Screenshot](docs/assets/final_architecture.png)

## Layers

### User Side
- **Usage Alerts**: trigger install or follow-up investigations.
- **Install Wizard**: guides through LLM selection (Claude), cluster/local install, and access requirements.
- **Prompts**: natural-language questions typed into the UI/CLI.

### Frontend (Streamlit UI / CLI)
- Collects user questions, displays responses, recommended actions, and raw facts.
- Tracks LLM usage, latency, and alerting metrics.

### Planner (Claude)
- Converts questions into structured JSON actions (`report`, `can_run`, `suggest_window`, `run_task`, etc.).
- Ensures output schema compliance before handing off to EdgePilot API.

### Backend
- **FastAPI (`edgepilot.app`)** loads the ring/blueprint metadata, composes PromQL, and orchestrates task execution.
- **Metric Reporting** surfaces API health, execution duration, and scheduling outcomes back to the UI.

### Tool Calls
- **Metrics Tooling**: queries Prometheus via PromQL for host/container signals.
- **Task Runner**: optional execution of experiments or remediation commands.

### Systems / External Calls
- **Prometheus & exporters** supply the deterministic metrics (node-exporter, cAdvisor, etc.).
- **Shell Commands / Scripts** allow controlled remediation or load tests when requested.

### Summarizer (Claude)
- Crafts the final narrative answer by combining the original question, planner output, and factual data.
- Adds recommended actions and confidence scores when available.

## Data Flow Summary
1. User installs EdgePilot, selects LLM (Claude Haiku), and launches the Streamlit UI.
2. A question is sent to the planner LLM; JSON action is returned.
3. EdgePilot API executes the action: queries Prometheus, schedules tasks, or runs shell workflows.
4. Raw facts and task results are streamed back to the UI and summarized by Claude.
5. The UI logs metrics and usage alerts, closing the loop for future sessions.

## Environment & Configuration
- Credentials are managed via `.env` (`ANTHROPIC_API_KEY`, `CLAUDE_MODEL=claude-3-5-haiku-20241022`, `EDGE_BASE_URL`).
- `infra/docker-compose.yml` runs Prometheus, exporters, Grafana, and EdgePilot dependencies locally.
- `edgepilot/pipeline.py` houses reusable planner/executor/summarizer helpers leveraged by Streamlit and CLI flows.

## Streamlit Quick Start

1. **Install prerequisites**
   - Docker Desktop (Mac/Windows) or Docker Engine (Linux).
   - Python 3.8+ (the repo ships with a virtualenv in `edgepilot/.venv`).

2. **Clone the repository & enter the project**
   ```bash
   git clone <repo-url>
   cd Practicum
   ```

3. **Bootstrap the monitoring stack**
   ```bash
   docker-compose -f infra/docker-compose.yml pull
   docker-compose -f infra/docker-compose.yml up -d
   ```
   This launches Prometheus, node-exporter, cAdvisor, Grafana, and Alertmanager on the `monitoring` network.

4. **Create / activate the virtual environment**
   ```bash
   python3 -m venv edgepilot/.venv   # skip if already checked in
   source edgepilot/.venv/bin/activate
   pip install -r edgepilot/requirements.txt
   ```

5. **Configure environment variables**
   - Copy `.env` and populate:
     ```bash
     cp .env.example .env   # if provided; otherwise edit .env directly
     ```
   - Ensure the following variables are set (values shown for reference):
     ```bash
     export ANTHROPIC_API_KEY=sk-ant-...
     export CLAUDE_MODEL=claude-3-5-haiku-20241022
     export CLAUDE_MAX_TOKENS=1024
     export EDGE_BASE_URL=http://127.0.0.1:5057
     ```
   - To load automatically: `set -a && source .env && set +a` before each session, or place the exports in `~/.zshrc`.

6. **Start the EdgePilot API**
   ```bash
   uvicorn edgepilot.app:app --reload --port 5057
   ```
   Leave this process running (or use a supervisor such as `pm2`/`honcho`).

7. **Launch the Streamlit assistant** (new terminal, same venv/env)
   ```bash
   source edgepilot/.venv/bin/activate
   set -a && source .env && set +a
   streamlit run edgepilot/streamlit_app.py
   ```
   Open the URL shown in the terminal (`http://localhost:8501` by default).

8. **Optional: Grafana login**
   - Navigate to `http://localhost:3000`
   - Default credentials: `admin / admin123`
   - Add Prometheus data source: `http://prometheus:9090`

9. **Shut down services**
   ```bash
   docker-compose -f infra/docker-compose.yml down
   ```

## Troubleshooting

- `ModuleNotFoundError: edgepilot` when running Streamlit: ensure `edgepilot/streamlit_app.py` is invoked from project root (it adjusts `sys.path`), and the venv is active.
- Prometheus can’t reach Alertmanager: confirm the container is running (`docker ps`) and `prometheus/prometheus.yml` targets `alertmanager:9093`.
- Claude API errors (`model not found`, `missing API key`): double-check `.env` values and run `echo $ANTHROPIC_API_KEY` before launching the apps.
