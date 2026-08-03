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

from agents._shared.observability import CircuitBreaker, assess_anomaly, engagement_seed
from agents.component_code_generator.generation import (
    GenerationInput,
    Generator,
    MockGenerator,
    distill_element_context,
    parse_variant_axes,
    run_structural_gate,
)
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
    find_sibling_sources,
    fork_component,
    is_valid_lit_source,
    pascal_case,
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
    sibling_reader: Optional[Callable[[str, str], dict[str, str]]] = None,
    generator: Optional[Generator] = None,
    breaker: Optional[CircuitBreaker] = None,
    timestamp: Optional[str] = None,
    output_dir: Optional[str] = None,
) -> ComponentCodeGeneratorOutput:
    """Full pipeline (Phase 1 deterministic fork + Phase 2 agentic from-spec).

    ``source_reader`` is injectable (Path A baseline lookup). ``generator`` is the injected Path-B
    from-spec generator (default deterministic Mock — CI-safe, no LLM); a live run passes AgentGenerator.
    Every generated element passes the structural gate (Adjustment 1); a failure retries once then
    SP-6-flags (never fabricate). ``breaker`` hard-caps generation calls (Probe-3 pattern).
    """
    reader = source_reader or find_baseline_source
    sib_reader = sibling_reader or find_sibling_sources
    gen = generator or MockGenerator()
    breaker = breaker or CircuitBreaker(fine_env="COMP_CODE_GEN_MAX_FINE_GRAINED",
                                        coarse_env="COMP_CODE_GEN_MAX_COARSE_CHUNKS")
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
            baseline_source, source_ref = src  # source_ref is "branch:path" (Path 1 provenance)
            # D-p1-4 (SP-6 honesty): if the branch source is WIP/incomplete, STILL fork it (it's
            # Sascha's real work-in-progress, not a fabricated shell) but flag it loudly.
            wip = not is_valid_lit_source(baseline_source)
            forked, tag, cls = fork_component(baseline_source, slug)
            element_dir = f"{pkg}/src/elements/{slugify(slot)}"
            file_path = f"{element_dir}/{cls}.ts"
            tree[file_path] = forked
            # Correction #18 / Ratification 1: fork the component's sibling ``types.ts`` + barrel
            # ``index.ts`` verbatim alongside it, so the drop-in matches helix-code's own-directory
            # convention and the fork is complete (present only in that convention → {} otherwise).
            for _sib_name, _sib_src in (sib_reader(source_ref, helix_code_root) or {}).items():
                tree[f"{element_dir}/{_sib_name}"] = _sib_src
            gate = StructuralGateResult(applied=False)  # deterministic fork — gate not applicable (Adjustment 1)
            if wip:
                warnings.append(BlockingWarning(code="baseline_source_wip", element_slot=slot,
                                                detail=f"forked WIP branch source {source_ref} (may be incomplete)",
                                                recommended_action="human_review"))
            results.append(ElementGenerationResult(slot=slot, element_tag=tag, class_name=cls,
                                                    file_path=file_path, path="fork_deterministic",
                                                    baseline_ref=source_ref, confidence=confidence,
                                                    structural_gate=gate,
                                                    rationale=f"deterministic fork of {source_ref}"
                                                              + (" (WIP source)" if wip else "")))
            cem_elements.append({"element_tag": tag, "class_name": cls, "file_path": file_path})
            summary.fork_deterministic += 1
            # NB: deterministic forks skip the structural gate (byte-identical by construction,
            # applied=False) — they do NOT count toward gate_passed, which tracks GENERATED code only.
        else:  # Path B — from-spec generation (agentic; Mock in CI)
            figma_meta = (e.get("figma_meta") if isinstance(e, dict) else getattr(e, "figma_meta", None)) or {}
            # Track D v0.2.5: distil the enriched composition frames for this organism into a compact
            # from-spec context. Prefer an explicitly-supplied figma_context; else derive from frames.
            # Absent → None → Path-B falls back to the v0.2 thin behaviour (BC-A).
            figma_context = figma_meta.get("figma_context") or distill_element_context(figma_meta.get("frames"))
            spec = GenerationInput(
                slot=slot, customer_slug=slug,
                variant_axes=parse_variant_axes(figma_meta.get("variant_names") or []),
                tokens_consumed=figma_meta.get("tokens_consumed") or [],
                figma_context=figma_context,
            )
            element_slug = slugify(slot)
            if not breaker.allow_fine_grained():
                warnings.append(BlockingWarning(code="deferred_to_phase_2", element_slot=slot,
                                                detail="circuit breaker tripped; not generated",
                                                recommended_action="human_review"))
                results.append(ElementGenerationResult(slot=slot, path="deferred", confidence="unresolved",
                                                        rationale="circuit breaker tripped"))
                summary.deferred += 1
                continue
            # generate → gate → one retry → SP-6 flag
            gen_res = gen.generate(spec)
            gate = run_structural_gate(gen_res.source, customer_slug=slug, element_slug=element_slug)
            if not (gate.passed and gen_res.confidence != "unresolved") and breaker.allow_fine_grained():
                gen_res = gen.generate(spec)  # one retry (Adjustment 1)
                gate = run_structural_gate(gen_res.source, customer_slug=slug, element_slug=element_slug)
            tag = f"{slug}-{element_slug}"
            cls = pascal_case(tag) + "Element"
            if gate.passed and gen_res.confidence != "unresolved":
                file_path = f"{pkg}/src/elements/{element_slug}/{cls}.ts"
                tree[file_path] = gen_res.source
                results.append(ElementGenerationResult(slot=slot, element_tag=tag, class_name=cls,
                                                        file_path=file_path, path="from_spec",
                                                        confidence=gen_res.confidence, structural_gate=gate,
                                                        rationale=gen_res.rationale))
                cem_elements.append({"element_tag": tag, "class_name": cls, "file_path": file_path})
                summary.from_spec += 1
                summary.gate_passed += 1
                if gen_res.confidence == "medium":  # proceed-with-warning (Decision #4)
                    warnings.append(BlockingWarning(code="generation_below_threshold", element_slot=slot,
                                                    detail=gen_res.rationale, recommended_action="human_review"))
            else:  # gate failed after retry, or agent abstained → SP-6 flag, no fabrication
                code = "structural_gate_failed" if not gate.passed else "generation_below_threshold"
                warnings.append(BlockingWarning(code=code, element_slot=slot,
                                                detail="; ".join(gate.failures) or gen_res.rationale,
                                                recommended_action="human_review"))
                results.append(ElementGenerationResult(slot=slot, path="from_spec", confidence="unresolved",
                                                        structural_gate=gate,
                                                        rationale="gate failed after retry / abstained"))
                summary.from_spec += 1
                summary.gate_failed += 1

    gen_name = getattr(gen, "name", "mock")
    prov = ProvenanceExtension(
        baseline_source_ref=(elements and helix_code_root) or None,
        model_routing=(f"{gen_name} from-spec generator" if summary.from_spec else None),
        elements=results,
    )
    # static package scaffolding + docs (deterministic)
    if tokens_json is not None:
        tree[f"{pkg}/src/tokens/tokens.json"] = tokens_json
    tree[f"{pkg}/src/index.ts"] = "// customer component library — exports generated by HELIX 3d\n"
    tree[f"{pkg}/custom-elements.json"] = render_cem(cem_elements)
    tree[f"{pkg}/docs/PROVENANCE.md"] = render_ccg_provenance_md(customer_slug, prov.model_dump())

    status = "success" if (summary.deferred == 0 and summary.gate_failed == 0) else "partial"
    package_path = write_package(tree, output_dir) if output_dir else None
    # observability: expected = one generation call per from-spec element; token totals from a real
    # generator's usage() (Mock exposes none → 0). Deterministic bookkeeping (Probe-3 pattern).
    expected = summary.from_spec
    fine_used = breaker.fine_grained_used
    tin = tout = 0
    usage_fn = getattr(gen, "usage", None)
    if callable(usage_fn):
        try:
            tin, tout = usage_fn()
        except Exception:  # noqa: BLE001
            tin = tout = 0
    return ComponentCodeGeneratorOutput(
        status=status, package_path=package_path, summary=summary, provenance=prov,
        blocking_warnings=warnings,
        cost_summary=CostSummary(fine_grained_invocations=fine_used, expected_fine_grained=expected,
                                 total_input_tokens=tin, total_output_tokens=tout,
                                 breaker_tripped=breaker.tripped,
                                 anomaly=assess_anomaly(expected, fine_used, breaker.tripped)),
        non_deterministic=(summary.from_spec > 0 and gen_name != "mock"),
    )


def input_gathering_executor(step_input: StepInput, **kwargs) -> StepOutput:
    data = getattr(step_input, "additional_data", None) or {}
    bundle = gather_inputs(data)
    return StepOutput(step_name=STEP_NAME_INPUT, content=bundle, success=not bundle["blocking"])


def _select_generator(data: dict, seed: int | None):
    """Mock (CI/default) vs real Agno generator, env-gated (mirrors 3c's split). The real generator
    gets the per-engagement seed (recorded, not sent — Anthropic route rejects it; temp=0 is the lever).
    Returns None → generate_component_code defaults to the deterministic Mock."""
    flag = str(data.get("use_real_agent") or os.environ.get("COMP_CODE_GEN_USE_REAL_AGENT", "")).lower()
    if flag in ("1", "true", "yes"):
        from agents.component_code_generator.generation import AgentGenerator
        return AgentGenerator(seed=seed)
    return None


def generate_executor(step_input: StepInput, **kwargs) -> StepOutput:
    gathered = step_input.get_step_content(STEP_NAME_INPUT) or {}
    if gathered.get("blocking"):
        env = ComponentCodeGeneratorOutput(
            status="failure",
            blocking_warnings=[BlockingWarning(**b) for b in gathered["blocking"]],
        )
        return StepOutput(step_name=STEP_NAME_GENERATE, content=env.model_dump(), success=False)
    data = getattr(step_input, "additional_data", None) or {}
    seed = engagement_seed(gathered["customer_slug"], data.get("engagement_timestamp") or _now_iso())
    env = generate_component_code(
        customer_slug=gathered["customer_slug"], scope=gathered["scope"],
        elements=gathered["elements"], tokens_json=gathered.get("tokens_json"),
        helix_code_root=gathered["helix_code_root"], generator=_select_generator(data, seed),
        output_dir=data.get("output_dir"),
    )
    return StepOutput(step_name=STEP_NAME_GENERATE, content=env.model_dump(),
                      success=env.status != "failure")
