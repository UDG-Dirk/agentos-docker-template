"""HELIX Composition-Only Extractor — deployed workflow for Pattern 3 (self-contained/unpublished).

For files like DGX Brandportal: own components authored as page frames, publishing nothing (0/0/0)
and referencing no external Core. Runs Pathway B (composition_tree + local components) and SKIPS Lane 6
entirely (nothing external to resolve). No wasteful Core-mode pass, no self-referential resolution.

Invoke: `POST /workflows/helix-composition-only-extractor/runs` with `message=<figma-key-or-URL>`.
Distinct from helix-client-extractor (Pattern 2, needs a remote Core) and helix-figma-extractor
(Pattern 1, published library). Zero LLM.
"""
from __future__ import annotations

import re

from agno.workflow import Step, Workflow
from agno.workflow.types import StepInput, StepOutput

from agents.figma_extractor.composition_mode import run_composition_extraction
from db import get_postgres_db


def _parse_one_key(message: str) -> str | None:
    """First Figma file key from the run message (design/file URL or bare 20-40 char key)."""
    if not message:
        return None
    m = re.search(r"/(?:file|design)/([A-Za-z0-9]{20,40})", message)
    if m:
        return m.group(1)
    m = re.search(r"\b([A-Za-z0-9]{20,40})\b", message)
    return m.group(1) if m else None


async def composition_only_executor(step_input: StepInput, **kwargs) -> StepOutput:
    """Parse one file key and run composition-only extraction (Pathway B, no Lane 6). Never raises."""
    message = step_input.input or step_input.previous_step_content or ""
    fk = _parse_one_key(str(message))
    if not fk:
        return StepOutput(
            content={"extraction_mode": "composition-only", "status": "failure",
                     "error_class": "missing_file_key",
                     "message": "provide a Figma file key or design URL in the run message"},
            success=False)
    # registered_libraries=[] -> composition-only (Lane 6 skipped) per run_composition_extraction
    result = await run_composition_extraction(fk, registered_libraries=[], file_role="self_contained")
    return StepOutput(content=result, success=(result.get("status") != "failure"))


composition_only_step = Step(name="composition-only-extract", executor=composition_only_executor)

helix_composition_only_extractor_workflow = Workflow(
    id="helix-composition-only-extractor",
    name="HELIX Composition-Only Extractor",
    description=(
        "Pattern 3 (self-contained/unpublished): Pathway B page-frame walk (composition_tree + local "
        "components), Lane 6 skipped (no external library). Message carries one Figma key/URL."
    ),
    db=get_postgres_db(),
    steps=[composition_only_step],
)
