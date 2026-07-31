"""3c Theme Generator — Phase 2 (agentic) tests.

Shape/invariant + deterministic-oracle assertions (spec §7.3 — agent components use
shape/invariant, NOT exact-content, to avoid drift). The deterministic MockReconciler
(no live LLM, no cost) stands in for the fine-grained agent — same discipline as 3b's
MockLLM. Real Agno agents (AgentReconciler/AgentCohesionReviewer) are opt-in and never
exercised at CI.
"""
from __future__ import annotations

from agents.semantic_matcher.scoring import ScoredCandidate, ScoringResult, ScoringSignals
from agents.theme_generator import (
    CircuitBreaker,
    MockReconciler,
    ReconciliationResult,
    generate_theme,
)
from agents.theme_generator.reconciliation import MockCohesionReviewer
from agents.theme_generator.step import _route_reconciliation

TS = "2026-07-31T00:00:00.000Z"


def _scoring(matched_via, conf=None):
    return ScoringResult(
        top_candidates=[ScoredCandidate(
            baseline_var="baseline.Button",
            signals=ScoringSignals(name_similarity=0.9, path_overlap=0.5,
                                   value_distance=0.0, layer_alignment=True),
            aggregate_score=0.9, signal_families_agreeing=3)],
        margin_top1_top2=0.4, matched_via=matched_via, deterministic_confidence=conf,
    )


def _baseline(components):
    return {"meta": {"baseline_ref": "master@abc", "cem_size_bytes": 10},
            "tokens": [{"name": "--c", "value": "#000"}], "components": components}


# --------------------------------------------------------------------------- #
# MockReconciler oracle behaviour
# --------------------------------------------------------------------------- #

def test_mock_reconciler_exact_name_maps_high():
    r = MockReconciler().reconcile(slot="BrandHero", client_component={"name": "BrandHero"},
                                   baseline_components=[{"name": "BrandHero"}, {"name": "Button"}], scoring=None)
    assert r.outcome == "map" and r.confidence == "high" and r.baseline_ref == "BrandHero"


def test_mock_reconciler_no_match_flags_review():
    r = MockReconciler().reconcile(slot="Zzqptar", client_component={"name": "Zzqptar"},
                                   baseline_components=[{"name": "Button"}], scoring=None)
    assert r.outcome == "flag_review" and r.confidence == "unresolved"


# --------------------------------------------------------------------------- #
# Confidence-driven routing (§6.2 / Decision #4)
# --------------------------------------------------------------------------- #

def test_route_high_emits_no_warning():
    assert _route_reconciliation(ReconciliationResult(outcome="map", baseline_ref="b", confidence="high")) \
        == (True, "agent_reconciled", None)


def test_route_medium_emits_with_warning():
    emit, kind, code = _route_reconciliation(ReconciliationResult(outcome="map", baseline_ref="b", confidence="medium"))
    assert emit and kind == "agent_reconciled" and code == "agent_confidence_below_threshold"


def test_route_unresolved_does_not_emit():
    emit, kind, code = _route_reconciliation(ReconciliationResult(outcome="flag_review", confidence="unresolved"))
    assert emit is False and kind == "agent_flagged_review" and code == "agent_abstained"


def test_route_passthrough_high_emits():
    emit, kind, code = _route_reconciliation(ReconciliationResult(outcome="passthrough", confidence="high"))
    assert emit and kind == "customer_passthrough" and code is None


# --------------------------------------------------------------------------- #
# Full pipeline (Phase 1 + Phase 2) with the mock oracle
# --------------------------------------------------------------------------- #

def _run(components, scoring, baseline_components, reconciler=None, breaker=None):
    return generate_theme(
        customer_slug="acme", scope="msq-dx", baseline=_baseline(baseline_components),
        client_components=components, client_tokens=[], scoring_by_slot=scoring,
        reconciler=reconciler, reviewer=MockCohesionReviewer(), breaker=breaker, timestamp=TS,
    )


def test_deferred_slot_reconciled_to_success():
    # BrandHero is deferred by Phase 1 (llm_required) then mapped high by the oracle.
    env = _run(
        components=[{"name": "Button"}, {"name": "BrandHero"}],
        scoring={"Button": _scoring("deterministic", "high"), "BrandHero": _scoring("llm_required")},
        baseline_components=[{"name": "Button"}, {"name": "BrandHero"}],
    )
    assert env.status == "success"                       # nothing left flagged
    assert env.cost_summary.fine_grained_invocations == 1  # one deferred slot reconciled
    assert env.cost_summary.coarse_grained_invocations == 1
    kinds = {d.slot: d.kind for d in env.provenance.derivations}
    assert kinds["BrandHero"] == "agent_reconciled"
    assert env.cohesion_coherent is True
    assert env.non_deterministic is True


def test_deferred_slot_unresolved_stays_partial():
    env = _run(
        components=[{"name": "Button"}, {"name": "Zzqptar"}],
        scoring={"Button": _scoring("deterministic", "high"), "Zzqptar": _scoring("llm_required")},
        baseline_components=[{"name": "Button"}],  # no match for Zzqptar
    )
    assert env.status == "partial"                        # Zzqptar still flagged
    assert any(w.code == "agent_abstained" and w.component_slot == "Zzqptar" for w in env.blocking_warnings)
    assert env.cohesion_coherent is False                 # flagged slot → cohesion issue
    assert any(w.code == "agent_disagreement" for w in env.blocking_warnings)


def test_circuit_breaker_trips_and_flags_remaining():
    breaker = CircuitBreaker(max_fine_grained=0, max_coarse_chunks=6)  # allow no fine-grained calls
    env = _run(
        components=[{"name": "BrandHero"}],
        scoring={"BrandHero": _scoring("llm_required")},
        baseline_components=[{"name": "BrandHero"}],
        breaker=breaker,
    )
    assert env.cost_summary.breaker_tripped is True
    assert env.cost_summary.fine_grained_invocations == 0
    assert env.status == "partial"
    assert any("circuit breaker" in (w.detail or "") for w in env.blocking_warnings)


def test_full_pipeline_missing_client_is_failure():
    env = generate_theme(customer_slug="acme", scope="msq-dx", baseline=_baseline([{"name": "Button"}]),
                         client_components=[], client_tokens=[], scoring_by_slot={}, timestamp=TS)
    assert env.status == "failure"
    assert "empty_client_extraction" in {w.code for w in env.blocking_warnings}


def test_medium_confidence_emits_with_warning():
    class MediumReconciler:
        name = "medium-fake"
        def reconcile(self, **kw):
            return ReconciliationResult(outcome="map", baseline_ref="baseline.X", confidence="medium",
                                        rationale="weak")
    env = _run(
        components=[{"name": "Widget"}],
        scoring={"Widget": _scoring("llm_required")},
        baseline_components=[{"name": "Button"}],
        reconciler=MediumReconciler(),
    )
    # medium → emitted (agent_reconciled) but a below-threshold warning is raised
    kinds = {d.slot: d.kind for d in env.provenance.derivations}
    assert kinds["Widget"] == "agent_reconciled"
    assert any(w.code == "agent_confidence_below_threshold" and w.component_slot == "Widget"
               for w in env.blocking_warnings)
