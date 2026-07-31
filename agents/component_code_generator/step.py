"""3d Component Code Generator — Phase-1 orchestration + Agno step executors.

Phase 1 is DETERMINISTIC: it consumes the 3c package (element fork-descriptors + tokens), routes
each element, and emits real Lit code for the Path-A (baseline-matched) elements by forking the
baseline source from helix-code (READ-ONLY). Path-B (no-baseline) elements are recorded DEFERRED —
Phase 2 adds the agentic from-spec path + structural gate. No LLM in Phase 1.

Pattern mirrors 3c: a pure, directly-testable core (``generate_component_code``) + a thin Agno
wrapper reading run ``additional_data`` and handing output across the step boundary.

SP-9: helix-code root + customer scope are inputs, never hardcoded. helix-code is READ-ONLY.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any, Callable, Optional

from agno.workflow.types import StepInput, StepOutput

from agents.component_code_generator.models import (
    BlockingWarning,
    ComponentCodeGeneratorOutput,
    ComponentGenerationSummary,
    CostSummary,
    ElementGenerationResult,
    ProvenanceExtension,
    StructuralGateResult,
)
from agents.component_code_generator.scaffolding import (
    find_baseline_source,
    fork_component,
    render_ccg_provenance_md,
    render_cem,
    route_element,
    slugify,
    write_package,
)

STEP_NAME_INPUT = "ccg-input-gathering"
STEP_NAME_GENERATE = "ccg-generate"

DEFAULT_HELIX_CODE_ROOT = os.environ.get("HELIX_CODE_ROOT", "/var/lib/helix/baseline")


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _slot(e: Any) -> str:
    return (e.get("slot") if isinstance(e, dict) else getattr(e, "slot", None)) or ""


def gather_inputs(additional_data: dict | None) -> dict:
    """Pull + shallow-validate the 3c input from run additional_data (SP-6 fail-loud).

    Expects: customer_slug, scope, elements (list of {slot, baseline_ref, derivation, confidence}),
    tokens_json (the 3c token layer, optional), helix_code_root (optional override).
    """
    data = additional_data or {}
    elements = data.get("elements") or []
    blocking: list[BlockingWarning] = []
    if not elements:
        blocking.append(BlockingWarning(code="input_unavailable",
                                        detail="additional_data.elements (3c package descriptors) missing/empty",
                                        recommended_action="abort_run"))
    return {
        "customer_slug": data.get("customer_slug") or "customer",
        "scope": data.get("scope") or "msq-dx",
        "elements": elements,
        "tokens_json": data.get("tokens_json"),
        "helix_code_root": data.get("helix_code_root") or DEFAULT_HELIX_CODE_ROOT,
        "blocking": [b.model_dump() for b in blocking],
    }


def generate_component_code(
    *,
    customer_slug: str,
    scope: str,
    elements: list[Any],
    tokens_json: Optional[str] = None,
    helix_code_root: str = DEFAULT_HELIX_CODE_ROOT,
    source_reader: Optional[Callable[[str, str], Optional[tuple[str, str]]]] = None,
    timestamp: Optional[str] = None,
    output_dir: Optional[str] = None,
) -> ComponentCodeGeneratorOutput:
    """Phase-1 core (deterministic, no LLM). Directly testable.

    ``source_reader(baseline_ref, helix_root) -> (source, relpath) | None`` is injectable so tests
    provide baseline source without a helix-code checkout; defaults to the real READ-ONLY lookup.
    """
    reader = source_reader or find_baseline_source
    slug = slugify(customer_slug)
    pkg = f"packages/{slug}-elements"

    if not elements:
        return ComponentCodeGeneratorOutput(
            status="failure",
            blocking_warnings=[BlockingWarning(code="input_unavailable",
                                               detail="no elements to generate", recommended_action="abort_run")],
        )

    tree: dict[str, str] = {}
    results: list[ElementGenerationResult] = []
    warnings: list[BlockingWarning] = []
    summary = ComponentGenerationSummary(total=len(elements))
    cem_elements: list[dict] = []

    for e in sorted(elements, key=_slot):
        slot = _slot(e)
        if not slot:
            continue
        derivation = (e.get("derivation") if isinstance(e, dict) else getattr(e, "derivation", None)) or ""
        baseline_ref = (e.get("baseline_ref") if isinstance(e, dict) else getattr(e, "baseline_ref", None))
        confidence = (e.get("confidence") if isinstance(e, dict) else getattr(e, "confidence", None)) or "unresolved"
        path = route_element(derivation=derivation, baseline_ref=baseline_ref)

        if path == "fork_deterministic":
            src = reader(baseline_ref, helix_code_root)
            if not src:
                warnings.append(BlockingWarning(code="baseline_source_unavailable", element_slot=slot,
                                                detail=f"baseline '{baseline_ref}' not found in helix-code",
                                                recommended_action="human_review"))
                results.append(ElementGenerationResult(slot=slot, path="deferred", baseline_ref=baseline_ref,
                                                        confidence=confidence,
                                                        rationale="baseline source missing; cannot fork"))
                summary.deferred += 1
                continue
            baseline_source, _relpath = src
            forked, tag, cls = fork_component(baseline_source, slug)
            file_path = f"{pkg}/src/elements/{slugify(slot)}/{cls}.ts"
            tree[file_path] = forked
            gate = StructuralGateResult(applied=False)  # deterministic fork — gate not applicable (Adjustment 1)
            results.append(ElementGenerationResult(slot=slot, element_tag=tag, class_name=cls,
                                                    file_path=file_path, path="fork_deterministic",
                                                    baseline_ref=baseline_ref, confidence=confidence,
                                                    structural_gate=gate,
                                                    rationale=f"deterministic fork of baseline '{baseline_ref}'"))
            cem_elements.append({"element_tag": tag, "class_name": cls, "file_path": file_path})
            summary.fork_deterministic += 1
            summary.gate_passed += 1
        else:  # deferred → Path B (Phase 2)
            warnings.append(BlockingWarning(code="deferred_to_phase_2", element_slot=slot,
                                            detail="no baseline match; from-spec generation is Phase 2",
                                            recommended_action="await_phase_2"))
            results.append(ElementGenerationResult(slot=slot, path="deferred", baseline_ref=baseline_ref,
                                                    confidence="unresolved",
                                                    rationale="Path B (from-spec) deferred to Phase 2"))
            summary.deferred += 1

    prov = ProvenanceExtension(
        baseline_source_ref=(elements and helix_code_root) or None,
        model_routing=None,  # deterministic-only Phase-1 run
        elements=results,
    )
    # static package scaffolding + docs (deterministic)
    if tokens_json is not None:
        tree[f"{pkg}/src/tokens/tokens.json"] = tokens_json
    tree[f"{pkg}/src/index.ts"] = "// customer component library — exports generated by HELIX 3d\n"
    tree[f"{pkg}/custom-elements.json"] = render_cem(cem_elements)
    tree[f"{pkg}/docs/PROVENANCE.md"] = render_ccg_provenance_md(customer_slug, prov.model_dump())

    status = "success" if summary.deferred == 0 else "partial"
    package_path = write_package(tree, output_dir) if output_dir else None
    return ComponentCodeGeneratorOutput(
        status=status, package_path=package_path, summary=summary, provenance=prov,
        blocking_warnings=warnings,
        cost_summary=CostSummary(),  # Phase 1 = zero LLM calls
        non_deterministic=False,     # deterministic-only until Phase 2 agentic paths run
    )


def input_gathering_executor(step_input: StepInput, **kwargs) -> StepOutput:
    data = getattr(step_input, "additional_data", None) or {}
    bundle = gather_inputs(data)
    return StepOutput(step_name=STEP_NAME_INPUT, content=bundle, success=not bundle["blocking"])


def generate_executor(step_input: StepInput, **kwargs) -> StepOutput:
    gathered = step_input.get_step_content(STEP_NAME_INPUT) or {}
    if gathered.get("blocking"):
        env = ComponentCodeGeneratorOutput(
            status="failure",
            blocking_warnings=[BlockingWarning(**b) for b in gathered["blocking"]],
        )
        return StepOutput(step_name=STEP_NAME_GENERATE, content=env.model_dump(), success=False)
    data = getattr(step_input, "additional_data", None) or {}
    env = generate_component_code(
        customer_slug=gathered["customer_slug"], scope=gathered["scope"],
        elements=gathered["elements"], tokens_json=gathered.get("tokens_json"),
        helix_code_root=gathered["helix_code_root"], output_dir=data.get("output_dir"),
    )
    return StepOutput(step_name=STEP_NAME_GENERATE, content=env.model_dump(),
                      success=env.status != "failure")
