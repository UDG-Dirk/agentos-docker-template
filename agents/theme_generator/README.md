# 3c Theme Generator — Agent 3c (v0.1)

Forks the **helix-code baseline** into a **customer-specific component-library package**
by combining three inputs:

- **3a Baseline Reader** output — reference components, tokens, structure.
- **3b Semantic Matcher** scoring — per-component client→baseline mapping (imported as a
  library, in-process; no `matcher.py` endpoint — YAGNI per spec §11.2).
- **HELIX extractor** `FigmaExtractionResult` — the client's actual design content.

Output is a self-contained fork of the baseline package (Decision #1) laid out like
helix-code (Decision #2), with a customer-facing `PROVENANCE.md` recording what came from
the baseline vs. the client vs. a 3c agent.

**Status:** v0.1 **Phase 1 (deterministic) implemented.** Workflow id `helix-theme-generator`
(registered in `app/main.py`). Agentic reconciliation + cohesion review are Phase 2.

## Phases

| Phase | Steps | Nature | Status |
|-------|-------|--------|--------|
| 1 | `theme-input-gathering`, `theme-deterministic-transform` | deterministic, zero-LLM | **done** |
| 2 | `agentic_reconciliation`, `cohesion_review` | agent (fine-grained per unmapped + one coarse pass) | pending |
| 3 | `provenance_generation`, `output_assembly` | deterministic | partial (provenance already emitted in Phase 1) |

The output envelope (`models.py::ThemeGeneratorOutput`) is already the full v0.1 shape, so
Phase 2 adds behaviour without changing the contract.

## Deterministic vs agentic boundary (spec §5.3)

Each client component is classified by its 3b `ScoringResult`:

| 3b result | Phase-1 action | Emitted? |
|-----------|----------------|----------|
| `matched_via=deterministic` + `deterministic_confidence ∈ {authoritative, high}` | **fork** baseline component + substitute tokens | yes |
| `matched_via=no_candidates` | **passthrough** — no baseline equivalent, keep client's (spec §5.1.4) | yes |
| `matched_via=llm_required`, or deterministic below `high` | **defer** to Phase-2 agent — recorded in provenance, run goes `partial` | no (never fabricated) |

A run is `success` only when every component resolved deterministically; `partial` when any
deferred; `failure` when baseline or client extraction is missing (fail-loud, SP-6 — no package).

## Rule-12 corrections (implementation-time, vs the v0.1 spec draft)

Applied during implementation; the errors are **not** propagated:

1. **Model routing (spec §7.1).** Spec says "Claude Sonnet 4.6 default". Actual: 3c routes
   through the LiteLLM proxy to `OPENAI_MODEL_ID` (default `gpt-5.4`) via
   `app.settings.default_chat_model()` (tools need `OpenAIChat`; `OpenAIResponses` breaks
   tool round-trips via LiteLLM). Phase 1 is deterministic and invokes no model; Phase 2
   agents will use `default_chat_model()` — **never a hardcoded model name** (SP-9).
2. **3b contract (spec §3.2 sketch).** The spec sketched `matched_via ∈
   {deterministic, escalated_to_llm, abstained}` and confidences incl. `medium`/`unresolved`.
   The REAL `ScoringResult.matched_via ∈ {deterministic, llm_required, no_candidates}` and
   `deterministic_confidence ∈ {authoritative, high, None}`. The boundary above is coded
   against the real enum (see `scaffolding.classify_component`).
3. **Section 8 FinOps** is populated from Probe 3
   (`shared-results:3c-llm-cost-baseline-probe-result-2026-07-31`): `CostSummary` carries the
   invocation/token rollup + a `breaker_tripped` circuit-breaker flag for Phase 2.

## Files

```
agents/theme_generator/
  models.py        # ThemeGeneratorOutput envelope + provenance/warning/summary contracts
  scaffolding.py   # deterministic transform: package tree, token substitution, forking, passthrough
  step.py          # generate_theme_phase1 (testable core) + Agno step executors
app/workflows/
  helix_theme_generator.py   # the 2-step Phase-1 workflow (id helix-theme-generator)
tests/theme_generator/
  test_theme_generator_phase1.py   # fixture-based EXACT assertions (real ScoringResult fixtures)
```

## Invocation (Phase 1)

`POST /workflows/helix-theme-generator/runs` with inputs in `additional_data`:

```json
{
  "customer_slug": "acme",
  "scope": "msq-dx",
  "baseline": { "...": "3a BaselineReaderOutput" },
  "client_extraction": { "...": "FigmaExtractionResult" },
  "scoring_by_slot": { "Button": { "...": "3b ScoringResult" } },
  "output_dir": "/optional/path/to/materialise/the/package"
}
```

Deterministic (temp/seed n/a in Phase 1 — no model calls); the envelope always carries
`non_deterministic: true` per Decision #5, and blocking conditions surface as
`blocking_warnings`, never as exceptions.
