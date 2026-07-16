# Semantic Matcher (3b) — Testbench

Empirical grounding for the HELIX 3b "deterministic-first with LLM escalation"
architecture (spec `helix-poc-agno:spec:semantic-matcher-v1-draft`). It validates
the scoring/decision logic in isolation against labelled test cases, where ground
truth is known. The default input source synthesizes "client" catalogs from
controlled mutations of the real HELIX baseline (`helix-code@220a327`); other
sources (golden sets, regression) plug in via the same interface.

## What this is (and is not)
- **Is:** the pure-function scoring module (`agents/semantic_matcher/scoring.py`) +
  a harness that runs a pluggable **input source** through scoring + (mock) LLM
  escalation + a post-validator, and reports metrics (theses A–F). The scoring
  module is reused verbatim by the real 3b agent.
- **Is not:** the full 3b Agno Step/workflow agent (later work). No deployed code
  is touched. No real LLM is called by default.

## Layout
```
agents/semantic_matcher/scoring.py            # deterministic scoring (pure functions)
tests/semantic_matcher/
  test_scoring.py                             # scoring unit tests
  test_mutation_testbench.py                  # harness smoke + invariants
  fixtures/
    baseline_snapshot_220a327.json            # Baseline Reader output (deliverable #1)
    baseline_tokens_enriched_220a327.json     # enriched catalog WITH values (see note)
  testbench/
    run_testbench.py                          # harness + post-validator + metrics reporter (--input-source)
    threshold_sweep.py                        # threshold sensitivity sweep (CSV [+PNG])
    llm_path.py                               # MockLLM (default) + gated RealLLM
    input_sources/
      __init__.py                             # source REGISTRY + MutationSource/GoldenSetSource/RegressionSource
      _common.py                              # SHARED contract: TestCase, TestCaseSource, token shape, loaders
      mutations/                              # the 6 synthetic generators (default source)
        name_only.py value_drift.py provenance_stripped.py
        name_collision.py cross_category.py composite.py
      golden_sets/                            # client hand-labelled sets (placeholder, empty)
      regression/                             # per-bug regression cases (placeholder, empty)
    results/                                  # testbench_*.{json,md}, threshold_sweep_*.csv[,png]
```

## How to run
```bash
cd ~/opencode/workbench/agno-setup/poc-agno-template
# full testbench run (mock LLM, < 60s), default input source = mutations
.venv/bin/python -m tests.semantic_matcher.testbench.run_testbench
#   (direct-script form also works, and takes the flag)
.venv/bin/python tests/semantic_matcher/testbench/run_testbench.py --input-source=mutations
# other sources (empty until populated -> clean exit pointing at their README):
.venv/bin/python tests/semantic_matcher/testbench/run_testbench.py --input-source=golden_sets
.venv/bin/python tests/semantic_matcher/testbench/run_testbench.py --input-source=regression
# threshold sweep (120 combos) -> results/threshold_sweep_<iso>.csv (+ .png if matplotlib)
.venv/bin/python -m tests.semantic_matcher.testbench.threshold_sweep
# tests
.venv/bin/python -m pytest tests/semantic_matcher/ -q
```

## Env vars
- `HELIX_TESTBENCH_USE_REAL_LLM=1` — opt in to the real OpenAIChat/LiteLLM escalation
  path (single-pass, no Reflexion, **costs money**). Unset ⇒ MockLLM only; the
  default run and CI never touch the network.

## Extending the testbench (adding an input source)
Every input source yields the same neutral unit — `TestCase` — and conforms to the
`TestCaseSource` protocol (`input_sources/_common.py`). The harness consumes them
uniformly, so a new source never touches `run_testbench.py`, `scoring.py`, or the
mutation generators.

Two ways to add one:

1. **Drop a module into an existing directory source** (`golden_sets/` or
   `regression/`). Any non-underscore module there is auto-discovered; expose
   `generate() -> list[dict]` (build cases with `.._common.build_case`) or
   `iter_test_cases() -> Iterator[TestCase]`. Example — `golden_sets/acme_v1.py`:
   ```python
   from .._common import build_case  # build_case returns the case dict the harness reads

   def generate() -> list[dict]:
       return [
           build_case(
               mutation_class="golden:acme_v1",           # -> metadata (grouping label)
               client_token={"name": "--acme-brand", "path": "color/brand",
                             "category": "color", "layer": "semantic",
                             "dtcg_type": "color",
                             "value": {"components": [0.2, 0.53, 0.94], "hex": "#3388F0"}},
               expected_baseline_var="--helix-color-brand1-500",  # human-verified, or None if unmapped
               note="labeler=jane; onboarding batch 1",
           ),
       ]
   ```
   Then: `run_testbench.py --input-source=golden_sets`.

2. **Add a whole new source type.** Implement a class with `name: str` and
   `iter_test_cases() -> Iterator[TestCase]`, then register it in
   `input_sources/__init__.py::REGISTRY` (and add an `EMPTY_HINT` entry if it can be
   empty). It becomes a `--input-source` choice automatically.

`TestCase(client_token, expected_baseline_var, metadata)` — `metadata` is an open
dict (`mutation_class`, `note`, `bug_id`, `labeler`, …); the harness groups metrics
by `metadata["mutation_class"]` when present, else by the source name.

## Note — enriched catalog (deviation from the literal deliverable)
`baseline_snapshot_220a327.json` is exactly what Baseline Reader emits, but it
carries token **names only, not values**. Value-veto scoring (thesis B / FM-3b-5)
and the value-drift mutations need values, so we additionally parse the baseline
DTCG leaves into `baseline_tokens_enriched_220a327.json`
(`{name, path, category, layer, dtcg_type, value}`). Regenerate both with
`scratchpad/capture_baseline.py`. The baseline also contains ~22% unresolved DTCG
alias values (e.g. `{colors.interactive.active}`); scoring treats these as
non-scalar (value signal unavailable) rather than crashing.
