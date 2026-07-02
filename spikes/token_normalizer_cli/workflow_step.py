#!/usr/bin/env python3
"""Token Normalizer as HELIX Workflow Step 2 (+ HITL output-review gate).

Thin Agno wrapper around the pure ``normalize_tokens`` function. Mirrors the S2
plumbing spike and ``figma_extractor/workflow_step.py``:

* ``Step(executor=fn)`` — function step, zero model calls (verified pattern, see
  lesson:agno-workflow-patterns).
* ``requires_output_review=True`` — function-step HITL is a Step-level property
  (verified: works for function steps, not agent-only). Pause exposes the
  NormalizationReport for human inspection before advancing.
* HARD REQUIREMENT: the Workflow must carry ``db=`` or ``continue_run`` raises
  ValueError on resume — we reuse the poc-agno-template ``agentos-db``.

Step 2 reads Step 1 output (adjacent) via ``step_input.previous_step_content``.

--check : register in AgentOS + in-process TestClient GET /workflows (no LLM run).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agno.workflow import Step, Workflow  # noqa: E402
from agno.workflow.types import StepInput, StepOutput  # noqa: E402

from normalizer import normalize_tokens  # noqa: E402

WF_ID = "helix-token-normalizer"
RUNS_DIR = Path(__file__).resolve().parent / "runs"
REVIEW_MESSAGE = (
    "Token normalization complete. Review the NormalizationReport: confidence "
    "distribution, type distribution, unresolved tokens, and composite "
    "decompositions before advancing to scaffolding."
)


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


def _token_normalization_executor(step_input: StepInput) -> StepOutput:
    """Step 2 executor — normalize the FigmaExtractionResult from Step 1."""
    extraction = getattr(step_input, "previous_step_content", None)
    if extraction is None:
        return StepOutput(
            content="token normalization failed: no Step 1 output (FigmaExtractionResult) found",
            success=False,
            stop=True,
        )
    try:
        result = normalize_tokens(extraction)
        return StepOutput(content=result)
    except Exception as e:  # pragma: no cover
        return StepOutput(content=f"token normalization failed: {e!r}", success=False, stop=True)


token_normalization_step = Step(
    name="token_normalization",
    executor=_token_normalization_executor,
    requires_output_review=True,
    output_review_message=REVIEW_MESSAGE,
)


def _build_workflow() -> Workflow:
    return Workflow(
        id=WF_ID,
        name=WF_ID,
        description="HELIX UC2 — Token Normalizer (Step 2), deterministic DTCG normalization with HITL gate.",
        db=_workflow_db(),
        steps=[token_normalization_step],
    )


helix_token_normalizer_workflow = _build_workflow()


def _check() -> dict:
    from agno.os import AgentOS
    from fastapi.testclient import TestClient

    os_app = AgentOS(
        name="helix-token-normalizer-probe",
        db=_workflow_db(),
        workflows=[helix_token_normalizer_workflow],
        authorization=False,
    )
    client = TestClient(os_app.get_app())
    r = client.get("/workflows")
    listed = r.json() if r.status_code == 200 else r.text
    ids = [w.get("id") for w in listed] if isinstance(listed, list) else listed
    findings = {
        "GET /workflows status": r.status_code,
        "workflow_registered": WF_ID in (ids or []),
        "workflow_ids": ids,
        "hitl_review_enabled": token_normalization_step.requires_output_review,
    }
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    (RUNS_DIR / "workflow_register_check.json").write_text(json.dumps(findings, indent=2, default=str))
    print(json.dumps(findings, indent=2, default=str))
    return findings


if __name__ == "__main__":
    if "--check" in sys.argv:
        _check()
    else:
        print("Importable as a registered Workflow. Use --check for registration smoke test.")
