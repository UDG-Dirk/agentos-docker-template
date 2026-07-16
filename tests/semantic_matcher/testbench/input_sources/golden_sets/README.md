# Golden Sets (input source — placeholder)

Hand-labelled ground-truth mappings from real client onboarding (Challenge 2).
Each client contributes a module here that maps client tokens to baseline vars a
human confirmed.

- **Naming:** `{client_name}_v{n}.py` (e.g. `acme_v1.py`).
- **Contract:** expose `generate() -> list[dict]` (build via `.._common.build_case`,
  with `expected_baseline_var` = the human-verified baseline var, or `None` if the
  reviewer marked it unmapped) OR `iter_test_cases() -> Iterator[TestCase]`.
  Put labeler / provenance info in each case's `note`/metadata.
- **Run:** `python tests/semantic_matcher/testbench/run_testbench.py --input-source=golden_sets`

Empty for now — the harness exits cleanly with a pointer to this file until a
module is added.
