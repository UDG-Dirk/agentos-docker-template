# 3c Theme Generator v0.1 — Verification (VT-1 … VT-15)

Verification tasks from the v0.1 spec §10, mapped to concrete evidence in this repo.
Status legend: **✅ covered** (offline test/code proves it) · **◐ mechanism-in-place**
(implemented + unit-covered; final value needs a live run) · **⏳ deferred** (external input
or hardening).

Test suites: `tests/theme_generator/test_theme_generator_phase1.py` (P1),
`_phase2.py` (P2), `_phase3.py` (P3) — 35 tests, all green.

| VT | Requirement | Status | Evidence |
|----|-------------|--------|----------|
| VT-1 | All ratified decisions honored (D#1–5) | ✅ | D#1 full-fork package + D#2 helix-code layout: `scaffolding.build_deterministic_package` / `render_package_json` (P1 `test_phase1_package_tree_shape_and_content`). D#3 hybrid: deterministic build + `generate_theme` agentic reconcile + coarse cohesion (P2/P3). D#4: `_route_reconciliation` (P2 `test_route_*`). D#5: `non_deterministic` always true + temp0/seed (P3 `test_engagement_seed_*`). |
| VT-2 | Input contracts (3a / 3b / extractor) consumed correctly | ✅ | `classify_component` against the real `ScoringResult` enum (P1 `test_classify_*`, `test_classify_dict_form_matches_object_form`); `gather_inputs` consumes `baseline` + `client_extraction`; P3 integration uses real HELIX_Modules-shaped fixture. |
| VT-3 | Deterministic path unit-tested, exact assertions | ✅ | P1 (15 exact-assertion tests). |
| VT-4 | Agentic path shape/invariant tested (SP-17) | ✅ | P2 (11 shape/invariant + oracle tests). |
| VT-5 | SP-6 extension: below-threshold → blocking_warning, no fabrication | ✅ | `_route_reconciliation` unresolved→no-emit+warning (P2 `test_route_unresolved_does_not_emit`, `test_deferred_slot_unresolved_stays_partial`). |
| VT-6 | SP-9 preserved: no hardcoded config; env-driven | ✅ | Model id from `OPENAI_MODEL_ID` via `default_chat_model`; package scope is a caller arg (`customer_package_name(scope)`); breaker caps env-overridable (`_env_int`). Explicit test: P3 `test_sp9_no_hardcoded_model_or_scope`. |
| VT-7 | PROVENANCE.md generated for a HELIX_Modules-scale engagement | ✅ (offline) / ◐ (live) | P3 `test_end_to_end_produces_package_and_ledger` materialises `docs/PROVENANCE.md` on the HELIX_Modules fixture; a live-figma run is hardening. |
| VT-8 | Cost-tracking hooks in place | ◐ | `CostSummary` in envelope (invocations, breaker, anomaly, expected) populated by `generate_theme` (P2/P3). Token $ totals (`total_input/output_tokens`) stay 0 until a real agent run wires usage metrics — live/hardening. |
| VT-9 | package.json + directory layout matches baseline | ✅ | P1 `test_phase1_package_tree_shape_and_content` (name `@scope/slug-elements`, provenance, `src/elements`, `src/tokens`, `docs`, `src/index.ts`). Full FE-DEV parity = VT-13. |
| VT-10 | Cross-run reproducibility within an engagement | ✅ (deterministic surface) / ◐ (real LLM) | P3 `test_same_inputs_same_package_bytes` (byte-identical package + envelope); `engagement_seed` stable. Real-LLM temp0+seed reproducibility is live/hardening. |
| VT-11 | `non_deterministic` always true in envelope | ✅ | Asserted across P1/P2/P3. |
| VT-12 | agent_confidence_summary matches invocation distribution | ✅ | `_confidence_summary` rebuilt from the ledger; P2 asserts confidence_summary after reconciliation. |
| VT-13 | [PENDING SASCHA] layout aligns with FE-DEV conventions | ⏳ | External input; spec §11.2 v0.2. Uses exact-helix-code-match default until then. |
| VT-14 | [PENDING PROBE 3] cost per engagement within budget | ◐ | Probe 3 landed (`shared-results:3c-llm-cost-baseline-probe-result-2026-07-31`); anomaly + hard breaker mechanism in place. Formal $ thresholds ratified at hardening (v0.2). |
| VT-15 | matcher.py deferred — 3c imports 3b library directly | ✅ | `reconciliation.py` imports `agents.semantic_matcher.scoring.name_similarity` directly; no `matcher.py` dependency anywhere. |

## Summary
- **Closed offline: VT-1,2,3,4,5,6,9,11,12,15** and the deterministic surface of VT-7/VT-10.
- **Mechanism in place, final value needs a live run: VT-8 (token $), VT-14 (thresholds).**
- **Deferred on external input: VT-13 (Sascha), plus the real-LLM aspects of VT-7/VT-8/VT-10** — all belong to the hardening pass (v0.2), consistent with the spec's own §11 deferrals.

No fabrication: every deferred item is labelled with *why* and *when* it closes. The v0.1
implementation is complete for everything that does not require a live figma engagement or
external (Sascha / threshold-ratification) input.
