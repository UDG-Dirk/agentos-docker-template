"""Executable verification gate for the 3c Theme Generator (Phase 4, riff).

Turns the VERIFICATION.md matrix into a ONE-COMMAND, offline PASS/FAIL check:

    python -m agents.theme_generator.verify

Runs the full pipeline (deterministic Mock agents — no live LLM, no cost) over a small
self-contained fixture that exercises all three 3b outcomes, then asserts the invariants
each offline-closable VT depends on. Exit 0 = all green. Complements the pytest suite:
this is the colleague-runnable gate, the pattern the repo already uses for `python -m evals`.

It deliberately does NOT reach into tests/ — the fixture is inline so the gate ships with
the package and runs anywhere the package imports.
"""
from __future__ import annotations

import json

from agents.semantic_matcher.scoring import ScoredCandidate, ScoringResult, ScoringSignals
from agents.theme_generator import generate_theme

_TS = "2026-07-31T00:00:00.000Z"


def _scoring(matched_via, conf=None, baseline_var="Button"):
    return ScoringResult(
        top_candidates=([ScoredCandidate(
            baseline_var=baseline_var,
            signals=ScoringSignals(name_similarity=0.9, path_overlap=0.5,
                                   value_distance=0.0, layer_alignment=True),
            aggregate_score=0.9, signal_families_agreeing=3)] if baseline_var else []),
        margin_top1_top2=0.4, matched_via=matched_via, deterministic_confidence=conf,
    )


def _run():
    baseline = {
        "meta": {"baseline_ref": "master@verify", "cem_size_bytes": 100},
        "tokens": [{"name": "--color-primary", "value": "#000"}],
        "components": [{"name": "Button"}, {"name": "Card"}],
    }
    client_components = [{"name": "Button"}, {"name": "Zzqptar"}, {"name": "Standalone"}]
    scoring = {
        "Button": _scoring("deterministic", "high", "Button"),   # → deterministic fork
        "Zzqptar": _scoring("llm_required", None, "Button"),      # → defer → mock flags (no name match)
        "Standalone": _scoring("no_candidates", None, None),      # → passthrough
    }
    return generate_theme(
        customer_slug="verify-co", scope="msq-dx", baseline=baseline,
        client_components=client_components, client_tokens=[], scoring_by_slot=scoring,
        timestamp=_TS, output_dir=None,
    )


def build_report() -> list[tuple[str, str, bool]]:
    """Return [(vt, description, passed), ...] for the offline-closable verification tasks."""
    env = _run()
    env2 = _run()  # for determinism
    kinds = {d.slot: d.kind for d in env.provenance.derivations}
    warn_codes = {w.code for w in env.blocking_warnings}

    checks: list[tuple[str, str, bool]] = []

    def chk(vt, desc, ok):
        checks.append((vt, desc, bool(ok)))

    chk("VT-1/11", "envelope always non_deterministic:true", env.non_deterministic is True)
    chk("VT-2", "consumes real 3b ScoringResult shape", len(env.provenance.derivations) == 3)
    chk("VT-3/9", "deterministic Button forked from baseline", kinds.get("Button") == "forked_from_baseline")
    chk("VT-5", "unresolved slot flagged (not fabricated) → blocking_warning",
        kinds.get("Zzqptar") == "agent_flagged_review" and "agent_abstained" in warn_codes)
    chk("VT-5b", "no_candidates slot → passthrough (kept, not invented)",
        kinds.get("Standalone") == "customer_passthrough")
    chk("VT-8", "cost_summary carries token + invocation fields",
        isinstance(env.cost_summary.total_input_tokens, int)
        and env.cost_summary.fine_grained_invocations == 1
        and env.cost_summary.coarse_grained_invocations == 1)
    chk("VT-8b", "anomaly clean at expected invocation count", env.cost_summary.anomaly is None)
    chk("VT-12", "confidence_summary total == component count",
        env.provenance.confidence_summary.total == 3)
    chk("VT-10", "byte-identical envelope across runs (determinism)", env.model_dump() == env2.model_dump())

    mcp_ok = True
    try:
        json.dumps(env.model_dump())
    except (TypeError, ValueError):
        mcp_ok = False
    chk("MCP-safe", "envelope is plain-JSON serialisable (no circular refs)", mcp_ok)

    return checks


def main() -> int:
    report = build_report()
    print("3c Theme Generator — verification gate (offline, Mock agents)\n")
    width = max(len(vt) for vt, _, _ in report)
    for vt, desc, ok in report:
        print(f"  [{'PASS' if ok else 'FAIL'}] {vt.ljust(width)}  {desc}")
    passed = sum(1 for _, _, ok in report if ok)
    total = len(report)
    print(f"\n{passed}/{total} checks passed.")
    print("(Live-run VTs — 7/8-token-$/10-realLLM, 13 Sascha, 14 thresholds — are hardening; see VERIFICATION.md.)")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
