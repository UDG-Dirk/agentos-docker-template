"""3d Component Code Generator — typed contracts.

Consumes 3c's ``ThemeGeneratorOutput`` (source-agnostic; UC1/UC2 today) and produces a customer
component-library package of real Lit + TypeScript code. Two generation paths (D3d-1 / D3d-3):

  * **Path A — deterministic fork** (Phase 1): the 3c slot maps to a baseline component
    (``forked_from_baseline`` / ``agent_reconciled`` with a baseline_ref). 3d reads the baseline
    Lit source from helix-code (READ-ONLY) and deterministically re-tags / re-classes / re-tokenises
    it into the customer namespace. Byte-identical by construction → no structural gate, no LLM.
  * **Path B — from-spec** (Phase 2): no baseline (``customer_passthrough`` /
    ``agent_flagged_review``) → LLM generation from thin Figma metadata, gated by the structural
    validation gate (Adjustment 1). Phase 1 routes these to DEFERRED and never fabricates.

Confidence vocabulary is the shared one (authoritative/high/medium/unresolved). SP-9: no config here.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from agents.theme_generator.models import Confidence  # shared vocabulary (single source)

# How an element's code was produced.
GenerationPath = Literal[
    "fork_deterministic",   # Path A, Phase 1: deterministic fork of a baseline component
    "fork_refined",         # Path A, Phase 2: fork + LLM refinement from Figma metadata
    "from_spec",            # Path B, Phase 2: LLM generation, no baseline
    "deferred",             # Phase 1 placeholder for a Path-B element (not yet generated)
]

CCGStatus = Literal["success", "partial", "failure"]


class BlockingWarning(BaseModel):
    """SP-6 / SP-6-extended blocking warning for 3d."""

    code: Literal[
        "input_unavailable",              # missing/empty 3c package
        "baseline_source_unavailable",    # Path A: baseline_ref names a component not in helix-code
        "structural_gate_failed",         # Phase 2: generated code failed the structural gate after retry
        "generation_below_threshold",     # Phase 2: agent confidence below threshold
        "generation_failed",              # Phase 2: agent error / non-schema output
        "deferred_to_phase_2",            # Phase 1: Path-B element not yet implemented
    ]
    element_slot: Optional[str] = None
    detail: str = ""
    recommended_action: Literal["human_review", "retry", "await_phase_2", "abort_run"] = "human_review"


class StructuralGateResult(BaseModel):
    """Deterministic structural checks on generated code (Adjustment 1 / VT-17).

    Declared now; APPLIED in Phase 2 to Path-B (and Path-A-refined) output. Path-A pure
    deterministic fork is byte-identical by construction and skips the gate (``applied=False``).
    """

    applied: bool = False
    ts_parseable: Optional[bool] = None
    lit_pattern_ok: Optional[bool] = None      # @customElement + @property + lit imports + extends LitElement
    naming_ok: Optional[bool] = None           # customer-prefixed tag, PascalCase class
    token_namespace_ok: Optional[bool] = None  # var(--{customer}-*), not var(--helix-*)
    registration_ok: Optional[bool] = None     # @customElement tag matches file/class
    failures: list[str] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        if not self.applied:
            return True  # deterministic fork — gate not applicable
        checks = [self.ts_parseable, self.lit_pattern_ok, self.naming_ok,
                  self.token_namespace_ok, self.registration_ok]
        return all(c for c in checks) and not self.failures


class ElementGenerationResult(BaseModel):
    """One generated element's outcome + provenance."""

    slot: str
    element_tag: Optional[str] = None      # e.g. "acme-icon-button"
    class_name: Optional[str] = None       # e.g. "AcmeIconButtonElement"
    file_path: Optional[str] = None        # repo-relative within the customer package
    path: GenerationPath
    baseline_ref: Optional[str] = None
    confidence: Confidence
    structural_gate: StructuralGateResult = Field(default_factory=StructuralGateResult)
    rationale: str = ""


class ComponentGenerationSummary(BaseModel):
    """Roll-up of per-element results (spec §4)."""

    total: int = 0
    fork_deterministic: int = 0
    fork_refined: int = 0
    from_spec: int = 0
    deferred: int = 0
    gate_passed: int = 0
    gate_failed: int = 0


class ProvenanceExtension(BaseModel):
    """3d's addition to the customer package's PROVENANCE.md (extends 3c's provenance)."""

    generator_version: str = "0.1.0-3d"
    baseline_source_ref: Optional[str] = None
    model_routing: Optional[str] = None
    elements: list[ElementGenerationResult] = Field(default_factory=list)


class CostSummary(BaseModel):
    """LLM cost/observability rollup (mirrors 3c; deterministic bookkeeping). Phase 1 = zero calls."""

    fine_grained_invocations: int = 0
    coarse_grained_invocations: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    breaker_tripped: bool = False
    expected_fine_grained: int = 0
    anomaly: Optional[str] = None


class ComponentCodeGeneratorOutput(BaseModel):
    """3d Agno-level output envelope. ``non_deterministic`` is True whenever any LLM path ran."""

    status: CCGStatus
    package_path: Optional[str] = None
    summary: ComponentGenerationSummary = Field(default_factory=ComponentGenerationSummary)
    provenance: ProvenanceExtension = Field(default_factory=ProvenanceExtension)
    blocking_warnings: list[BlockingWarning] = Field(default_factory=list)
    cost_summary: CostSummary = Field(default_factory=CostSummary)
    non_deterministic: bool = False
