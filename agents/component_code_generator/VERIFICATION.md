# 3d Component Code Generator v0.1 — Verification

Verification tasks mapped to concrete evidence. Status legend: **✅ closed** (offline test/code proves
it) · **◐ wired** (mechanism in place + unit-covered; final value needs the Phase-5 live run) ·
**⏸ pending** (a later phase / external input).

Test suites: `tests/component_code_generator/test_ccg_phase1.py` (P1), `_phase2.py` (P2),
`_phase3.py` (P3) — 35 tests, all green. Runnable gate: `python -m agents.component_code_generator.verify` (12/12).

| VT | Requirement | Status | Evidence |
|----|-------------|--------|----------|
| VT-1 | Path routing deterministic (Path A fork vs Path B from-spec) | ✅ | `route_element` (P1 `test_route_*`): forked/reconciled+baseline → fork; else deferred→Path B. |
| VT-2 | Path-A fork re-namespaces tag (hx- → customer) | ✅ | `fork_component` (P1 `test_fork_retags_*`); nested `<hx-icon>` rewritten too. |
| VT-3 | Path-A fork re-classes (PascalCase + Element) + re-tokenises (--helix- → --customer-) | ✅ | P1 `test_fork_renames_class_*`, `test_fork_retokenizes_*`; byte-deterministic. |
| VT-4 | Path-B from-spec generation (Mock/Real split, SP-20) | ✅ | `MockGenerator`/`AgentGenerator` (P2 `test_mock_generator_output_passes_gate`, `test_real_generator_*`). |
| VT-5 | Variant name-string typos normalised ("Activ"→Active) before render | ✅ | `parse_variant_axes`/`normalize_variant_value` (P2 `test_parse_variant_axes_*`; P3 output check). |
| VT-6 | No-baseline element generated-or-flagged, NEVER fabricated (SP-6) | ✅ | gate-fail-after-retry → `structural_gate_failed`, unresolved, not emitted (P2 `test_gate_failure_after_retry_flags_sp6_*`). |
| VT-7 | PROVENANCE.md generated for a HELIX_Modules-scale engagement | ◐ (shape) | `render_ccg_provenance_md` emitted end-to-end (P3 integration); a REAL helix-code + live-LLM run is **Phase 5**. |
| VT-8 | Cost-tracking hooks in place | ◐ | `CostSummary` (invocations, expected, anomaly, breaker) populated; token capture wired via shared `extract_usage` (P2 `test_real_generator_*` captures real tokens). Mock → 0; live value Phase 5. |
| VT-9 | package.json/dir layout + CEM + PROVENANCE emitted | ✅ | P1/P3 integration: `packages/{slug}-elements/src/elements/{el}/{Class}.ts` + `custom-elements.json` + `docs/PROVENANCE.md`. |
| VT-10 | Cross-run reproducibility within an engagement | ✅ (Mock) / ◐ (real LLM) | P3 `test_byte_identical_across_runs`; `engagement_seed` stable. Real-LLM bit-reproducibility is model-bounded (Anthropic no seed; temp=0). |
| VT-11 | `non_deterministic` True only when a real generator ran | ✅ | P2/P3: Mock → False; `_FakeRealGenerator` (name!=mock) → True. |
| VT-14 | Cost per engagement within budget (soft $5 / hard $12 / anomaly 2×) | ⏸ | Probe P4 baseline; mechanism (anomaly + breaker) in place (P2 `test_breaker_*`). Real cost validated at **Phase 5**. |
| VT-17 | Structural validation gate identifies pass/fail | ✅ | `run_structural_gate` (P2 `test_gate_passes_*`/`test_gate_fails_*`): catches --helix- leakage, wrong tag, unbalanced/missing-Lit. Applied to generated code only (forks skip, applied=False). |

## SP-22 shared observability
`agents/_shared/observability.py` (CircuitBreaker/engagement_seed/assess_anomaly/extract_usage) is
used by BOTH 3c and 3d; 3c re-exports for backward compat (3c suite 38/38 unchanged). ✅

## Runnable gate
```
python -m agents.component_code_generator.verify
```
Runs the full pipeline over a self-contained fixture (Path-A fork + Path-B from-spec) with the
deterministic Mock — no helix-code checkout, no LLM — and prints PASS/FAIL per offline-closable VT;
exit 0 = green. Kept green by `tests/component_code_generator/test_ccg_verify.py`.

## Summary
- **Closed offline:** VT-1,2,3,4,5,6,9,11,17 + the deterministic surface of VT-7/VT-10.
- **Wired, value pending Phase-5 live run:** VT-8 (real token $), VT-14 (real cost vs budget), and the
  real-LLM/real-helix-code aspects of VT-7/VT-10.
- **Phase 5 (hardening live-run on HELIX_Modules)** closes the live VTs and — per the 3c precedent —
  is where real-path bugs surface + get localized (SP-26).

No fabrication: every ◐/⏸ item states *why* and *when* it closes.
