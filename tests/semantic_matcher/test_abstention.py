"""Contract + behavioural tests for LLM abstention (spec v3.1).

Covers: LLMMatchResult schema, MockLLM oracle abstention + env override, harness
routing to unmapped 'llm_abstained', abstention metrics, and scenario 10.

Updated for spec v3.2 (defense-layers-2-3): Layer 2 (name/type coherence) now
precedes Layer 1, so scenario 10 routes to 'name_type_incoherent'; the Layer 1
abstention mechanism + routing remain covered here for COHERENT unmappable tokens.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from agents.semantic_matcher.models import LLMMatchResult
from tests.semantic_matcher.testbench.llm_path import MockLLM
from tests.semantic_matcher.testbench.run_testbench import compute_metrics, run, run_records


def _color(hex_str: str) -> dict:
    r, g, b = (int(hex_str[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return {"colorSpace": "srgb", "components": [r, g, b], "alpha": 1, "hex": hex_str.upper()}


def _tok(name, path, category, layer, dtcg_type, value) -> dict:
    return {"name": name, "path": path, "category": category,
            "layer": layer, "dtcg_type": dtcg_type, "value": value}


# --------------------------------------------------------------------------- schema
def test_schema_unmappable_allows_null_baseline_var():
    r = LLMMatchResult(outcome="unmappable", baseline_var=None, confidence="unresolved",
                       rationale="no plausible match")
    assert r.outcome == "unmappable" and r.baseline_var is None


def test_schema_map_requires_baseline_var():
    with pytest.raises((ValidationError, ValueError)):
        LLMMatchResult(outcome="map", baseline_var=None, confidence="high", rationale="x")


def test_schema_unmappable_rejects_non_null_baseline_var():
    with pytest.raises((ValidationError, ValueError)):
        LLMMatchResult(outcome="unmappable", baseline_var="--helix-x", rationale="x")


# --------------------------------------------------------------------------- MockLLM
def _unmappable_client_and_candidates():
    # client name says 'dimension' but declared color; candidates are colors -> low agg
    client = _tok("--helix-dimension-spacing-row-sm", "dimension/spacing/row/sm",
                  "color", "semantic", "color", "{dimension.spacing.row.sm}")
    cands = [_tok("--helix-color-brand1-50", "color/brand1/50", "color", "primitive",
                  "color", _color("#E7F5FF")),
             _tok("--helix-color-brand1-100", "color/brand1/100", "color", "primitive",
                  "color", _color("#D0EBFF"))]
    return client, cands


def test_mockllm_abstains_when_rate_1(monkeypatch):
    monkeypatch.setenv("MOCKLLM_ABSTENTION_RATE_OVERRIDE", "1.0")
    client, cands = _unmappable_client_and_candidates()
    res = MockLLM().match(client, cands, expected_baseline_var=None)
    assert res.outcome == "unmappable"
    assert res.baseline_var is None
    assert res.rationale


def test_mockllm_does_not_abstain_when_rate_0(monkeypatch):
    monkeypatch.setenv("MOCKLLM_ABSTENTION_RATE_OVERRIDE", "0.0")
    client, cands = _unmappable_client_and_candidates()
    res = MockLLM().match(client, cands, expected_baseline_var=None)
    assert res.outcome == "map"
    assert res.baseline_var in {c["name"] for c in cands}


def test_mockllm_maps_when_expected_present(monkeypatch):
    monkeypatch.setenv("MOCKLLM_ABSTENTION_RATE_OVERRIDE", "1.0")
    # a genuine match: expected is set, value nails a candidate -> should MAP not abstain
    client = _tok("--helix-color-brand1-50", "color/brand1/50", "color", "primitive",
                  "color", _color("#E7F5FF"))
    cands = [_tok("--helix-color-brand1-50", "color/brand1/50", "color", "primitive",
                  "color", _color("#E7F5FF"))]
    res = MockLLM().match(client, cands, expected_baseline_var="--helix-color-brand1-50")
    assert res.outcome == "map"
    assert res.baseline_var == "--helix-color-brand1-50"


def test_mockllm_no_candidates_is_unmappable():
    res = MockLLM().match(_tok("--x", "x", "color", None, "color", _color("#000000")), [])
    assert res.outcome == "unmappable"


# --------------------------------------------------------------------------- harness routing
# NOTE (spec v3.2, defense-layers-2-3): Layer 2 (name/type coherence) now fires
# BEFORE the LLM, so the scenario-10 cases — whose NAME contradicts their declared
# TYPE — route to 'name_type_incoherent' (Layer 2) with PRECEDENCE over
# 'llm_abstained' (Layer 1). The scenario-10 tests below assert that new layered
# reality (they previously asserted llm_abstained / recall-gap reproduction, which
# Layer 2 intentionally supersedes — the predecessor flagged exactly this in its
# follow-ups). Layer 1's abstention MECHANISM stays covered by the MockLLM unit
# tests above; its harness ROUTING stays covered by the coherent-token test below
# (a token Layer 2 does NOT pre-empt still routes via Layer 1 when the LLM abstains).


class _AlwaysAbstainLLM:
    name = "stub-abstain"

    def match(self, client_token, candidates, *, expected_baseline_var=None, scoring_result=None):
        return LLMMatchResult(outcome="unmappable", baseline_var=None,
                              confidence="unresolved", rationale="stub abstention")


def test_layer1_abstention_routing_survives_for_coherent_tokens():
    """A COHERENT but unmappable token (name/path/type agree) is NOT pre-empted by
    Layer 2; when the LLM abstains it still routes to 'llm_abstained' (Layer 1)."""
    from tests.semantic_matcher.testbench.input_sources._common import load_baseline
    from tests.semantic_matcher.testbench.run_testbench import (
        build_baseline_enrichment, run_case)

    base = load_baseline()
    bbn = {r["name"]: r for r in base}
    enr = build_baseline_enrichment(base)
    client = _tok("--helix-color-zzz-nonexistent", "color/zzz/nonexistent", "color",
                  "primitive", "color", _color("#1F74C9"))
    case = {"mutation_class": "probe", "client_token": client,
            "expected_baseline_var": None, "note": ""}
    rec = run_case(case, base, bbn, set(bbn), _AlwaysAbstainLLM(), None, enr)
    assert rec["llm_invoked"] is True            # Layer 2 did NOT pre-empt (coherent token)
    assert rec["name_type_incoherent"] is False
    assert rec["llm_abstained"] is True
    assert rec["unmapped_reason"] == "llm_abstained"
    assert rec["post_validator_checks"]["vocabulary"] == "skipped"


# --------------------------------------------------------------------------- scenario 10
def test_scenario10_caught_by_layer2_abstention_on(monkeypatch):
    """With abstention ON, scenario 10 is caught STRUCTURALLY by Layer 2 (name/type
    incoherence), taking precedence over Layer 1 — nothing reaches abstention."""
    monkeypatch.setenv("MOCKLLM_ABSTENTION_RATE_OVERRIDE", "1.0")
    recs = run_records(input_source="regression")
    assert len(recs) == 10
    incoherent = [r for r in recs if r["unmapped_reason"] == "name_type_incoherent"]
    assert len(incoherent) >= 9, f"expected >=9/10 caught by Layer 2, got {len(incoherent)}"
    for r in incoherent:
        assert r["actual_baseline_var"] is None
        assert r["llm_invoked"] is False          # never reached the LLM
        assert r["coherence_check"]["verdict"] == "incoherent"
    m = compute_metrics(recs)
    assert m["name_type_incoherent_count"] == len(incoherent)
    assert m["unmapped_by_reason"].get("name_type_incoherent") == len(incoherent)


def test_scenario10_layer2_catches_without_abstention(monkeypatch):
    """ACID TEST (spec success criterion 4): with abstention DISABLED, Layer 2 alone
    must catch the recall-gap cases — zero wrong mappings, none rely on Layer 1."""
    monkeypatch.setenv("MOCKLLM_ABSTENTION_RATE_OVERRIDE", "0.0")
    recs = run_records(input_source="regression")
    assert len(recs) == 10
    incoherent = sum(1 for r in recs if r["unmapped_reason"] == "name_type_incoherent")
    abstentions = sum(1 for r in recs if r["llm_abstained"])
    wrong = sum(1 for r in recs if r["actual_baseline_var"] is not None
                and r["expected_baseline_var"] is None)
    assert abstentions == 0                        # Layer 1 is off
    assert incoherent >= 9, f"Layer 2 must catch >=9/10 without Layer 1, got {incoherent}"
    assert wrong == 0, "Layer 2 must eliminate the recall-gap wrong mappings"


# --------------------------------------------------------------------------- regression
def test_mutations_run_no_false_accept_with_abstention(monkeypatch):
    monkeypatch.setenv("MOCKLLM_ABSTENTION_RATE_OVERRIDE", "1.0")
    report = run(write=False, input_source="mutations")
    o = report["overall"]
    assert o["false_accept_rate_on_deterministic"] in (0.0, None)
    assert o["unmapped_precision"] == 1.0  # abstention only ADDS correct unmapped
    assert report["n_records"] == 160
