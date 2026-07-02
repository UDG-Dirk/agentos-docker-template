"""
HELIX Figma Extractor Workflow
==============================

Single-step AgentOS Workflow wrapping the Figma Extractor agent (HELIX UC2
pipeline Step 1), with:

  * a deterministic RETRY-ON-EMPTY guard, and
  * a human-in-the-loop review gate (``requires_output_review=True``).

Why the guard: the extractor is non-deterministic — the model sometimes finalizes
right after the Phase-1 discovery call and returns an EMPTY result (0 tokens,
0 components) despite the prompt's ">=7 calls / empty = FAILURE" rule. This was
observed on DEV (run_sequential_001: 1 MCP call, empty) and again on the first
prod run. ``Step.max_retries`` does NOT help — an empty result is a *successful*
run, not an error. So the step's executor is a function that re-invokes the agent
(with a stronger continuation nudge) until the extraction is non-empty or the
attempt budget is exhausted. The HITL gate then reviews the final result.

Registered in ``app/main.py`` via ``AgentOS(workflows=[...])``.
"""

from __future__ import annotations

from agno.workflow import Step, Workflow
from agno.workflow.types import StepInput, StepOutput

from agents.figma_extractor.agent import figma_extractor_agent
from agents.figma_extractor.models import FigmaExtractionResult
from db import get_postgres_db

MAX_EXTRACTION_ATTEMPTS = 3

_CONTINUE_NUDGE = (
    "\n\nCRITICAL — a previous attempt returned an EMPTY extraction (no tokens, no "
    "components) after essentially only the discovery call. That is a FAILURE. Do NOT "
    "stop after discovery. You MUST make at least 7 get_figma_data calls: one discovery, "
    "then one per foundation page (Typography, Colors, Layout, Icons, ImageRatios, Text) "
    "and one per priority component (Button, Input, Toggle, Checkbox, Dropdown, FormField). "
    "Keep calling tools until BOTH tokens AND components are populated, then assemble the "
    "FigmaExtractionResult."
)


def _coerce(content) -> FigmaExtractionResult | None:
    """Best-effort coerce an agent response payload to FigmaExtractionResult."""
    if isinstance(content, FigmaExtractionResult):
        return content
    try:
        if hasattr(content, "model_dump"):  # another pydantic model
            return FigmaExtractionResult(**content.model_dump())
        if isinstance(content, dict):
            return FigmaExtractionResult(**content)
    except Exception:
        return None
    return None


def _is_empty(result: FigmaExtractionResult | None) -> bool:
    """A discovery-only / premature-finalize result: pages found but nothing extracted."""
    if result is None:
        return True
    return not result.tokens and not result.components


async def extract_with_retry(step_input: StepInput, **kwargs) -> StepOutput:
    """Run the extractor, retrying while the result is empty (premature finalize)."""
    base_input = step_input.input or step_input.previous_step_content or ""
    session_id = getattr(getattr(step_input, "workflow_session", None), "session_id", None)

    last_result: FigmaExtractionResult | None = None
    for attempt in range(1, MAX_EXTRACTION_ATTEMPTS + 1):
        message = base_input if attempt == 1 else f"{base_input}{_CONTINUE_NUDGE}"
        run_kwargs = {"input": message}
        if session_id:
            run_kwargs["session_id"] = session_id
        response = await figma_extractor_agent.arun(**run_kwargs)
        last_result = _coerce(getattr(response, "content", response))
        if not _is_empty(last_result):
            return StepOutput(content=last_result)  # non-empty extraction -> success

    # All attempts empty: surface the last result, flagged, for human review.
    if last_result is not None:
        note = (
            f"extraction returned empty (no tokens/components) after "
            f"{MAX_EXTRACTION_ATTEMPTS} attempts — premature finalize not recovered"
        )
        if note not in last_result.gaps_detected:
            last_result.gaps_detected.append(note)
        return StepOutput(content=last_result, success=False)
    return StepOutput(content=None, success=False, error="extraction produced no parseable result")


extract_step = Step(
    name="extract",
    executor=extract_with_retry,
    requires_output_review=True,  # HITL gate on the (retried) extraction result
)

helix_figma_extractor_workflow = Workflow(
    id="helix-figma-extractor",
    name="HELIX Figma Extractor",
    description="Extract design tokens, components, and variant matrices from a Figma file (retry-on-empty + HITL-reviewed).",
    db=get_postgres_db(),
    steps=[extract_step],
)
