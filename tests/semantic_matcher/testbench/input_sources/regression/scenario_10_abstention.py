"""Scenario 10 — LLM abstention regression (recall-gap reproduction).

Regression fixture for the v0.2 recall gap (shared-results:3b-v02-recall-gap-analysis-result):
a cross_category token whose NAME contradicts its DECLARED type and whose VALUE is a
DTCG alias reference. The alias is opaque to the value/type post-validator, so the
mapping path is not caught structurally — only LLM abstention can reject it.

10 adversarial cases, all expected UNMAPPED by construction:
  - 5: a NUMBER/dimension token (name says 'dimension'/'spacing'/...) with an ALIAS
       value, relabelled dtcg_type='color', category='color'.
  - 5: a COLOR token (name says 'color'/'colors'/...) with an ALIAS value, relabelled
       dtcg_type='number', category='dimension'.

Expected behaviour:
  - MOCKLLM_ABSTENTION_RATE_OVERRIDE=1.0 (default): >=9/10 route to unmapped
    (unmapped_reason='llm_abstained').
  - MOCKLLM_ABSTENTION_RATE_OVERRIDE=0.0: tokens accept WRONG mappings — reproduces
    the recall gap (regression guard).

Run: python -m tests.semantic_matcher.testbench.run_testbench --input-source=regression
"""
from __future__ import annotations

from .._common import build_case, clone, load_baseline

CLASS_NAME = "scenario_10_abstention"


def _alias_tokens(dtcg_type: str) -> list[dict]:
    out = [r for r in load_baseline()
           if r["dtcg_type"] == dtcg_type
           and isinstance(r["value"], str) and r["value"].startswith("{")]
    return sorted(out, key=lambda r: r["name"])


def generate() -> list[dict]:
    cases: list[dict] = []

    # 5x: alias-valued NUMBER token declared as COLOR (name says dimension/spacing/...)
    for r in _alias_tokens("number")[:5]:
        ct = clone(r)
        ct["dtcg_type"] = "color"
        ct["category"] = "color"
        cases.append(build_case(
            CLASS_NAME, ct, None,
            f"alias-valued number '{r['name']}' declared as color (name contradicts type)"))

    # 5x: alias-valued COLOR token declared as NUMBER/dimension (name says color/colors)
    for r in _alias_tokens("color")[:5]:
        ct = clone(r)
        ct["dtcg_type"] = "number"
        ct["category"] = "dimension"
        cases.append(build_case(
            CLASS_NAME, ct, None,
            f"alias-valued color '{r['name']}' declared as number (name contradicts type)"))

    return cases
