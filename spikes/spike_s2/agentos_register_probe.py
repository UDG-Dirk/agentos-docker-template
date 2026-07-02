"""
Spike S2 — AgentOS registration + REST invocation probe (KEY TEST #6).

Proves the workflow can be registered in AgentOS and invoked over the REST API,
WITHOUT modifying poc-agno-template source. Builds an AgentOS(workflows=[...]),
gets its FastAPI app, and drives it in-process with TestClient:
  GET  /workflows                      -> our workflow is listed
  POST /workflows/{id}/runs            -> start a run (will pause at HITL gate)
  GET  /workflows/{id}/runs/{run_id}   -> observe paused state
  POST /workflows/{id}/runs/{run_id}/continue -> resume (approve)

Run:
  cd ~/opencode/workbench/agno-setup/poc-agno-template
  .venv/bin/dotenv run -- .venv/bin/python /home/dirk/opencode/workbench/helix-poc-agno/spike_s2/agentos_register_probe.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HELIX = Path("/home/dirk/opencode/workbench/helix-poc-agno/spike_s2")
if str(HELIX) not in sys.path:
    sys.path.insert(0, str(HELIX))

from agno.os import AgentOS
from fastapi.testclient import TestClient

from models import ProjectConfig  # type: ignore
from workflow_code import helix_workflow, _workflow_db  # type: ignore

OUT = HELIX / "agentos_register_result.json"
WF_ID = "helix-pipeline-plumbing-test"

findings: dict = {}

agent_os = AgentOS(
    name="S2-registration-probe",
    db=_workflow_db(),
    workflows=[helix_workflow],   # <-- REGISTRATION pattern
    authorization=False,
)
app = agent_os.get_app()
client = TestClient(app)

# 1) listed?
r = client.get("/workflows")
listed = r.json() if r.status_code == 200 else r.text
ids = [w.get("id") for w in listed] if isinstance(listed, list) else listed
findings["GET /workflows status"] = r.status_code
findings["workflow_registered"] = WF_ID in (ids or [])
findings["workflow_ids"] = ids

# 2) start a run (multipart form; session_state as JSON string)
cfg = ProjectConfig(
    figma_file_key="8qPSyetzviLR6eF6bkpL44",
    figma_pat="dummy_pat_not_real",
    storybook_repo_url="https://rmvc01.rm.udg.de/test/helix-storybook",
    cms_repo_url="https://rmvc01.rm.udg.de/test/helix-cms-models",
    cms_type="storyblok",
    framework="vue",
)
data = {
    "message": "Run HELIX pipeline via REST (dummy).",   # AgentOS REST field is 'message', not 'input'
    "session_state": json.dumps(cfg.model_dump()),
    "stream": "false",
}
rr = client.post(f"/workflows/{WF_ID}/runs", data=data)
findings["POST runs status"] = rr.status_code
try:
    body = rr.json()
except Exception:
    body = {"raw": rr.text[:500]}
findings["run_status"] = body.get("status") if isinstance(body, dict) else None
findings["run_is_paused"] = (findings["run_status"] in ("PAUSED", "paused"))
findings["run_id"] = body.get("run_id") if isinstance(body, dict) else None
findings["post_runs_body_head"] = json.dumps(body, default=str)[:500]

OUT.write_text(json.dumps(findings, indent=2, default=str))
print(json.dumps(findings, indent=2, default=str))
print(f"[done] wrote {OUT}")
