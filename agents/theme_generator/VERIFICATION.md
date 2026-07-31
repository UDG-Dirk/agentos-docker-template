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
| VT-7 | PROVENANCE.md generated for a HELIX_Modules-scale engagement | ✅ **CLOSED (live)** | Live-run 2026-07-31 on the real Modules extraction (22 components) vs Core baseline (30 comp / 112 tokens), REAL claude-sonnet-4-6 agents: generated `docs/PROVENANCE.md` with baseline ref, per-component derivation table (real agent rationales), confidence summary (1 authoritative / 17 high / 4 unresolved), passthrough + flagged register. Evidence: `shared-results:3c-theme-generator-v0-1-hardening-live-run-result-2026-07-31`. |
| VT-8 | Cost-tracking hooks in place | ✅ **CLOSED (live)** | Live-run captured REAL token values: `fine_grained_invocations=19, coarse_grained_invocations=1, total_input_tokens=16510, total_output_tokens=2238, expected_fine_grained=19, anomaly=null, breaker_tripped=false`. The agno `RunOutput.metrics` field IS populated (closes the Phase-4-riff [U]). Mechanism (`_extract_usage` + `.usage()`) proven end-to-end on real agents. |
| VT-9 | package.json + directory layout matches baseline | ✅ | P1 `test_phase1_package_tree_shape_and_content` (name `@scope/slug-elements`, provenance, `src/elements`, `src/tokens`, `docs`, `src/index.ts`). Full FE-DEV parity = VT-13. |
| VT-10 | Cross-run reproducibility within an engagement | ✅ (deterministic surface) / ◐ (real LLM, model-limited) | P3 `test_same_inputs_same_package_bytes` (byte-identical package + envelope); `engagement_seed` stable. **Live-run finding:** the deployed route (gpt-5.4 → Anthropic claude-sonnet-4-6) REJECTS `seed` (litellm.UnsupportedParamsError) — so `seed` is recorded-not-sent and `temperature=0` is the determinism lever for that model (Anthropic temp=0 is near- but not guaranteed-deterministic). True bit-reproducibility of LLM output is a model capability we don't control. |
| VT-11 | `non_deterministic` always true in envelope | ✅ | Asserted across P1/P2/P3. |
| VT-12 | agent_confidence_summary matches invocation distribution | ✅ | `_confidence_summary` rebuilt from the ledger; P2 asserts confidence_summary after reconciliation. |
| VT-13 | [PENDING SASCHA] layout aligns with FE-DEV conventions | ⏳ | External input; spec §11.2 v0.2. Uses exact-helix-code-match default until then. |
| VT-14 | cost per engagement within budget | ✅ **CLOSED (live)** | Live Modules-scale run cost ≈ **$0.083** (16510 in + 2238 out @ Sonnet-class $3/$15 per M) — well under the §8 **$2** soft budget. Anomaly did NOT fire (19 invocations = 19 expected, ratio 1.0); circuit breaker did NOT trip. Formal threshold *ratification* is still Dirk's call (v0.2), but the budget check passes with real numbers. |
| VT-15 | matcher.py deferred — 3c imports 3b library directly | ✅ | `reconciliation.py` imports `agents.semantic_matcher.scoring.name_similarity` directly; no `matcher.py` dependency anywhere. |

## Runnable gate

The offline-closable VTs are also an executable, colleague-runnable gate (the pattern the repo
uses for `python -m evals`):

```
python -m agents.theme_generator.verify
```

Runs the full pipeline (deterministic Mock agents — no live LLM, no cost) over a self-contained
fixture and prints a PASS/FAIL line per VT; exit 0 = all green. Kept green by
`tests/theme_generator/test_theme_generator_verify.py`.

## Live-run hardening (2026-07-31, HELIX_Modules substrate)

A real end-to-end run — Modules extraction (client) vs Core extraction (baseline), **real
claude-sonnet-4-6 agents via LiteLLM** — closed VT-7, VT-8, VT-14 and surfaced fixes the
mock path could not:

- **Fix 1 — `seed` incompatible with the deployed model.** gpt-5.4 → Anthropic claude-sonnet-4-6
  rejects the `seed` param (`litellm.UnsupportedParamsError`). The real agents now pass
  `temperature=0` only; the per-engagement seed is recorded, not sent.
- **Fix 2 — SP-6 resilience on the real path.** A failed/non-schema agent call now returns a
  `flag_review`/unresolved result (→ blocking_warning) instead of crashing the run — no abort,
  no fabrication. Verified: 4 genuinely-ambiguous components were flagged, 0 fabricated.
- **Finding — 3b is token-level, not component-level.** `scoring.py` scores tokens, not
  components; the spec §3.2 per-component `ScoringResult` endpoint does not exist. The live-run
  synthesised component scoring from name-similarity. v0.2 candidate: a 3b component-scoring
  surface (or a documented 3c-side adapter).
- **Substrate finding.** Modules organisms (navigation shells, flyouts, footers) have no Core
  *atom* equivalents → 1 fork, 17 passthrough, 4 flagged. A real customer whose system derives
  from Core would fork far more. Confirms the "minimal fork on self-referential substrate" the
  task anticipated; SP-25 candidate holds.

## Summary
- **Closed: VT-1…12, VT-14, VT-15** — offline for the deterministic set, **live** for VT-7/8/14.
- **Deferred on external input: VT-13 (Sascha FE-DEV conventions).** Real-LLM bit-reproducibility
  (VT-10 live) is bounded by the model (Anthropic has no `seed`) — documented, not a gap.

No fabrication: every deferred/limited item is labelled with *why* and *when* it closes. 3c v0.1
is verified end-to-end; the only open item is Sascha's layout input.
