"""Unit tests for the 3b deterministic scoring module.

Covers the cases named in the task VERIFICATION CRITERIA: exact match, complete
disagreement, single-signal-only, missing value (non-scalar), value-veto.
"""
from __future__ import annotations

from agents.semantic_matcher.scoring import (
    DEFAULT_THRESHOLDS,
    name_similarity,
    path_overlap,
    score_candidates,
    value_distance,
)


def _color(hex_str: str) -> dict:
    r = int(hex_str[1:3], 16) / 255
    g = int(hex_str[3:5], 16) / 255
    b = int(hex_str[5:7], 16) / 255
    return {"colorSpace": "srgb", "components": [r, g, b], "alpha": 1, "hex": hex_str.upper()}


def _tok(name, path, category, layer, dtcg_type, value) -> dict:
    return {"name": name, "path": path, "category": category,
            "layer": layer, "dtcg_type": dtcg_type, "value": value}


# --------------------------------------------------------------------------- primitives
def test_name_similarity_bounds():
    assert name_similarity("--helix-color-brand", "--helix-color-brand") == 1.0
    assert name_similarity("", "") == 1.0
    assert 0.0 <= name_similarity("abc", "xyz") < 1.0


def test_path_overlap_jaccard():
    assert path_overlap("color/brand/primary", "color/brand/primary") == 1.0
    assert path_overlap("color/brand", "color/error") == 1.0 / 3.0  # {color,brand}∩{color,error}
    assert path_overlap(None, "a/b") == 0.0


def test_value_distance_by_type():
    assert value_distance("color", _color("#000000"), _color("#000000")) == 0.0
    assert value_distance("color", _color("#000000"), _color("#FFFFFF")) == 1.0
    assert value_distance("number", 100, 100) == 0.0
    assert abs(value_distance("number", 100, 150) - 0.3333333) < 1e-3
    assert value_distance("string", "DM Sans", "dm sans") == 0.0
    assert value_distance("string", "dm sans", "roboto") == 1.0
    # non-scalar / unavailable
    assert value_distance("typography", {"x": 1}, {"y": 2}) is None


# --------------------------------------------------------------------------- exact match
def test_exact_match_is_deterministic_authoritative():
    cand = _tok("--helix-color-brand-primary", "color/brand/primary", "color",
                "semantic", "color", _color("#3388F0"))
    client = dict(cand)  # identical
    distractor = _tok("--helix-color-error-bg", "color/error/bg", "color",
                      "semantic", "color", _color("#FFE0E0"))
    res = score_candidates(client, [cand, distractor])
    assert res.matched_via == "deterministic"
    assert res.top_candidates[0].baseline_var == "--helix-color-brand-primary"
    assert res.deterministic_confidence in ("authoritative", "high")
    assert res.top_candidates[0].signals.value_distance == 0.0


# --------------------------------------------------------------------------- complete disagreement
def test_complete_disagreement_escalates():
    client = _tok("--totally-different-xyz", "misc/unknown", "misc", "primitive",
                  "color", _color("#123456"))
    cand = _tok("--helix-color-brand-primary", "color/brand/primary", "color",
                "semantic", "color", _color("#3388F0"))
    res = score_candidates(client, [cand])
    assert res.matched_via == "llm_required"


# --------------------------------------------------------------------------- single-signal-only
def test_single_signal_only_does_not_deterministic_accept():
    # name matches strongly but path/layer absent and value far -> < MIN_FAMILIES_AGREE effective accept
    client = _tok("--helix-color-brand-primary", None, "color", None, "color", _color("#FFFFFF"))
    cand = _tok("--helix-color-brand-primary", "color/brand/primary", "color",
                "semantic", "color", _color("#000000"))
    res = score_candidates(client, [cand])
    # value is maximally far -> veto fires -> cannot be deterministic
    assert res.matched_via == "llm_required"


# --------------------------------------------------------------------------- missing value (non-scalar)
def test_missing_value_non_scalar_no_crash():
    client = _tok("--helix-typography-body", "typography/body", "typography", "semantic",
                  "typography", {"fontFamily": "dm sans", "fontWeight": 400})
    cand = _tok("--helix-typography-body", "typography/body", "typography", "semantic",
                "typography", {"fontFamily": "dm sans", "fontWeight": 700})
    res = score_candidates(client, [cand])
    # value_distance is None (non-scalar); must not crash; value can't satisfy the veto
    assert res.top_candidates[0].signals.value_distance is None
    assert res.matched_via in ("deterministic", "llm_required")
    # with no scalar value, value_ok is False -> escalates
    assert res.matched_via == "llm_required"


# --------------------------------------------------------------------------- value-veto (FM-3b-5)
def test_value_veto_is_hard_even_with_name_and_path_agreement():
    # name + path + layer all identical; only the color is off by > veto threshold
    base_path, base_name = "color/brand/primary", "--helix-color-brand-primary"
    client = _tok(base_name, base_path, "color", "semantic", "color", _color("#3388F0"))
    cand = _tok(base_name, base_path, "color", "semantic", "color", _color("#FF0000"))
    distractor = _tok("--helix-color-error-bg", "color/error/bg", "color", "semantic",
                      "color", _color("#FFE0E0"))
    res = score_candidates(client, [cand, distractor])
    assert res.matched_via == "llm_required", "hard value-veto must block name+path agreement"
    assert "value-veto=FIRED" in (res.deterministic_rationale or "")


def test_value_veto_passes_within_tolerance():
    # identical name/path/layer, color within 8-hex-unit tolerance -> deterministic
    client = _tok("--helix-color-brand-primary", "color/brand/primary", "color",
                  "semantic", "color", _color("#3388F0"))
    cand = _tok("--helix-color-brand-primary", "color/brand/primary", "color",
                "semantic", "color", _color("#3488F0"))  # +1 on R channel
    distractor = _tok("--helix-color-error-bg", "color/error/bg", "color", "semantic",
                      "color", _color("#FFE0E0"))
    res = score_candidates(client, [cand, distractor])
    assert res.matched_via == "deterministic"


# --------------------------------------------------------------------------- no candidates
def test_no_candidates():
    client = _tok("--helix-color-brand-primary", "color/brand/primary", "color",
                  "semantic", "color", _color("#3388F0"))
    res = score_candidates(client, [])
    assert res.matched_via == "no_candidates"
    assert res.top_candidates == []


# --------------------------------------------------------------------------- thresholds override
def test_threshold_override_changes_verdict():
    client = _tok("--helix-color-brand-primary", "color/brand/primary", "color",
                  "semantic", "color", _color("#3388F0"))
    cand = _tok("--helix-color-brand-primary", "color/brand/primary", "color",
                "semantic", "color", _color("#3388F0"))
    strict = dict(DEFAULT_THRESHOLDS)
    strict["MIN_FAMILIES_AGREE"] = 99  # impossible
    res = score_candidates(client, [cand], thresholds=strict)
    assert res.matched_via == "llm_required"
