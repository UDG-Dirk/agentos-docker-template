"""value_drift mutation generator — 3b Mutation Testbench.

Keeps name/path/category/layer/dtcg_type INTACT; drifts ONLY `value`.

Six drift levels x 5 cases each = 30 cases:
  - color +/-3 hex units   (5 cases)
  - color +/-8 hex units   (5 cases)
  - color +/-20 hex units  (5 cases)
  - number +/-5%           (5 cases)
  - number +/-15%          (5 cases)
  - number +/-50%          (5 cases)

expected_baseline_var is the ORIGINAL baseline name for every case: drift
preserves token identity by construction — the point of this mutation class
is measuring at which drift magnitude the value-veto should start rejecting
a match, not whether identity changed.
"""
from __future__ import annotations

import random

from .._common import SEED, baseline_by_type, clone, build_case

CLASS_NAME = "value_drift"

_COLOR_LEVELS = [3, 8, 20]
_NUMBER_LEVELS = [5, 15, 50]
_CASES_PER_LEVEL = 5

# Deterministic alternating sign pattern applied across the 5 cases of every
# level (3 "+" / 2 "-" per level) — fixed, not randomized, for reproducibility.
_SIGNS = [1, -1, 1, -1, 1]


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _clamp255(value: int) -> int:
    return max(0, min(255, value))


def _drift_color(record: dict, hex_units: int, sign: int) -> dict:
    ct = clone(record)
    value = ct["value"]
    delta = sign * (hex_units / 255.0)
    new_components = [_clamp01(c + delta) for c in value["components"]]
    value["components"] = new_components
    r, g, b = (_clamp255(int(round(c * 255))) for c in new_components)
    value["hex"] = "#%02X%02X%02X" % (r, g, b)
    return ct


def _drift_number(record: dict, pct: int, sign: int) -> dict:
    ct = clone(record)
    factor = 1 + sign * (pct / 100.0)
    ct["value"] = round(ct["value"] * factor, 6)
    return ct


def generate() -> list[dict]:
    rng = random.Random(SEED)
    cases: list[dict] = []

    # Only tokens with CONCRETE values — the helix baseline carries unresolved
    # DTCG alias references (e.g. "{colors.interactive.active}") as string values
    # (98 colors, 13 numbers @220a327); those have no numeric value to drift.
    color_pool = sorted(
        (r for r in baseline_by_type("color")
         if isinstance(r["value"], dict) and "components" in r["value"]),
        key=lambda r: r["name"],
    )
    number_pool = sorted(
        (r for r in baseline_by_type("number") if isinstance(r["value"], (int, float))),
        key=lambda r: r["name"],
    )

    color_picks = rng.sample(color_pool, len(_COLOR_LEVELS) * _CASES_PER_LEVEL)
    number_picks = rng.sample(number_pool, len(_NUMBER_LEVELS) * _CASES_PER_LEVEL)

    idx = 0
    for level in _COLOR_LEVELS:
        for i in range(_CASES_PER_LEVEL):
            r = color_picks[idx]
            idx += 1
            sign = _SIGNS[i]
            ct = _drift_color(r, level, sign)
            note = f"color {'+' if sign > 0 else '-'}{level} hex"
            cases.append(build_case(CLASS_NAME, ct, r["name"], note))

    idx = 0
    for level in _NUMBER_LEVELS:
        for i in range(_CASES_PER_LEVEL):
            r = number_picks[idx]
            idx += 1
            sign = _SIGNS[i]
            ct = _drift_number(r, level, sign)
            note = f"number {'+' if sign > 0 else '-'}{level}%"
            cases.append(build_case(CLASS_NAME, ct, r["name"], note))

    return cases
