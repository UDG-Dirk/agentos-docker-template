"""3c Theme Generator — Agno step executors + Phase-1 orchestration.

Two Phase-1 steps (spec §9.2):
  * ``theme-input-gathering``     (Step 1) — gather + validate the three inputs
    (3a baseline, client Figma extraction, 3b per-component scoring).
  * ``theme-deterministic-transform`` (Step 2) — the deterministic package build
    (scaffolding + token substitution + high-confidence forking + passthrough).

Steps 3-6 (agentic reconciliation, cohesion review, provenance, assembly) are
Phase 2+; the envelope shape is already stable (see models.py) so those phases
add behaviour without changing the contract.

Pattern mirrors ``agents/baseline_reader/step.py`` and the normalizer: a pure,
directly-testable core (``generate_theme_phase1``) with a thin Agno wrapper that
reads run ``additional_data`` and hands output across the step boundary via
``StepInput.get_step_output``.

SP-9: model routing comes from ``app.settings`` at run time, never hardcoded here
(Phase 1 is deterministic and invokes no model; the field is recorded for Phase 2).
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any, Optional

from agno.workflow.types import StepInput, StepOutput

from agents.theme_generator.models import (
    BlockingWarning,
    ComponentDerivation,
    CostSummary,
    ProvenanceSummary,
    ThemeGeneratorOutput,
    UnmappedComponent,
)
from agents.theme_generator.reconciliation import (
    CircuitBreaker,
    CohesionReviewer,
    MockCohesionReviewer,
    MockReconciler,
    Reconciler,
    ReconciliationResult,
    assess_anomaly,
    engagement_seed,
)
from agents.theme_generator.scaffolding import (
    _element_module,
    build_deterministic_package,
    render_package_json,
    render_provenance_md,
    slugify,
    write_package,
)

STEP_NAME_INPUT = "theme-input-gathering"
STEP_NAME_TRANSFORM = "theme-deterministic-transform"  # Phase-1-only path (kept + tested)
STEP_NAME_GENERATE = "theme-generate"  # full pipeline: deterministic + agentic + cohesion

# Default package scope; overridable per-run via additional_data (SP-9: not a secret,
# but kept out of code as a constant the caller can set for a different customer org).
DEFAULT_SCOPE = "msq-dx"


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def gather_inputs(additional_data: dict | None) -> dict:
    """Pull + shallow-validate the three 3c inputs from run additional_data.

    Returns a normalized bundle: customer_slug, scope, baseline, client_components,
    client_tokens, scoring_by_slot, plus a ``blocking`` list (SP-6) naming anything
    missing. Never raises — missing inputs surface as blocking_warnings.
    """
    data = additional_data or {}
    baseline = data.get("baseline") or {}
    client = data.get("client_extraction") or {}
    scoring_by_slot = data.get("scoring_by_slot") or {}

    blocking: list[BlockingWarning] = []
    if not baseline:
        blocking.append(BlockingWarning(code="baseline_unavailable",
                                        detail="additional_data.baseline missing/empty",
                                        recommended_action="abort_run"))
    if not client.get("components"):
        blocking.append(BlockingWarning(code="client_extraction_unavailable",
                                        detail="additional_data.client_extraction.components missing/empty",
                                        recommended_action="abort_run"))
    return {
        "customer_slug": data.get("customer_slug") or client.get("file_key") or "customer",
        "scope": data.get("scope") or DEFAULT_SCOPE,
        "baseline": baseline,
        "client_components": client.get("components") or [],
        "client_tokens": client.get("tokens") or [],
        "scoring_by_slot": scoring_by_slot,
        "blocking": [b.model_dump() for b in blocking],
    }


def generate_theme_phase1(
    *,
    customer_slug: str,
    scope: str,
    baseline: dict,
    client_components: list[Any],
    client_tokens: list[Any],
    scoring_by_slot: dict[str, Any],
    timestamp: Optional[str] = None,
    model_routing: Optional[str] = None,
    output_dir: Optional[str] = None,
) -> ThemeGeneratorOutput:
    """Phase-1 core (deterministic, no LLM). Directly testable.

    Assembles the provenance (baseline ref + CEM health from 3a's meta), runs the
    deterministic transform, optionally materialises the package to ``output_dir``,
    and returns the full ThemeGeneratorOutput envelope. ``non_deterministic`` is
    True even here, per Decision #5 (3c is always labelled non-deterministic).
    """
    meta = (baseline or {}).get("meta") or {}
    prov = ProvenanceSummary(
        baseline_source_ref=meta.get("baseline_ref"),
        baseline_cem_size_bytes=int(meta.get("cem_size_bytes") or 0),
        generation_timestamp=timestamp or _now_iso(),
        model_routing=model_routing,  # None on a deterministic-only Phase-1 run
    )
    tree, prov, unmapped, warnings, status = build_deterministic_package(
        customer_slug=customer_slug,
        scope=scope,
        baseline=baseline,
        client_components=client_components,
        scoring_by_slot=scoring_by_slot,
        baseline_tokens=(baseline or {}).get("tokens") or [],
        client_tokens=client_tokens,
        provenance=prov,
    )

    package_path = None
    if status != "failure" and output_dir:
        package_path = write_package(tree, output_dir)

    return ThemeGeneratorOutput(
        status=status,
        package_path=package_path,
        provenance=prov,
        blocking_warnings=warnings,
        unmapped_components=unmapped,
        cost_summary=CostSummary(),  # Phase 1 = zero invocations
        non_deterministic=True,
    )


def _route_reconciliation(result: ReconciliationResult) -> tuple[bool, str, Optional[str]]:
    """Confidence-driven routing (Decision #4 / spec §6.2).

    Returns (emit, derivation_kind, blocking_warning_code). authoritative/high →
    emit, no warning; medium → emit + warning (proceed-with-warning); unresolved or
    flag_review → do NOT emit, warning only (no fabrication).
    """
    if result.outcome == "flag_review" or result.confidence == "unresolved":
        code = "agent_abstained" if result.outcome == "flag_review" else "agent_confidence_below_threshold"
        return False, "agent_flagged_review", code
    kind = "agent_reconciled" if result.outcome == "map" else "customer_passthrough"
    if result.confidence == "medium":
        return True, kind, "agent_confidence_below_threshold"
    return True, kind, None  # authoritative / high


def generate_theme(
    *,
    customer_slug: str,
    scope: str,
    baseline: dict,
    client_components: list[Any],
    client_tokens: list[Any],
    scoring_by_slot: dict[str, Any],
    reconciler: Optional[Reconciler] = None,
    reviewer: Optional[CohesionReviewer] = None,
    breaker: Optional[CircuitBreaker] = None,
    timestamp: Optional[str] = None,
    output_dir: Optional[str] = None,
) -> ThemeGeneratorOutput:
    """Full v0.1 pipeline: deterministic Phase 1 + agentic Phase 2 (reconciliation +
    cohesion review). The reconciler/reviewer are INJECTED — CI passes the
    deterministic Mock (no LLM); a live run passes the Agno-agent implementations.

    Phase 2 re-processes only the Phase-1 *deferred* slots (kind=agent_flagged_review):
    each is reconciled once (map/passthrough/flag_review), routed by confidence
    (§6.2), and the provenance ledger + package tree + unmapped register are updated
    in place. A CircuitBreaker hard-caps invocations (Probe 3); on trip, remaining
    deferrals are flagged for human review, never fabricated.
    """
    reconciler = reconciler or MockReconciler()
    reviewer = reviewer or MockCohesionReviewer()
    breaker = breaker or CircuitBreaker()

    meta = (baseline or {}).get("meta") or {}
    prov = ProvenanceSummary(
        baseline_source_ref=meta.get("baseline_ref"),
        baseline_cem_size_bytes=int(meta.get("cem_size_bytes") or 0),
        generation_timestamp=timestamp or _now_iso(),
        model_routing=f"{getattr(reconciler, 'name', 'unknown')} reconciler",
    )
    tree, prov, unmapped, warnings, _status = build_deterministic_package(
        customer_slug=customer_slug, scope=scope, baseline=baseline,
        client_components=client_components, scoring_by_slot=scoring_by_slot,
        baseline_tokens=(baseline or {}).get("tokens") or [], client_tokens=client_tokens,
        provenance=prov,
    )
    if warnings:  # missing inputs → fail-loud, no Phase 2
        return ThemeGeneratorOutput(status="failure", provenance=prov, blocking_warnings=warnings,
                                    unmapped_components=unmapped, non_deterministic=True)

    baseline_components = (baseline or {}).get("components") or []
    client_by_slot = {(_slot_name_local(c)): c for c in client_components}
    deriv_by_slot = {d.slot: d for d in prov.derivations}
    unmapped_by_slot = {u.slot: u for u in unmapped}

    # --- Phase 2a: fine-grained reconciliation of deferred slots ---
    deferred = [d.slot for d in prov.derivations if d.kind == "agent_flagged_review"]
    for slot in deferred:
        if not breaker.allow_fine_grained():
            warnings.append(BlockingWarning(code="agent_confidence_below_threshold", component_slot=slot,
                                            detail="circuit breaker tripped; not reconciled",
                                            recommended_action="human_review"))
            continue
        result = reconciler.reconcile(slot=slot, client_component=client_by_slot.get(slot),
                                      baseline_components=baseline_components,
                                      scoring=scoring_by_slot.get(slot))
        emit, kind, warn_code = _route_reconciliation(result)
        d = deriv_by_slot[slot]
        d.kind, d.confidence, d.baseline_ref, d.rationale = kind, result.confidence, result.baseline_ref, result.rationale
        if emit:
            tree[f"src/elements/{slugify(slot)}.ts"] = _element_module(slot, result.baseline_ref, kind)
            # resolved → drop from the unmapped register unless it's an explicit passthrough
            if slot in unmapped_by_slot and kind != "customer_passthrough":
                unmapped_by_slot.pop(slot)
        if warn_code:
            warnings.append(BlockingWarning(code=warn_code, component_slot=slot, detail=result.rationale,
                                            recommended_action="human_review"))

    # rebuild confidence summary + unmapped from the mutated ledger
    prov.confidence_summary = _confidence_summary(prov.derivations)
    unmapped = list(unmapped_by_slot.values())

    # --- Phase 2b: coarse cohesion review (one pass) ---
    cohesion_coherent: Optional[bool] = None
    cohesion_issues: list[str] = []
    flagged_remaining = sum(1 for d in prov.derivations if d.kind == "agent_flagged_review")
    if breaker.allow_coarse():
        verdict = reviewer.review(package_summary={
            "component_count": len(prov.derivations), "unresolved_count": flagged_remaining,
        })
        cohesion_coherent, cohesion_issues = verdict.coherent, list(verdict.issues)
        if not verdict.coherent:
            warnings.append(BlockingWarning(code="agent_disagreement", detail="; ".join(verdict.issues) or "cohesion issues",
                                            recommended_action="human_review"))

    # --- re-render provenance-dependent artifacts + finalise ---
    prov.model_routing = f"{getattr(reconciler, 'name', 'unknown')} reconciler / {getattr(reviewer, 'name', 'unknown')} cohesion"
    tree["package.json"] = render_package_json(customer_slug, scope, prov)
    tree["docs/PROVENANCE.md"] = render_provenance_md(prov)

    status = "partial" if (flagged_remaining or breaker.tripped) else "success"
    package_path = write_package(tree, output_dir) if output_dir else None
    # deterministic observability (Probe 3): expected = one fine-grained call per deferred slot;
    # anomaly fires on >2x that, or on a breaker trip. Bookkeeping, not an agent.
    expected_fg = len(deferred)
    anomaly = assess_anomaly(expected_fg, breaker.fine_grained_used, breaker.tripped)
    # token totals (VT-8): summed from any agent exposing usage(); Mock has none → stays 0.
    r_in, r_out = _usage_of(reconciler)
    c_in, c_out = _usage_of(reviewer)
    return ThemeGeneratorOutput(
        status=status, package_path=package_path, provenance=prov, blocking_warnings=warnings,
        unmapped_components=unmapped,
        cost_summary=CostSummary(fine_grained_invocations=breaker.fine_grained_used,
                                 coarse_grained_invocations=breaker.coarse_used,
                                 breaker_tripped=breaker.tripped,
                                 expected_fine_grained=expected_fg, anomaly=anomaly,
                                 total_input_tokens=r_in + c_in, total_output_tokens=r_out + c_out),
        cohesion_coherent=cohesion_coherent, cohesion_issues=cohesion_issues, non_deterministic=True,
    )


def _usage_of(obj) -> tuple[int, int]:
    """(input, output) tokens from an agent exposing usage(); (0, 0) for the Mock (no such method)."""
    fn = getattr(obj, "usage", None)
    if callable(fn):
        try:
            u = fn()
            return int(u[0]), int(u[1])
        except Exception:  # noqa: BLE001 — observability must never break a run
            return 0, 0
    return 0, 0


def _slot_name_local(c: Any) -> str:
    return (c.get("name") if isinstance(c, dict) else getattr(c, "name", None)) or ""


def _confidence_summary(derivations: list[ComponentDerivation]):
    from agents.theme_generator.models import ConfidenceSummary
    cs = ConfidenceSummary()
    for d in derivations:
        setattr(cs, d.confidence, getattr(cs, d.confidence) + 1)
    return cs


def input_gathering_executor(step_input: StepInput, **kwargs) -> StepOutput:
    """Step 1 — gather + validate inputs from the run's additional_data (SP-6 fail-loud)."""
    data = getattr(step_input, "additional_data", None) or {}
    bundle = gather_inputs(data)
    return StepOutput(step_name=STEP_NAME_INPUT, content=bundle, success=not bundle["blocking"])


def deterministic_transform_executor(step_input: StepInput, **kwargs) -> StepOutput:
    """Step 2 — run the deterministic transform on Step 1's gathered bundle.

    Reads Step 1 by name across the step boundary (the proven get_step_output
    pattern). If Step 1 flagged missing inputs, surfaces a failure envelope
    without attempting a build (SP-6: no fabrication on missing substrate).
    """
    gathered = step_input.get_step_content(STEP_NAME_INPUT) or {}
    if gathered.get("blocking"):
        env = ThemeGeneratorOutput(
            status="failure",
            blocking_warnings=[BlockingWarning(**b) for b in gathered["blocking"]],
        )
        return StepOutput(step_name=STEP_NAME_TRANSFORM, content=env.model_dump(), success=False)

    data = getattr(step_input, "additional_data", None) or {}
    env = generate_theme_phase1(
        customer_slug=gathered["customer_slug"],
        scope=gathered["scope"],
        baseline=gathered["baseline"],
        client_components=gathered["client_components"],
        client_tokens=gathered["client_tokens"],
        scoring_by_slot=gathered["scoring_by_slot"],
        output_dir=data.get("output_dir"),
    )
    return StepOutput(step_name=STEP_NAME_TRANSFORM, content=env.model_dump(),
                      success=env.status != "failure")


def _select_agents(data: dict, seed: int | None = None):
    """Choose Mock (CI/default) vs. real Agno agents for Phase 2.

    Real agents fire only when explicitly opted in (additional_data.use_real_agent
    or THEME_GEN_USE_REAL_AGENT env) — mirrors 3b's HELIX_TESTBENCH_USE_REAL_LLM
    gate so CI never makes a live, billable call. The real agents get temp=0 + the
    per-engagement ``seed`` (Decision #5). Returns (reconciler, reviewer);
    (None, None) → generate_theme defaults to the deterministic Mock.
    """
    flag = str(data.get("use_real_agent") or os.environ.get("THEME_GEN_USE_REAL_AGENT", "")).lower()
    if flag in ("1", "true", "yes"):
        from agents.theme_generator.reconciliation import AgentCohesionReviewer, AgentReconciler
        return AgentReconciler(seed=seed), AgentCohesionReviewer(seed=seed)
    return None, None


def full_generation_executor(step_input: StepInput, **kwargs) -> StepOutput:
    """Full pipeline step (Phase 1 deterministic + Phase 2 agentic + cohesion).

    Reads Step 1's gathered bundle; on missing inputs surfaces a failure envelope
    without a build (SP-6). Picks Mock vs. real agents via _select_agents.
    """
    gathered = step_input.get_step_content(STEP_NAME_INPUT) or {}
    if gathered.get("blocking"):
        env = ThemeGeneratorOutput(
            status="failure",
            blocking_warnings=[BlockingWarning(**b) for b in gathered["blocking"]],
        )
        return StepOutput(step_name=STEP_NAME_GENERATE, content=env.model_dump(), success=False)

    data = getattr(step_input, "additional_data", None) or {}
    # per-engagement seed (Decision #5): deterministic from (customer, timestamp)
    seed = engagement_seed(gathered["customer_slug"], data.get("engagement_timestamp") or _now_iso())
    reconciler, reviewer = _select_agents(data, seed=seed)
    env = generate_theme(
        customer_slug=gathered["customer_slug"], scope=gathered["scope"],
        baseline=gathered["baseline"], client_components=gathered["client_components"],
        client_tokens=gathered["client_tokens"], scoring_by_slot=gathered["scoring_by_slot"],
        reconciler=reconciler, reviewer=reviewer, output_dir=data.get("output_dir"),
    )
    return StepOutput(step_name=STEP_NAME_GENERATE, content=env.model_dump(),
                      success=env.status != "failure")
