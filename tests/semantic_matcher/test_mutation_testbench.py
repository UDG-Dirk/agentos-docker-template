"""Smoke + invariant tests for the mutation testbench harness.

Verifies the task's VERIFICATION CRITERIA: all 6 classes produce results, the
harness runs end-to-end on the mock path without exceptions, the report is
well-formed, and the ground-truth-by-construction invariants hold.
"""
from __future__ import annotations

import time

import pytest

from tests.semantic_matcher.testbench.input_sources._common import (
    load_baseline,
    prefilter_candidates,
)
from tests.semantic_matcher.testbench.run_testbench import (
    MUTATION_CLASSES,
    collect_cases,
    run,
)

EXPECTED_COUNTS = {
    "name_only": 30, "value_drift": 30, "provenance_stripped": 30,
    "name_collision": 40, "cross_category": 10, "composite": 20,
}


def test_all_six_classes_present_with_expected_counts():
    cases = collect_cases()
    got: dict[str, int] = {}
    for c in cases:
        got[c["mutation_class"]] = got.get(c["mutation_class"], 0) + 1
    assert set(got) == set(MUTATION_CLASSES)
    assert got == EXPECTED_COUNTS, got
    assert len(cases) == sum(EXPECTED_COUNTS.values()) == 160


def test_baseline_fixture_loads():
    base = load_baseline()
    assert len(base) == 510
    for r in base[:5]:
        assert {"name", "path", "category", "layer", "dtcg_type", "value"} <= set(r)


def test_case_ground_truth_shape():
    for c in collect_cases():
        assert c["client_token"].get("name")
        ev = c["expected_baseline_var"]
        assert ev is None or isinstance(ev, str)
        # cross_category + composite are unmapped by construction
        if c["mutation_class"] in ("cross_category", "composite"):
            assert ev is None


def test_harness_runs_end_to_end_under_60s_mock():
    t0 = time.time()
    report = run(write=False)
    dt = time.time() - t0
    assert dt < 60, f"mock run took {dt:.1f}s (>60s)"
    assert report["n_records"] == 160
    o = report["overall"]
    # safety-critical: no wrong deterministic accepts, perfect unmapped precision
    assert o["false_accept_rate_on_deterministic"] in (0.0, None)
    assert o["unmapped_precision"] == 1.0
    for key in ("deterministic_accept_rate", "correct_mapping_rate",
                "llm_invocation_rate", "unmapped_recall"):
        assert report["overall"][key] is not None


def test_report_is_wellformed():
    report = run(write=False)
    assert set(report["per_class"]) == set(MUTATION_CLASSES)
    # 4x4 signal correlation matrix present
    mtx = report["signal_correlation_matrix"]
    fams = ["name_similarity", "path_overlap", "value_distance", "layer_alignment"]
    assert set(mtx) == set(fams)
    for a in fams:
        assert set(mtx[a]) == set(fams)
    # margin histogram non-empty
    assert report["margin_histogram"]


def test_prefilter_nonempty_for_real_categories():
    base = load_baseline()
    sample = {"name": "--helix-color-x", "path": "color/x", "category": "color",
              "layer": "semantic", "dtcg_type": "color", "value": {"components": [0, 0, 0], "hex": "#000000"}}
    assert len(prefilter_candidates(sample, base)) > 0
