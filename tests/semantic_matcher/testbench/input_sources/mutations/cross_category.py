"""cross_category mutation generator — 3b Mutation Testbench.

Flips `dtcg_type` (and `category`) so the declared type LIES about the actual
content, while `name`/`path`/`value` keep pointing at the original baseline
token (to tempt a name/path match). The category/type pre-filter plus the
value-branch mismatch should reject every case -> expected_baseline_var=None.
"""
from __future__ import annotations

import random

from .._common import SEED, baseline_by_type, clone, build_case

CLASS_NAME = "cross_category"

_COLOR_AS_NUMBER_CATEGORY = "dimension"
_NUMBER_AS_COLOR_CATEGORY = "color"


def generate() -> list[dict]:
    rng = random.Random(SEED)
    cases: list[dict] = []

    # 5 color records: keep the color value, but declare number/dimension.
    color_pool = sorted(baseline_by_type("color"), key=lambda r: r["name"])
    color_picks = rng.sample(color_pool, 5)
    for r in color_picks:
        ct = clone(r)  # name/path/layer/value untouched
        ct["dtcg_type"] = "number"
        ct["category"] = _COLOR_AS_NUMBER_CATEGORY
        cases.append(
            build_case(CLASS_NAME, ct, None, "declared color as number")
        )

    # 5 number records: keep the numeric value, but declare color.
    number_pool = sorted(baseline_by_type("number"), key=lambda r: r["name"])
    number_picks = rng.sample(number_pool, 5)
    for r in number_picks:
        ct = clone(r)  # name/path/layer/value untouched
        ct["dtcg_type"] = "color"
        ct["category"] = _NUMBER_AS_COLOR_CATEGORY
        cases.append(
            build_case(CLASS_NAME, ct, None, "declared number as color")
        )

    return cases
