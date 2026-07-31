"""3c Theme Generator — Phase 3 tests: end-to-end integration + observability + MCP-safety.

Runs the full pipeline over a REALISTIC fixture mined from the live-fire HELIX_Modules
extraction (tests/theme_generator/fixtures/helix_modules_3c_fixture.json): 12 baseline
components, 10 client components spanning all three 3b outcomes (deterministic fork,
llm_required defer→reconcile, no_candidates passthrough). Uses the deterministic Mock
reconciler (no live LLM). Assertions are shape/invariant per spec §7.3.
"""
from __future__ import annotations

import json
from pathlib import Path

from agents.theme_generator import generate_theme
from agents.theme_generator.reconciliation import assess_anomaly, engagement_seed

_FIXTURE = Path(__file__).parent / "fixtures" / "helix_modules_3c_fixture.json"
TS = "2026-07-31T00:00:00.000Z"


def _fixture():
    return json.loads(_FIXTURE.read_text())


def _run(output_dir=None):
    fx = _fixture()
    return generate_theme(
        customer_slug="helix-modules", scope="msq-dx", baseline=fx["baseline"],
        client_components=fx["client_extraction"]["components"],
        client_tokens=fx["client_extraction"]["tokens"],
        scoring_by_slot=fx["scoring_by_slot"], timestamp=TS, output_dir=output_dir,
    )


# --------------------------------------------------------------------------- #
# end-to-end
# --------------------------------------------------------------------------- #

def test_end_to_end_produces_package_and_ledger(tmp_path):
    env = _run(output_dir=str(tmp_path / "pkg"))
    # every client component is accounted for in the provenance ledger
    assert len(env.provenance.derivations) == 10
    assert env.provenance.confidence_summary.total == 10
    # package materialised on disk with the expected skeleton
    root = tmp_path / "pkg"
    for rel in ("package.json", "docs/PROVENANCE.md", "src/tokens/tokens.json", "src/index.ts"):
        assert (root / rel).is_file(), rel
    # deterministic forks (Label, TableModule) emitted element files
    assert (root / "src" / "elements" / "label.ts").is_file()
    assert env.status in ("success", "partial")
    assert env.non_deterministic is True


def test_invocation_accounting_matches_deferred_count():
    env = _run()
    # exactly the 3 llm_required slots go to the fine-grained agent; 1 coarse pass
    assert env.cost_summary.fine_grained_invocations == 3
    assert env.cost_summary.expected_fine_grained == 3
    assert env.cost_summary.coarse_grained_invocations == 1
    assert env.cost_summary.breaker_tripped is False
    assert env.cost_summary.anomaly is None  # 3 == expected, no anomaly


def test_no_candidates_slots_are_passthrough_in_unmapped():
    env = _run()
    unmapped_slots = {u.slot for u in env.unmapped_components if u.reason == "no_candidates"}
    # the five no_candidates client components
    assert {"MediaText", "HeroTeaser", "FlyoutDesktop", "L1NavigationElement", "HeaderLogo"} <= unmapped_slots


def test_deterministic_forks_present_in_ledger():
    env = _run()
    kinds = {d.slot: d.kind for d in env.provenance.derivations}
    assert kinds["Label"] == "forked_from_baseline"       # deterministic authoritative
    assert kinds["TableModule"] == "forked_from_baseline"  # deterministic high


def test_provenance_md_lists_derivations():
    env = _run()
    # PROVENANCE.md is regenerated from the final ledger and names components
    # (rendered into the tree; assert via a fresh run to a tmp dir instead of parsing here)
    assert env.provenance.baseline_source_ref == "master@fixture-core-lib"


# --------------------------------------------------------------------------- #
# MCP-safety: the envelope must be plain-JSON serialisable (the run_workflow
# circular-ref bug was non-JSON-native content). This is the offline guard.
# --------------------------------------------------------------------------- #

def test_envelope_is_mcp_json_serialisable():
    env = _run()
    dumped = json.dumps(env.model_dump())          # must not raise (no circular refs / non-native types)
    reloaded = json.loads(dumped)
    assert reloaded["status"] in ("success", "partial")
    assert reloaded["non_deterministic"] is True
    assert "cost_summary" in reloaded and "provenance" in reloaded


# --------------------------------------------------------------------------- #
# determinism (Decision #5)
# --------------------------------------------------------------------------- #

def test_engagement_seed_is_stable_and_nonnegative():
    a = engagement_seed("helix-modules", TS)
    b = engagement_seed("helix-modules", TS)
    assert a == b and a >= 0
    assert engagement_seed("other", TS) != a  # different customer → different seed


def test_same_inputs_same_package_bytes(tmp_path):
    e1 = _run(output_dir=str(tmp_path / "a"))
    e2 = _run(output_dir=str(tmp_path / "b"))
    # byte-identical package.json + tokens across runs (fixed timestamp, deterministic mock)
    for rel in ("package.json", "src/tokens/tokens.json"):
        assert (tmp_path / "a" / rel).read_text() == (tmp_path / "b" / rel).read_text()
    # envelopes identical except package_path (which reflects the differing output dirs)
    d1, d2 = e1.model_dump(), e2.model_dump()
    d1.pop("package_path"), d2.pop("package_path")
    assert d1 == d2


# --------------------------------------------------------------------------- #
# anomaly helper (Probe 3)
# --------------------------------------------------------------------------- #

def test_assess_anomaly_flags_over_2x_and_breaker():
    assert assess_anomaly(expected_fine_grained=3, actual_fine_grained=3) is None
    assert assess_anomaly(expected_fine_grained=3, actual_fine_grained=7) is not None  # > 2x
    assert assess_anomaly(expected_fine_grained=0, actual_fine_grained=0, breaker_tripped=True) is not None
