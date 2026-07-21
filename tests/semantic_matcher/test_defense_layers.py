"""Contract + behavioural tests for defense layers 2 and 3 (spec v3.2).

Layer 2 — name-vs-declared-type coherence (Surface A): a token whose NAME (and
intact PATH) contradicts its declared TYPE routes to unmapped
'name_type_incoherent' BEFORE the LLM, on both the deterministic and LLM paths.

Layer 3 — enriched baseline value/type check (Surface B): the post-validator
resolves a chosen baseline token's DTCG alias value and checks the RESOLVED value
against its declared type (closing the alias-opacity bypass); an unresolved alias
falls back to skipping the check.
"""
from __future__ import annotations

from tests.semantic_matcher.testbench.input_sources._common import load_baseline
from tests.semantic_matcher.testbench.llm_path import MockLLM
from tests.semantic_matcher.testbench.run_testbench import (
    build_baseline_enrichment,
    check_name_type_coherence,
    compute_metrics,
    post_validate,
    run_case,
    run_records,
)


def _color(hex_str: str) -> dict:
    r, g, b = (int(hex_str[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return {"colorSpace": "srgb", "components": [r, g, b], "alpha": 1, "hex": hex_str.upper()}


def _tok(name, path, category, layer, dtcg_type, value) -> dict:
    return {"name": name, "path": path, "category": category,
            "layer": layer, "dtcg_type": dtcg_type, "value": value}


def _harness_ctx():
    base = load_baseline()
    bbn = {r["name"]: r for r in base}
    return base, bbn, set(bbn), build_baseline_enrichment(base)


# =========================================================================== #
# Layer 2 — name/type coherence (Surface A)
# =========================================================================== #
def test_coherence_flags_name_type_contradiction():
    # name+path say color, declared type number -> incoherent
    c = _tok("--helix-color-brand1-100", "color/brand1/100", "dimension", "primitive",
             "number", 12)
    v = check_name_type_coherence(c)
    assert v is not None and v["verdict"] == "incoherent"
    assert v["name_category_family"] == "color" and v["declared_family"] == "numeric"


def test_coherence_soft_passes_coherent_token():
    c = _tok("--helix-color-brand1-100", "color/brand1/100", "color", "primitive",
             "color", _color("#D0EBFF"))
    v = check_name_type_coherence(c)
    assert v is not None and v["verdict"] == "coherent"


def test_coherence_soft_passes_name_collision_shape():
    # name_collision mislabels the NAME (says color) but keeps the true numeric PATH.
    # Name is NOT corroborated by the path -> soft-pass (no false flag).
    c = _tok("--helix-color-token-15", "component_dimensions/radius/sm", "component_dimensions",
             "primitive", "number", 8)
    assert check_name_type_coherence(c) is None


def test_coherence_soft_passes_unknown_declared_type():
    # synthesized typography (family 'other') -> cannot judge -> soft-pass
    c = _tok("--client-typography-00", "font/family/body", "typography", "semantic",
             "typography", {"fontFamily": "dm sans", "fontWeight": 700})
    assert check_name_type_coherence(c) is None


def test_layer2_routes_incoherent_to_unmapped_both_paths(monkeypatch):
    """Surface A contract: an incoherent token routes to 'name_type_incoherent' on
    BOTH paths — independent of the LLM (asserted under abstention ON and OFF) and
    never deterministic-accepted nor LLM-invoked (Layer 2 short-circuits first)."""
    base, bbn, names, enr = _harness_ctx()
    incoherent_tokens = [
        # color-named/pathed, declared number
        _tok("--helix-color-brand1-100", "color/brand1/100", "dimension", "primitive",
             "number", 5),
        # number-named/pathed (dimension), declared color
        _tok("--helix-dimension-layout-breakpoint", "dimension/layout/breakpoint", "color",
             "primitive", "color", _color("#123456")),
    ]
    for rate in ("1.0", "0.0"):  # LLM abstention on vs off -> must not matter
        monkeypatch.setenv("MOCKLLM_ABSTENTION_RATE_OVERRIDE", rate)
        for client in incoherent_tokens:
            case = {"mutation_class": "probe", "client_token": client,
                    "expected_baseline_var": None, "note": ""}
            rec = run_case(case, base, bbn, names, MockLLM(), None, enr)
            assert rec["unmapped_reason"] == "name_type_incoherent", (rate, client["name"])
            assert rec["actual_baseline_var"] is None
            assert rec["llm_invoked"] is False           # never reached the LLM
            assert rec["deterministic_accepted"] is False
            assert rec["coherence_check"]["verdict"] == "incoherent"


def test_layer2_metric_and_reason_wiring():
    recs = run_records(input_source="mutations")
    m = compute_metrics(recs)
    # cross_category (10 by construction) is caught by Layer 2 in the canonical run
    assert m["name_type_incoherent_count"] >= 1
    assert m["name_type_incoherent_rate"] is not None
    assert m["unmapped_by_reason"].get("name_type_incoherent") == m["name_type_incoherent_count"]
    # no honest class is false-flagged: only cross_category is incoherent here
    flagged = {r["mutation_class"] for r in recs
               if r["unmapped_reason"] == "name_type_incoherent"}
    assert flagged == {"cross_category"}, flagged


# =========================================================================== #
# Layer 3 — enriched baseline value/type check (Surface B)
# =========================================================================== #
def test_enrichment_resolves_alias_baseline_value():
    base = load_baseline()
    enr = build_baseline_enrichment(base)
    aliases = [r for r in base if isinstance(r["value"], str) and r["value"].startswith("{")]
    assert aliases, "fixture expected to contain raw DTCG alias values"
    for r in aliases[:20]:
        ev = enr[r["name"]]
        # resolved to a concrete (non-alias) value, flagged as resolvable
        assert ev["unresolved_alias"] is False
        assert not (isinstance(ev["value"], str) and str(ev["value"]).startswith("{"))


def test_postvalidator_uses_resolved_value_for_alias_baseline():
    """Surface B contract: the post-validator inspects the RESOLVED value of an
    alias-valued baseline token (not the opaque '{...}' string)."""
    base = load_baseline()
    enr = build_baseline_enrichment(base)
    # a color baseline token whose raw value is an alias, resolving to a color dict
    alias_color = next(r for r in base
                       if r["dtcg_type"] == "color"
                       and isinstance(r["value"], str) and r["value"].startswith("{"))
    chosen = {"name": alias_color["name"], "dtcg_type": "color"}
    client = _tok("--client-x", "color/x", "color", None, "color", _color("#FFFFFF"))

    pv = post_validate(client, chosen, "rationale", set(enr), enr)
    # resolved value is a color dict consistent with declared color -> passes, and the
    # check was actually EVALUATED (not skipped) because the alias resolved.
    assert pv["checks"]["baseline_value_type_consistent"] is True

    # Now prove the resolved value is genuinely inspected: a doctored enrichment where
    # the resolved value CONTRADICTS the declared type must fail the check.
    bad_enr = dict(enr)
    bad_enr[alias_color["name"]] = {"dtcg_type": "number", "value": _color("#010203"),
                                    "unresolved_alias": False}
    chosen_num = {"name": alias_color["name"], "dtcg_type": "number"}
    pv_bad = post_validate(client, chosen_num, "rationale", set(bad_enr), bad_enr)
    assert pv_bad["checks"]["baseline_value_type_consistent"] is False
    assert pv_bad["passed"] is False


def test_postvalidator_skips_unresolved_alias():
    """An unresolved alias is never trusted: the baseline value/type check is skipped
    (fallback), so it does not spuriously reject the mapping."""
    enr = {"--helix-colors-orphan": {"dtcg_type": "color", "value": None,
                                     "unresolved_alias": True}}
    chosen = {"name": "--helix-colors-orphan", "dtcg_type": "color"}
    client = _tok("--client-x", "color/x", "color", None, "color", _color("#FFFFFF"))
    pv = post_validate(client, chosen, "rationale", {"--helix-colors-orphan"}, enr)
    assert pv["checks"]["baseline_value_type_consistent"] == "skipped"
    assert pv["passed"] is True
