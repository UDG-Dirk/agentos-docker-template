"""3c Theme Generator — Phase 1 (deterministic) tests.

Fixture-based, EXACT assertions (spec §7.3 — deterministic components use exact
assertions, not shape/invariant; those are for Phase-2 agent components). Uses the
REAL 3b ``ScoringResult`` contract to prove the §5.3 boundary is wired against the
actual enum (matched_via ∈ deterministic|llm_required|no_candidates;
deterministic_confidence ∈ authoritative|high|None), not the spec's §3.2 sketch.
"""
from __future__ import annotations

import json

from agents.semantic_matcher.scoring import ScoredCandidate, ScoringResult, ScoringSignals
from agents.theme_generator import (
    ThemeGeneratorOutput,
    build_deterministic_package,
    classify_component,
    generate_theme_phase1,
    substitute_tokens,
    write_package,
)
from agents.theme_generator.models import ProvenanceSummary

TS = "2026-07-31T00:00:00.000Z"  # fixed timestamp → byte-deterministic output


def _scoring(matched_via, conf=None, baseline_var="baseline.Button"):
    return ScoringResult(
        top_candidates=[ScoredCandidate(
            baseline_var=baseline_var,
            signals=ScoringSignals(name_similarity=0.9, path_overlap=0.5,
                                   value_distance=0.0, layer_alignment=True),
            aggregate_score=0.9, signal_families_agreeing=3,
        )],
        margin_top1_top2=0.4, matched_via=matched_via, deterministic_confidence=conf,
    )


def _baseline():
    return {
        "meta": {"baseline_ref": "master@abc123", "cem_size_bytes": 4096},
        "tokens": [{"name": "--color-primary", "value": "#000000"},
                   {"name": "--space-sm", "value": "4px"}],
        "components": [{"name": "Button"}, {"name": "Input"}],
    }


def _client_components():
    return [{"name": "Button"}, {"name": "Input"}, {"name": "BrandHero"}]


# --------------------------------------------------------------------------- #
# classify_component — the §5.3 boundary against the real enum
# --------------------------------------------------------------------------- #

def test_classify_deterministic_high_forks():
    assert classify_component(_scoring("deterministic", "high")) == ("fork", "high")


def test_classify_deterministic_authoritative_forks():
    assert classify_component(_scoring("deterministic", "authoritative")) == ("fork", "authoritative")


def test_classify_no_candidates_passthrough():
    assert classify_component(_scoring("no_candidates", None)) == ("passthrough", "high")


def test_classify_llm_required_defers():
    assert classify_component(_scoring("llm_required", None)) == ("defer", "unresolved")


def test_classify_deterministic_without_confidence_defers():
    # matched_via deterministic but no confidence → NOT high → defer (fail-safe)
    assert classify_component(_scoring("deterministic", None)) == ("defer", "unresolved")


def test_classify_dict_form_matches_object_form():
    # a ScoringResult arriving as a JSON dict across a step boundary behaves identically
    d = {"matched_via": "deterministic", "deterministic_confidence": "high",
         "top_candidates": [{"baseline_var": "baseline.Button"}]}
    assert classify_component(d) == ("fork", "high")


# --------------------------------------------------------------------------- #
# substitute_tokens — deterministic overlay by name
# --------------------------------------------------------------------------- #

def test_substitute_overrides_by_name_case_and_dash_insensitive():
    baseline = [{"name": "--color-primary", "value": "#000000"},
                {"name": "--space-sm", "value": "4px"}]
    client = [{"name": "color-primary", "value": "#FF0000"}]  # no dashes, still matches
    out_json, subs = substitute_tokens(baseline, client)
    out = json.loads(out_json)
    assert subs == 1
    assert out["--color-primary"] == "#FF0000"   # overridden
    assert out["--space-sm"] == "4px"            # baseline default kept


def test_substitute_is_byte_deterministic():
    baseline = [{"name": "--b", "value": "2"}, {"name": "--a", "value": "1"}]
    a, _ = substitute_tokens(baseline, [])
    b, _ = substitute_tokens(baseline, [])
    assert a == b  # sorted keys → stable


# --------------------------------------------------------------------------- #
# build_deterministic_package / generate_theme_phase1
# --------------------------------------------------------------------------- #

def test_phase1_mixed_run_is_partial_with_correct_ledger():
    scoring = {
        "Button": _scoring("deterministic", "high", "baseline.Button"),  # fork
        "Input": _scoring("no_candidates", None),                        # passthrough
        "BrandHero": _scoring("llm_required", None),                     # defer → partial
    }
    env = generate_theme_phase1(
        customer_slug="Acme Corp", scope="msq-dx", baseline=_baseline(),
        client_components=_client_components(), client_tokens=[{"name": "--color-primary", "value": "#FF0000"}],
        scoring_by_slot=scoring, timestamp=TS,
    )
    assert env.status == "partial"                 # a deferred component exists
    assert env.non_deterministic is True           # always, per Decision #5
    # confidence distribution: 1 high (fork) + 1 high (passthrough) + 1 unresolved (defer)
    cs = env.provenance.confidence_summary
    assert (cs.high, cs.unresolved, cs.authoritative, cs.medium) == (2, 1, 0, 0)
    kinds = {d.slot: d.kind for d in env.provenance.derivations}
    assert kinds == {"Button": "forked_from_baseline", "Input": "customer_passthrough",
                     "BrandHero": "agent_flagged_review"}
    # unmapped register: passthrough + deferred (not the forked one)
    reasons = {u.slot: u.reason for u in env.unmapped_components}
    assert reasons == {"Input": "no_candidates", "BrandHero": "llm_abstained"}
    assert env.cost_summary.fine_grained_invocations == 0  # Phase 1 = deterministic


def test_phase1_all_deterministic_is_success():
    scoring = {"Button": _scoring("deterministic", "high"),
               "Input": _scoring("deterministic", "authoritative"),
               "BrandHero": _scoring("no_candidates", None)}
    env = generate_theme_phase1(
        customer_slug="acme", scope="msq-dx", baseline=_baseline(),
        client_components=_client_components(), client_tokens=[], scoring_by_slot=scoring, timestamp=TS,
    )
    assert env.status == "success"  # no deferrals


def test_phase1_package_tree_shape_and_content():
    tree, prov, unmapped, warnings, status = build_deterministic_package(
        customer_slug="Acme Corp", scope="msq-dx", baseline=_baseline(),
        client_components=[{"name": "Button"}],
        scoring_by_slot={"Button": _scoring("deterministic", "high", "baseline.Button")},
        baseline_tokens=_baseline()["tokens"], client_tokens=[],
        provenance=ProvenanceSummary(baseline_source_ref="master@abc123",
                                     baseline_cem_size_bytes=4096, generation_timestamp=TS),
    )
    assert status == "success" and not warnings
    assert set(tree) == {"package.json", "src/tokens/tokens.json", "src/index.ts",
                         "docs/PROVENANCE.md", "src/elements/button.ts"}
    pkg = json.loads(tree["package.json"])
    assert pkg["name"] == "@msq-dx/acme-corp-elements"
    assert pkg["provenance"]["baseline_source_ref"] == "master@abc123"
    assert "baseline.Button" in tree["src/elements/button.ts"]
    assert "master@abc123" in tree["docs/PROVENANCE.md"]


def test_phase1_empty_client_is_failure_with_blocking_warning():
    env = generate_theme_phase1(
        customer_slug="acme", scope="msq-dx", baseline=_baseline(),
        client_components=[], client_tokens=[], scoring_by_slot={}, timestamp=TS,
    )
    assert env.status == "failure"
    assert env.package_path is None
    codes = {w.code for w in env.blocking_warnings}
    assert "empty_client_extraction" in codes


def test_phase1_missing_baseline_is_failure():
    env = generate_theme_phase1(
        customer_slug="acme", scope="msq-dx", baseline={},
        client_components=[{"name": "Button"}], client_tokens=[], scoring_by_slot={}, timestamp=TS,
    )
    assert env.status == "failure"
    assert "baseline_unavailable" in {w.code for w in env.blocking_warnings}


def test_write_package_materialises_tree(tmp_path):
    tree, *_ = build_deterministic_package(
        customer_slug="acme", scope="msq-dx", baseline=_baseline(),
        client_components=[{"name": "Button"}],
        scoring_by_slot={"Button": _scoring("deterministic", "high")},
        baseline_tokens=_baseline()["tokens"], client_tokens=[],
        provenance=ProvenanceSummary(generation_timestamp=TS),
    )
    dest = write_package(tree, tmp_path / "pkg")
    assert (tmp_path / "pkg" / "package.json").is_file()
    assert (tmp_path / "pkg" / "src" / "elements" / "button.ts").is_file()
    assert dest == str(tmp_path / "pkg")


def test_envelope_model_dump_round_trips():
    env = generate_theme_phase1(
        customer_slug="acme", scope="msq-dx", baseline=_baseline(),
        client_components=[{"name": "Button"}],
        scoring_by_slot={"Button": _scoring("deterministic", "high")},
        client_tokens=[], timestamp=TS,
    )
    d = env.model_dump()
    assert ThemeGeneratorOutput(**d).status == env.status
    assert d["non_deterministic"] is True
