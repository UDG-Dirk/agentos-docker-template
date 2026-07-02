#!/usr/bin/env python3
"""Figma Extractor as HELIX Workflow Step 1 + AgentOS registration.

Wraps the extractor (agent.py) in an Agno Workflow Step via a function executor,
mirroring the S2 plumbing spike. Downstream steps are pass-through stubs (we are
validating Step 1 only). Pattern (sequential|broadcast) is read from session_state
key 'extractor_pattern' (default sequential).

--check : register in AgentOS + in-process TestClient GET /workflows (no LLM run).

Run:
  cd ~/opencode/workbench/agno-setup/poc-agno-template
  .venv/bin/dotenv run -- .venv/bin/python \
     ~/opencode/workbench/helix-poc-agno/agents/figma_extractor/workflow_step.py --check
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agno.workflow import Step, Workflow            # noqa: E402
from agno.workflow.types import StepInput, StepOutput  # noqa: E402

from agent import DEFAULT_FILE_KEY, _default_session_state, _run  # noqa: E402

WF_ID = "helix-figma-extractor"
RUNS_DIR = Path(__file__).resolve().parent / "runs"


def _workflow_db():
    """HITL/persisted sessions need a db (reuse template agentos-db). None if unavailable."""
    try:
        tmpl = Path(__file__).resolve().parents[3] / "agno-setup" / "poc-agno-template"
        if str(tmpl) not in sys.path:
            sys.path.insert(0, str(tmpl))
        from db import get_postgres_db  # type: ignore
        return get_postgres_db()
    except Exception as e:  # pragma: no cover
        print(f"[warn] no workflow db ({e})", file=sys.stderr)
        return None


def _figma_extraction_executor(step_input: StepInput) -> StepOutput:
    """Step 1 executor — runs the Figma Extractor and returns FigmaExtractionResult."""
    ss = getattr(step_input, "session_state", None) or {}
    file_key = ss.get("figma_file_key", DEFAULT_FILE_KEY)
    pattern = ss.get("extractor_pattern", "sequential")
    out_path = RUNS_DIR / f"workflow_step1_{pattern}.json"
    # run the async extractor on a fresh loop (executor is sync, called outside an active loop)
    try:
        loop = asyncio.new_event_loop()
        try:
            payload = loop.run_until_complete(_run(pattern, file_key, ss, out_path))
        finally:
            loop.close()
        from models import FigmaExtractionResult  # type: ignore
        result = FigmaExtractionResult(**payload["result"])
        return StepOutput(content=result)
    except Exception as e:  # pragma: no cover
        return StepOutput(content=f"figma extraction failed: {e!r}", success=False, stop=True)


def _passthrough(step_input: StepInput) -> StepOutput:
    """Downstream stub (Token Normalizer / Scaffolder land here later)."""
    return StepOutput(content=getattr(step_input, "previous_step_content", None))


figma_extraction_step = Step(name="figma_extraction", executor=_figma_extraction_executor)
token_normalization_step = Step(name="token_normalization", executor=_passthrough)

helix_figma_workflow = Workflow(
    id=WF_ID,
    name=WF_ID,
    description="HELIX UC2 — Figma Extractor (Step 1) + pass-through stub. First production agent.",
    db=_workflow_db(),
    steps=[figma_extraction_step, token_normalization_step],
)


def _check() -> dict:
    from agno.os import AgentOS
    from fastapi.testclient import TestClient
    os_app = AgentOS(name="helix-figma-extractor-probe", db=_workflow_db(),
                     workflows=[helix_figma_workflow], authorization=False)
    client = TestClient(os_app.get_app())
    r = client.get("/workflows")
    listed = r.json() if r.status_code == 200 else r.text
    ids = [w.get("id") for w in listed] if isinstance(listed, list) else listed
    findings = {
        "GET /workflows status": r.status_code,
        "workflow_registered": WF_ID in (ids or []),
        "workflow_ids": ids,
    }
    out = RUNS_DIR / "workflow_register_check.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(findings, indent=2, default=str))
    print(json.dumps(findings, indent=2, default=str))
    return findings


if __name__ == "__main__":
    if "--check" in sys.argv:
        _check()
    else:
        print("Importable as a registered Workflow. Use --check for registration smoke test.")
        print(f"session_state template: {_default_session_state(DEFAULT_FILE_KEY)}")
