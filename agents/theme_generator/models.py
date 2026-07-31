"""3c Theme Generator — typed contracts (envelope + provenance + warnings).

Pydantic shapes for the 3c Theme Generator output envelope (spec §4.5), the
SP-6-extended blocking-warning (spec §6.3, Decision #4), and the provenance /
confidence summaries that make a generated customer package auditable (spec §4.4).

Phase 1 (this module + scaffolding.py + step.py) is the DETERMINISTIC foundation:
package scaffolding, high-confidence component forking, token substitution, and
passthrough handling. The agentic reconciliation + cohesion-review steps (spec
§5.2) are Phase 2 — their result shapes are declared here so the envelope is
stable across phases, but Phase 1 never populates the agent-only fields.

Confidence vocabulary is shared with 3b Semantic Matcher
(``agents.semantic_matcher.models.Confidence``): authoritative / high / medium /
unresolved. We re-use it verbatim so the two layers speak the same language.

SP-9: no config, keys, model names, or URLs live here — pure data contracts.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# Shared confidence vocabulary — same Literal 3b emits (single source of truth).
Confidence = Literal["authoritative", "high", "medium", "unresolved"]

# How a given component slot was produced. Deterministic values are the only ones
# Phase 1 emits; the agent_* values are Phase 2 (declared now for a stable envelope).
DerivationKind = Literal[
    "forked_from_baseline",      # deterministic: baseline component forked + tokens substituted
    "customer_passthrough",      # deterministic: no baseline equivalent, client's kept as-is
    "agent_reconciled",          # Phase 2: fine-grained agent proposed a mapping
    "agent_flagged_review",      # Phase 2: agent below-threshold → human review
]

ThemeGenStatus = Literal["success", "partial", "failure"]


class BlockingWarning(BaseModel):
    """SP-6 blocking-warning (Decision #4 extends SP-6 to agent outputs).

    Phase 1 raises the deterministic-origin codes (missing baseline / extraction,
    empty inputs). The agent-origin codes (spec §6.3) are Phase 2 but declared
    here so the envelope shape never changes between phases.
    """

    code: Literal[
        # deterministic-origin (Phase 1)
        "baseline_unavailable",
        "client_extraction_unavailable",
        "empty_baseline",
        "empty_client_extraction",
        # agent-origin (Phase 2, spec §6.3)
        "agent_confidence_below_threshold",
        "agent_abstained",
        "agent_disagreement",
        "variant_mismatch_unresolved",
    ]
    component_slot: Optional[str] = None  # affected component, when applicable
    detail: str = ""
    recommended_action: Literal[
        "human_review", "retry_with_context", "component_passthrough", "abort_run"
    ] = "human_review"


class ComponentDerivation(BaseModel):
    """One row of the provenance ledger: where a single component slot came from."""

    slot: str                              # component name / slot id
    kind: DerivationKind
    confidence: Confidence
    baseline_ref: Optional[str] = None     # baseline component this was forked from, if any
    rationale: str = ""


class ConfidenceSummary(BaseModel):
    """Distribution of component confidences across the generated package (spec §4.4)."""

    authoritative: int = 0
    high: int = 0
    medium: int = 0
    unresolved: int = 0

    @property
    def total(self) -> int:
        return self.authoritative + self.high + self.medium + self.unresolved


class UnmappedComponent(BaseModel):
    """A client component with no confident baseline mapping (from 3b's register)."""

    slot: str
    reason: Literal[
        "llm_abstained", "name_type_incoherent", "component_passthrough", "no_candidates"
    ]
    detail: str = ""


class ProvenanceSummary(BaseModel):
    """What came from the baseline vs. the client vs. a 3c agent (spec §4.4).

    Feeds both the customer-facing PROVENANCE.md and the ``package.json``
    provenance field (spec §4.3). Phase 1 populates the deterministic fields;
    agent-derived counts stay 0 until Phase 2.
    """

    baseline_source_ref: Optional[str] = None      # e.g. "master@<sha>"
    baseline_cem_size_bytes: int = 0
    generation_timestamp: Optional[str] = None      # ISO8601 (stamped by the caller, not here)
    engine_version: str = "0.1.0-3c"
    model_routing: Optional[str] = None             # resolved OPENAI_MODEL_ID (Phase 2 agents)
    derivations: list[ComponentDerivation] = Field(default_factory=list)
    confidence_summary: ConfidenceSummary = Field(default_factory=ConfidenceSummary)


class CostSummary(BaseModel):
    """LLM cost/observability rollup (spec §8, Probe-3-informed).

    Phase 1 is fully deterministic → zero invocations. Phase 2 populates the
    invocation counts + token totals; a hard circuit-breaker (Probe 3 rec) trips
    ``breaker_tripped`` and emits a blocking_warning rather than spending silently.
    """

    fine_grained_invocations: int = 0
    coarse_grained_invocations: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    breaker_tripped: bool = False
    # Anomaly observability (Probe 3): expected fine-grained calls for this component
    # count, and a flag when the actual count runs anomalously high. Deterministic —
    # this is a bookkeeping signal, NOT an agent. Extractable to a shared observability
    # harness later (the pattern is workflow-agnostic).
    expected_fine_grained: int = 0
    anomaly: Optional[str] = None


class ThemeGeneratorOutput(BaseModel):
    """3c Agno-level output envelope (spec §4.5).

    ``non_deterministic`` is ALWAYS True for 3c per Decision #5 — even a Phase-1
    run that happened to touch no agent is labelled non-deterministic so the
    envelope shape is honest about what 3c is.
    """

    status: ThemeGenStatus
    package_path: Optional[str] = None
    provenance: ProvenanceSummary = Field(default_factory=ProvenanceSummary)
    blocking_warnings: list[BlockingWarning] = Field(default_factory=list)
    unmapped_components: list[UnmappedComponent] = Field(default_factory=list)
    cost_summary: CostSummary = Field(default_factory=CostSummary)
    # Coarse cohesion-review result (Phase 2, spec §5.2.3). None on a Phase-1-only run.
    cohesion_coherent: Optional[bool] = None
    cohesion_issues: list[str] = Field(default_factory=list)
    non_deterministic: bool = True
