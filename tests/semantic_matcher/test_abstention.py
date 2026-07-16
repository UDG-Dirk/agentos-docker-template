"""Contract + behavioural tests for LLM abstention (spec v3.1).

Covers: LLMMatchResult schema, MockLLM oracle abstention + env override, harness
routing to unmapped 'llm_abstained', abstention metrics, and scenario 10
(recall-gap reproduction with abstention on vs off).
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
def test_harness_routes_abstention_to_unmapped(monkeypatch):
    monkeypatch.setenv("MOCKLLM_ABSTENTION_RATE_OVERRIDE", "1.0")
    recs = run_records(input_source="regression")
    abst = [r for r in recs if r["llm_abstained"]]
    assert abst, "expected at least one abstention in scenario 10"
    for r in abst:
        assert r["actual_baseline_var"] is None
        assert r["unmapped_reason"] == "llm_abstained"
        assert r["post_validator_checks"]["vocabulary"] == "skipped"
        assert r["post_validator_checks"]["category_alignment"] == "skipped"
        assert r["post_validator_passed"] is True  # rationale present
    m = compute_metrics(recs)
    assert m["llm_abstention_count"] == len(abst)
    assert m["llm_abstention_rate"] is not None


# --------------------------------------------------------------------------- scenario 10
def test_scenario10_default_abstention(monkeypatch):
    monkeypatch.setenv("MOCKLLM_ABSTENTION_RATE_OVERRIDE", "1.0")
    recs = run_records(input_source="regression")
    assert len(recs) == 10
    abstentions = sum(1 for r in recs if r["llm_abstained"])
    assert abstentions >= 8, f"expected >=8/10 abstentions, got {abstentions}"


def test_scenario10_abstention_disabled_reproduces_gap(monkeypatch):
    monkeypatch.setenv("MOCKLLM_ABSTENTION_RATE_OVERRIDE", "0.0")
    recs = run_records(input_source="regression")
    assert len(recs) == 10
    abstentions = sum(1 for r in recs if r["llm_abstained"])
    wrong = sum(1 for r in recs if r["actual_baseline_var"] is not None
                and r["expected_baseline_var"] is None)
    assert abstentions == 0
    assert wrong >= 1, "abstention disabled must reproduce the recall-gap (wrong mappings)"


# --------------------------------------------------------------------------- regression
def test_mutations_run_no_false_accept_with_abstention(monkeypatch):
    monkeypatch.setenv("MOCKLLM_ABSTENTION_RATE_OVERRIDE", "1.0")
    report = run(write=False, input_source="mutations")
    o = report["overall"]
    assert o["false_accept_rate_on_deterministic"] in (0.0, None)
    assert o["unmapped_precision"] == 1.0  # abstention only ADDS correct unmapped
    assert report["n_records"] == 160
