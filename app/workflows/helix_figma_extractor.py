"""
HELIX Figma Extractor Workflow (parallel: client-branch + baseline-branch)
==========================================================================

Shape (decision:cycle-2-wiring-2026-07-21 + human-in-the-loop (HITL) review Finding-51 fix 2026-07-27):

    steps = [ Parallel(extract, baseline-read),  →  normalize  →  smoke-test ]

  extract and baseline-read run in PARALLEL; normalize and smoke-test are TOP-LEVEL
  steps after the Parallel converges.

  extract (top-level Parallel member "extract"):
    DETERMINISTIC extractor (spec figma-extractor-deterministic-v0-1, RATIFIED) —
    deterministic_extract_executor: NO LLM, NO agent. Orchestrates Lane 5 (meta) → Lane 2
    (semantic roster) → per-set Framelink get_figma_data via ClientSession.call_tool →
    Lane 3 (bindings) → asset download → Levenshtein typo detection. Emits a
    FigmaExtractionResult-shaped dict (Normalizer contract) with the §5 rich envelope nested
    under 'deterministic_extraction'. (The old LLM agent path — extract_with_retry + drill
    guard — is retained above as dead code per spec §9 replace-in-place; git preserves history.)

  normalize (TOP-LEVEL step "normalize", post-Parallel):
    Token Normalizer (pure Python, zero LLM / zero MCP). Reads extract's
    FigmaExtractionResult BY NAME across the Parallel boundary via
    get_step_output('extract') — the proven cycle-2 non-adjacent access pattern —
    converts to DTCG NormalizedTokens. The HITL output-review gate lives here; it is
    reachable ONLY because normalize is top-level (Agno evaluates requires_output_review
    for top-level Step/Router only — nesting it inside Steps/Parallel made it a no-op,
    Finding 51 / shared-results:hitl-pause-behavior-verification-result-2026-07-27).

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

Rollback: this MR (normalize moved top-level) is a single-commit revert; ``id`` unchanged.
Reverting restores the prior shape (normalize nested in the Parallel — HITL gate a no-op).

Registered in ``app/main.py`` via ``AgentOS(workflows=[...])`` — id unchanged.
"""

from __future__ import annotations

import json
import os
import re

from agno.workflow import Parallel, Step, Workflow
from agno.workflow.types import StepInput, StepOutput

from agents.baseline_reader.step import (
    STEP_NAME_BASELINE,
    STEP_NAME_SMOKE,
    baseline_access_smoke_test_executor,
    baseline_read_executor,
)
from agents.figma_extractor.agent import figma_extractor_agent, figma_mcp_tools
from agents.figma_extractor.deterministic import run_deterministic_extraction
from agents.figma_extractor.models import FigmaExtractionResult
from agents.token_normalizer.normalizer import FigmaExtractionResult as _NormalizerFER
from agents.token_normalizer.normalizer import normalize_tokens
from db import get_postgres_db

# ---------------------------------------------------------------------------
# Step 1 — extract (retry-on-empty guard)
# ---------------------------------------------------------------------------
MAX_EXTRACTION_ATTEMPTS = 3

# Option D — drill guard. A "node-level drill call" is a get_figma_data call that
# carries a concrete nodeId (i.e. NOT the file-root discovery call 0:0 / 0:1 / no
# nodeId). If the agent discovered pages but drilled fewer than this many times, it
# almost certainly finalized empty per RULE 1 — reject and re-nudge; hard-abort with
# a diagnostic after the final attempt rather than silently returning an empty result.
DRILL_THRESHOLD_N = 3
_ROOT_NODE_IDS = {"", "0:0", "0:1"}
_NODE_ID_KEYS = ("nodeId", "node_id", "node-id")

_CONTINUE_NUDGE = (
    "\n\nCRITICAL — a previous attempt returned an EMPTY extraction (no tokens, no "
    "components) after essentially only the discovery call. That is a FAILURE. Do NOT "
    "stop after discovery. You MUST make at least 7 get_figma_data calls: one discovery, "
    "then one per foundation page (Typography, Colors, Layout, Icons, ImageRatios, Text) "
    "and one per priority component (Button, Input, Toggle, Checkbox, Dropdown, FormField). "
    "Keep calling tools until BOTH tokens AND components are populated, then assemble the "
    "FigmaExtractionResult."
)

# Escalation added on the FINAL attempt when prior attempts stayed shallow (drilled
# fewer than DRILL_THRESHOLD_N times). Names the failure explicitly.
_DRILL_NUDGE = (
    "\n\nHARD REQUIREMENT — prior attempts discovered pages but did NOT drill into them "
    "(too few get_figma_data calls with a concrete nodeId). You MUST call get_figma_data "
    "AGAIN for each foundation and component page using the node ids from your discovery "
    "response — the file-root (0:0) call alone is not extraction. This is your final attempt."
)


class DrillGuardAbort(Exception):
    """Raised when the extractor discovers pages but never drills into them across all
    attempts (RULE 1 premature-finalize). Carries a secret-free diagnostic so the failure
    surfaces with context instead of a silent empty result."""

    def __init__(self, diagnostic: dict):
        self.diagnostic = diagnostic
        super().__init__(diagnostic.get("suggestion", "drill guard hard-abort"))


def _scrub_secrets(text: str) -> str:
    """Never let a PAT leak into a diagnostic. Redact the live FIGMA_PAT value if present."""
    if not text:
        return text
    for var in ("FIGMA_PAT", "FIGMA_API_KEY"):
        val = os.environ.get(var)
        if val:
            text = text.replace(val, "<redacted>")
    return text


def _count_node_drill_calls(response) -> int:
    """Count get_figma_data calls that carry a concrete (non-root) nodeId.

    Single agent (not a Team), so ``response.tools`` reflects THIS agent's own calls —
    the leader-only caveat (lesson figma-extractor-implementation RULE 2) does not apply.
    """
    tools = getattr(response, "tools", None) or []
    count = 0
    for t in tools:
        name = getattr(t, "tool_name", None) or (t.get("tool_name") if isinstance(t, dict) else None)
        if not name or "get_figma_data" not in name:
            continue
        args = getattr(t, "tool_args", None)
        if args is None and isinstance(t, dict):
            args = t.get("tool_args")
        args = args or {}
        node_id = next((str(args[k]) for k in _NODE_ID_KEYS if k in args and args[k] is not None), "")
        if node_id.strip() not in _ROOT_NODE_IDS:
            count += 1
    return count


def _pages_discovered(result: FigmaExtractionResult | None) -> int:
    return len(result.pages_discovered) if result is not None else 0


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
    """Run the extractor, retrying while the result is empty (premature finalize).

    Option D drill guard: track how many node-level get_figma_data calls each attempt
    makes; escalate the nudge on the final attempt if the agent has stayed shallow; and
    if every attempt finalizes empty, HARD-ABORT with a secret-free diagnostic
    (``DrillGuardAbort``) instead of silently returning an empty result — so the failure
    surfaces with context rather than sliding through to a quiet, reviewless normalize.
    """
    base_input = step_input.input or step_input.previous_step_content or ""
    session_id = getattr(getattr(step_input, "workflow_session", None), "session_id", None)

    last_result: FigmaExtractionResult | None = None
    best_drill_calls = 0
    best_pages = 0
    last_response_excerpt = ""
    for attempt in range(1, MAX_EXTRACTION_ATTEMPTS + 1):
        message = base_input if attempt == 1 else f"{base_input}{_CONTINUE_NUDGE}"
        # Final attempt + only-ever-shallow drilling → escalate with the explicit drill nudge.
        if attempt == MAX_EXTRACTION_ATTEMPTS and best_drill_calls < DRILL_THRESHOLD_N:
            message = f"{message}{_DRILL_NUDGE}"
        run_kwargs = {"input": message}
        if session_id:
            run_kwargs["session_id"] = session_id
        response = await figma_extractor_agent.arun(**run_kwargs)
        last_result = _coerce_figma(getattr(response, "content", response))
        best_drill_calls = max(best_drill_calls, _count_node_drill_calls(response))
        best_pages = max(best_pages, _pages_discovered(last_result))
        last_response_excerpt = _scrub_secrets(str(getattr(response, "content", "") or ""))[:2000]
        if not _is_empty(last_result):
            return StepOutput(content=last_result)

    # Every attempt finalized empty → hard-abort with diagnostics (Option D HITL surface).
    diagnostic = {
        "error": "drill_guard_hard_abort",
        "pages_discovered": best_pages,
        "node_level_drill_calls": best_drill_calls,
        "drill_threshold_N": DRILL_THRESHOLD_N,
        "attempts": MAX_EXTRACTION_ATTEMPTS,
        "last_extractor_response_excerpt": last_response_excerpt,
        "suggestion": (
            f"drill guard rejected — the extractor discovered {best_pages} page(s) but made "
            f"only {best_drill_calls} node-level get_figma_data call(s) across "
            f"{MAX_EXTRACTION_ATTEMPTS} attempts (< threshold {DRILL_THRESHOLD_N}); this is the "
            "documented RULE 1 premature-finalize. Consider adjusting the prompt, the "
            "parser_model choice, or the drill threshold."
        ),
    }
    raise DrillGuardAbort(diagnostic)


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
    """Normalize the FigmaExtractionResult from the extract step.

    Runs TOP-LEVEL, after the Parallel(extract, baseline) converges (so its HITL
    output-review gate is reachable — Agno only evaluates requires_output_review on
    top-level Step/Router). Reads extract's output BY NAME across the Parallel boundary
    via ``get_step_output('extract')`` — the proven cycle-2 non-adjacent access pattern
    (see baseline_access_smoke_test_executor / shared-results:3a-cycle-2-wiring-result).
    NOT ``previous_step_content`` — that would now be the Parallel's aggregate output."""
    extract_out = step_input.get_step_output("extract")
    raw = getattr(extract_out, "content", None) if extract_out is not None else None
    extraction = _coerce_extraction_for_normalizer(raw)
    if extraction is None:
        return StepOutput(
            content="normalization failed: no usable FigmaExtractionResult from the extract step",
            success=False,
            stop=True,
        )
    try:
        normalized = normalize_tokens(extraction)
    except Exception as e:  # keep the pipeline observable rather than 500-ing
        return StepOutput(content=f"normalization failed: {e!r}", success=False, stop=True)
    return StepOutput(content=normalized.model_dump())


# ---------------------------------------------------------------------------
# Step 1 — DETERMINISTIC extractor (spec figma-extractor-deterministic-v0-1, RATIFIED)
# Replaces the LLM-orchestrated extract_with_retry (kept above as dead code / git history
# per spec §9 replace-in-place; removable in a cleanup follow-up). Zero LLM inference.
# ---------------------------------------------------------------------------
_FILE_KEY_RE = re.compile(r"(?:/(?:file|design)/)([A-Za-z0-9]{20,40})|([A-Za-z0-9]{20,40})")


def _parse_file_key(message: str) -> str | None:
    """Deterministically extract the Figma file key from the run message (full URL or bare key)."""
    if not message:
        return None
    m = re.search(r"/(?:file|design)/([A-Za-z0-9]{20,40})", message)
    if m:
        return m.group(1)
    # bare-key fallback: the first 20-40 char alphanumeric run
    m = re.search(r"\b([A-Za-z0-9]{20,40})\b", message)
    return m.group(1) if m else None


async def deterministic_extract_executor(step_input: StepInput, **kwargs) -> StepOutput:
    """Function-backed extract step (no agent, no LLM). Parses the file key from the run message,
    connects the Framelink MCP session, and runs the deterministic orchestration. Emits a
    FigmaExtractionResult-shaped dict (contract for normalize) with the §5 envelope nested under
    'deterministic_extraction'. Never raises past the step boundary."""
    message = step_input.input or step_input.previous_step_content or ""
    file_key = _parse_file_key(str(message))
    if not file_key:
        return StepOutput(
            content=FigmaExtractionResult(
                file_key="", gaps_detected=["no Figma file key found in run message"]
            ).model_dump(),
            success=False,
        )
    async with figma_mcp_tools:  # connect Framelink stdio MCP for the extraction (deterministic call_tool)
        content = await run_deterministic_extraction(file_key, session=figma_mcp_tools.session)
    status = (content.get("deterministic_extraction") or {}).get("status")
    return StepOutput(content=content, success=(status != "failure"))


extract_step = Step(
    name="extract",
    executor=deterministic_extract_executor,
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

# Converge: verify non-adjacent access to 3a's output (the pattern 3b will use).
smoke_test_step = Step(
    name=STEP_NAME_SMOKE,
    executor=baseline_access_smoke_test_executor,
)

# HITL FIX (Finding 51): normalize_step is TOP-LEVEL, after the Parallel converges — Agno
# only evaluates requires_output_review on top-level Step/Router, so nesting normalize inside
# Steps(...) inside Parallel(...) made the review gate structurally unreachable. Extract runs
# as a direct Parallel member; normalize reads it via get_step_output('extract'). Parallelism
# (cycle-2) preserved: extract || baseline still run concurrently.
helix_figma_extractor_workflow = Workflow(
    id="helix-figma-extractor",
    name="HELIX Figma Extractor",
    description=(
        "Parallel(extract, baseline) → normalize (DTCG, HITL-reviewed, top-level) → "
        "non-adjacent-access smoke test. Extract retries-on-empty; normalize reads extract "
        "across the Parallel boundary; baseline reads helix-code at baseline_ref."
    ),
    db=get_postgres_db(),
    steps=[
        Parallel(extract_step, baseline_step, name="client-and-baseline"),
        normalize_step,   # top-level → HITL output-review gate is now reachable (Finding 51 fix)
        smoke_test_step,
    ],
)
