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

from datetime import UTC, datetime
from typing import Any, Optional

from agno.workflow.types import StepInput, StepOutput

from agents.theme_generator.models import (
    BlockingWarning,
    CostSummary,
    ProvenanceSummary,
    ThemeGeneratorOutput,
)
from agents.theme_generator.scaffolding import build_deterministic_package, write_package

STEP_NAME_INPUT = "theme-input-gathering"
STEP_NAME_TRANSFORM = "theme-deterministic-transform"

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
