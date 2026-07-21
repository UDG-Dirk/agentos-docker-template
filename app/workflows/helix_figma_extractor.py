"""
HELIX Figma Extractor Workflow (parallel: client-branch + baseline-branch)
==========================================================================

Two branches run in PARALLEL and converge into a downstream smoke-test step
(decision:cycle-2-wiring-2026-07-21):

  client branch (Steps "figma-extract-normalize"):
    Step 1 — extract  : Figma Extractor agent, wrapped in a retry-on-empty guard
                        (extract_with_retry). Non-deterministic model sometimes
                        finalizes after discovery with an empty result; the guard
                        re-invokes until tokens/components are non-empty.
    Step 2 — normalize: Token Normalizer (pure Python, zero LLM / zero MCP). Reads
                        Step 1's FigmaExtractionResult via previous_step_content,
                        converts to DTCG NormalizedTokens. HITL output-review gate
                        lives here — the reviewer sees the normalization_report.

  baseline branch (Agent 3a — "baseline-read"):
    Pull-on-invocation read of the helix-code baseline at a configurable
    ``baseline_ref`` (default "master" — helix-code's default branch), persisted
    to a Coolify volume. Output is
    CLIENT-INVARIANT and consumed by 3b/3c/3d from Workflow state — see
    agents/baseline_reader/step.py.

  converge (Step "baseline-access-smoke-test"):
    Reads 3a's output BY NAME across the Parallel boundary
    (``get_step_output("baseline-read")``) — the non-adjacent access pattern
    3b will use — asserts shape, and passes normalized tokens through.

Run-time parameter: pass ``additional_data={"baseline_ref": "<branch|tag|sha>"}``
to ``workflow.arun(...)`` to override the baseline ref (default "master").

Rollback: replace ``steps=[Parallel(...), smoke_test_step]`` with
``steps=[extract_step, normalize_step]`` to return to the sequential two-step
workflow — one revert. id unchanged.

Registered in ``app/main.py`` via ``AgentOS(workflows=[...])`` — id unchanged.
"""

from __future__ import annotations

import json

from agno.workflow import Parallel, Step, Steps, Workflow
from agno.workflow.types import StepInput, StepOutput

from agents.baseline_reader.step import (
    STEP_NAME_BASELINE,
    STEP_NAME_SMOKE,
    baseline_access_smoke_test_executor,
    baseline_read_executor,
)
from agents.figma_extractor.agent import figma_extractor_agent
from agents.figma_extractor.models import FigmaExtractionResult
from agents.token_normalizer.normalizer import FigmaExtractionResult as _NormalizerFER
from agents.token_normalizer.normalizer import normalize_tokens
from db import get_postgres_db

# ---------------------------------------------------------------------------
# Step 1 — extract (retry-on-empty guard)
# ---------------------------------------------------------------------------
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


def _coerce_figma(content) -> FigmaExtractionResult | None:
    if isinstance(content, FigmaExtractionResult):
        return content
    try:
        if hasattr(content, "model_dump"):
            return FigmaExtractionResult(**content.model_dump())
        if isinstance(content, dict):
            return FigmaExtractionResult(**content)
    except Exception:
        return None
    return None


def _is_empty(result: FigmaExtractionResult | None) -> bool:
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
        last_result = _coerce_figma(getattr(response, "content", response))
        if not _is_empty(last_result):
            return StepOutput(content=last_result)

    if last_result is not None:
        note = (
            f"extraction returned empty (no tokens/components) after "
            f"{MAX_EXTRACTION_ATTEMPTS} attempts — premature finalize not recovered"
        )
        if note not in last_result.gaps_detected:
            last_result.gaps_detected.append(note)
        return StepOutput(content=last_result, success=False)
    return StepOutput(content=None, success=False, error="extraction produced no parseable result")


# ---------------------------------------------------------------------------
# Step 2 — normalize (pure-Python DTCG normalization + HITL review)
# ---------------------------------------------------------------------------
_REVIEW_MESSAGE = (
    "Token normalization complete. Review the NormalizationReport: confidence "
    "distribution, type distribution, unresolved tokens, and composite "
    "decompositions before advancing."
)


def _coerce_extraction_for_normalizer(raw):
    """Coerce Step 1's output into the normalizer's FigmaExtractionResult, regardless
    of how agno delivers it across the step boundary (pydantic object, dict, or JSON
    string — the latter can occur after DB persist / continue-run resume)."""
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump()
    if isinstance(raw, dict):
        data = raw.get("result", raw)  # tolerate a {result: ...} wrapper
        try:
            return _NormalizerFER(**data)
        except Exception:
            return None
    # already a duck-typed extraction object with .tokens/.components
    return raw if hasattr(raw, "tokens") else None


def normalize_step_executor(step_input: StepInput, **kwargs) -> StepOutput:
    """Step 2 — normalize the FigmaExtractionResult produced by Step 1."""
    extraction = _coerce_extraction_for_normalizer(getattr(step_input, "previous_step_content", None))
    if extraction is None:
        return StepOutput(
            content="normalization failed: no usable FigmaExtractionResult from Step 1",
            success=False,
            stop=True,
        )
    try:
        normalized = normalize_tokens(extraction)
    except Exception as e:  # keep the pipeline observable rather than 500-ing
        return StepOutput(content=f"normalization failed: {e!r}", success=False, stop=True)
    return StepOutput(content=normalized.model_dump())


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------
extract_step = Step(
    name="extract",
    executor=extract_with_retry,
)

normalize_step = Step(
    name="normalize",
    executor=normalize_step_executor,
    requires_output_review=True,  # single HITL gate, on the final normalized output
    output_review_message=_REVIEW_MESSAGE,
)

# Agent 3a — Baseline Reader (pull-on-invocation; see agents/baseline_reader/step.py).
baseline_step = Step(
    name=STEP_NAME_BASELINE,
    executor=baseline_read_executor,
)

# Client branch: extract -> normalize, grouped so it runs as one parallel branch.
client_branch = Steps(
    name="figma-extract-normalize",
    steps=[extract_step, normalize_step],
)

# Converge: verify non-adjacent access to 3a's output (the pattern 3b will use).
smoke_test_step = Step(
    name=STEP_NAME_SMOKE,
    executor=baseline_access_smoke_test_executor,
)

helix_figma_extractor_workflow = Workflow(
    id="helix-figma-extractor",
    name="HELIX Figma Extractor",
    description=(
        "Parallel: (client) extract design tokens/components from Figma "
        "(retry-on-empty) then normalize to DTCG tokens (HITL-reviewed); "
        "(baseline) read the helix-code baseline at baseline_ref. Both branches "
        "converge into a non-adjacent-access smoke test."
    ),
    db=get_postgres_db(),
    steps=[
        Parallel(client_branch, baseline_step, name="client-and-baseline"),
        smoke_test_step,
    ],
)
