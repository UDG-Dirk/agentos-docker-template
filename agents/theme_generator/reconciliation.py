"""3c Theme Generator — Phase 2 agentic reconciliation + cohesion review (spec §5.2).

Two agent surfaces, each behind an injectable interface so CI runs a deterministic
oracle (no live LLM, no cost) while real runs use an Agno agent — the exact
Mock/Real split 3b's testbench uses (tests/semantic_matcher/testbench/llm_path.py):

  * Reconciler        — fine-grained, ONE call per deferred/unmapped component
    (the Phase-1 "defer" cases: matched_via=llm_required or a below-high
    deterministic match). Proposes map / passthrough / flag-for-review.
  * CohesionReviewer  — coarse, ONE call per run over the assembled package.

Confidence-driven routing (Decision #4 / spec §6.2), applied by the orchestration:
  authoritative|high → emit component;
  medium             → emit component + blocking_warning (proceed-with-warning);
  unresolved         → do NOT emit — blocking_warning only (no fabrication).

A CircuitBreaker (Probe 3 recommendation) hard-caps invocations and trips fail-loud
rather than spending silently. SP-9: the real agent's model comes from
``app.settings.default_chat_model()`` (OPENAI_MODEL_ID) — never hardcoded.
"""
from __future__ import annotations

import os
from typing import Any, Literal, Optional, Protocol

from pydantic import BaseModel

from agents.semantic_matcher.scoring import name_similarity
from agents.theme_generator.models import Confidence

# --- circuit-breaker defaults (Probe 3 rec; env-overridable, SP-9) -----------
_DEFAULT_MAX_FINE_GRAINED = 200
_DEFAULT_MAX_COARSE_CHUNKS = 6


class ReconciliationResult(BaseModel):
    """One fine-grained agent decision for a single unmapped component."""

    outcome: Literal["map", "passthrough", "flag_review"]
    baseline_ref: Optional[str] = None   # set when outcome="map"
    confidence: Confidence = "unresolved"
    rationale: str = ""

    def model_post_init(self, _ctx) -> None:  # contract check (mirrors 3b LLMMatchResult)
        if self.outcome == "map" and not self.baseline_ref:
            raise ValueError("outcome='map' requires a baseline_ref")


class CohesionVerdict(BaseModel):
    """Coarse whole-package coherence result (spec §5.2.3)."""

    coherent: bool
    issues: list[str] = []
    confidence: Confidence = "high"
    rationale: str = ""


class Reconciler(Protocol):
    def reconcile(self, *, slot: str, client_component: Any,
                  baseline_components: list[Any], scoring: Any) -> ReconciliationResult: ...


class CohesionReviewer(Protocol):
    def review(self, *, package_summary: dict) -> CohesionVerdict: ...


class CircuitBreaker:
    """Hard invocation cap (Probe 3). Trips fail-loud instead of spending silently.

    ``allow_fine_grained`` / ``allow_coarse`` record a slot and return False once
    the cap is hit; callers then flag remaining work for human review.
    """

    def __init__(self, max_fine_grained: int | None = None, max_coarse_chunks: int | None = None) -> None:
        self.max_fine_grained = max_fine_grained if max_fine_grained is not None else _env_int(
            "THEME_GEN_MAX_FINE_GRAINED", _DEFAULT_MAX_FINE_GRAINED)
        self.max_coarse_chunks = max_coarse_chunks if max_coarse_chunks is not None else _env_int(
            "THEME_GEN_MAX_COARSE_CHUNKS", _DEFAULT_MAX_COARSE_CHUNKS)
        self.fine_grained_used = 0
        self.coarse_used = 0
        self.tripped = False

    def allow_fine_grained(self) -> bool:
        if self.fine_grained_used >= self.max_fine_grained:
            self.tripped = True
            return False
        self.fine_grained_used += 1
        return True

    def allow_coarse(self) -> bool:
        if self.coarse_used >= self.max_coarse_chunks:
            self.tripped = True
            return False
        self.coarse_used += 1
        return True


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _slot_name(c: Any) -> str:
    return (c.get("name") if isinstance(c, dict) else getattr(c, "name", None)) or ""


# --------------------------------------------------------------------------- #
# Deterministic oracle (CI default — no LLM, no cost)
# --------------------------------------------------------------------------- #
class MockReconciler:
    """Deterministic oracle stand-in for the fine-grained agent (CI default).

    Name-similarity against baseline component names is the disambiguator (reuses
    3b's ``name_similarity``): >=0.85 → map/high, >=0.5 → map/medium, else
    flag_review/unresolved. Fully reproducible; real-agent behaviour is measured
    at hardening, not here (same discipline as 3b's MockLLM).
    """

    name = "mock"

    def reconcile(self, *, slot, client_component, baseline_components, scoring) -> ReconciliationResult:
        best_name, best_sim = None, 0.0
        for b in baseline_components or []:
            bn = _slot_name(b)
            sim = name_similarity(slot, bn)
            if sim > best_sim:
                best_name, best_sim = bn, sim
        if best_name and best_sim >= 0.85:
            return ReconciliationResult(outcome="map", baseline_ref=best_name, confidence="high",
                                        rationale=f"name similarity {best_sim:.2f} to baseline '{best_name}'")
        if best_name and best_sim >= 0.5:
            return ReconciliationResult(outcome="map", baseline_ref=best_name, confidence="medium",
                                        rationale=f"weak name similarity {best_sim:.2f} to '{best_name}'")
        return ReconciliationResult(outcome="flag_review", confidence="unresolved",
                                    rationale="no baseline within name-similarity threshold")


class MockCohesionReviewer:
    """Deterministic oracle for the coarse cohesion pass (CI default)."""

    name = "mock"

    def review(self, *, package_summary: dict) -> CohesionVerdict:
        # deterministic heuristic: a package with any flagged/unresolved slot is "issues found"
        flagged = int(package_summary.get("unresolved_count", 0) or 0)
        if flagged:
            return CohesionVerdict(coherent=False,
                                   issues=[f"{flagged} component(s) unresolved / flagged for review"],
                                   confidence="high", rationale="deterministic cohesion heuristic")
        return CohesionVerdict(coherent=True, issues=[], confidence="high",
                               rationale="deterministic cohesion heuristic: no flagged slots")


# --------------------------------------------------------------------------- #
# Real Agno-agent path (gated; used in live runs, not CI — mirrors 3b RealLLM)
# --------------------------------------------------------------------------- #
_RECONCILE_INSTRUCTIONS = (
    "You reconcile ONE client design-system component against a baseline component library. "
    "Given the client component and the baseline components, decide exactly one: map it to the single "
    "best baseline component (outcome='map', set baseline_ref), keep it as a customer-specific "
    "component with no baseline equivalent (outcome='passthrough'), or flag it for human review "
    "(outcome='flag_review'). ALWAYS give a confidence (authoritative|high|medium|unresolved) and a "
    "rationale. Abstain to 'flag_review' with confidence='unresolved' when genuinely unsure — never "
    "invent a mapping."
)
_COHESION_INSTRUCTIONS = (
    "You review a generated customer component-library package for internal consistency: cross-component "
    "naming, token usage, and style coherence. Return coherent=true/false, a list of concrete issues (empty "
    "if none), a confidence, and a rationale. Be specific and terse."
)


class AgentReconciler:
    """Real fine-grained reconciler — Agno agent, structured output (live runs only)."""

    name = "agent"

    def __init__(self) -> None:
        from agno.agent import Agent

        from app.settings import default_chat_model
        self._agent = Agent(
            name="3c-reconciler",
            model=default_chat_model(),  # OPENAI_MODEL_ID via LiteLLM (SP-9), temp handled by caller/env
            instructions=[_RECONCILE_INSTRUCTIONS],
            output_schema=ReconciliationResult,
        )

    def reconcile(self, *, slot, client_component, baseline_components, scoring) -> ReconciliationResult:
        baseline_names = [_slot_name(b) for b in (baseline_components or [])]
        prompt = (f"Client component: {slot}\nClient detail: {client_component}\n"
                  f"Baseline components: {baseline_names}\n"
                  f"3b scoring (if any): {scoring}\nDecide: map / passthrough / flag_review.")
        return self._agent.run(input=prompt).content


class AgentCohesionReviewer:
    """Real coarse cohesion reviewer — Agno agent, structured output (live runs only)."""

    name = "agent"

    def __init__(self) -> None:
        from agno.agent import Agent

        from app.settings import default_chat_model
        self._agent = Agent(
            name="3c-cohesion-reviewer",
            model=default_chat_model(),
            instructions=[_COHESION_INSTRUCTIONS],
            output_schema=CohesionVerdict,
        )

    def review(self, *, package_summary: dict) -> CohesionVerdict:
        return self._agent.run(input=f"Package summary: {package_summary}. Review for cohesion.").content
