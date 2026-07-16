# Regression Cases (input source — placeholder)

Per-bug regression cases captured when a matching defect is found (e.g. during the
HARDEN stage or in production). Each locks in the correct behaviour so the bug
cannot silently return.

- **Naming:** `bug_{id}_{short_slug}.py` (e.g. `bug_142_color_alias_collision.py`).
- **Contract:** expose `generate() -> list[dict]` (build via `.._common.build_case`;
  `expected_baseline_var` = the correct mapping, or `None` if it should be unmapped)
  OR `iter_test_cases() -> Iterator[TestCase]`. Record the originating `bug_id` in
  the case `note`/metadata.
- **Run:** `python tests/semantic_matcher/testbench/run_testbench.py --input-source=regression`

Empty for now — the harness exits cleanly with a pointer to this file until a
module is added.
