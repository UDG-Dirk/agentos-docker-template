"""composite mutation generator — 3b Mutation Testbench.

~20 typography tokens with PARTIAL sub-field agreement. The helix baseline
@220a327 has no DTCG `typography` composite leaves (font tokens are split into
`string` family names and `number` weights), so we SYNTHESIZE composites:

  value = {"fontFamily": <str>, "fontWeight": <int>, "fontSize": <number>}

fontFamily is drawn from a REAL baseline font token (so it agrees with a baseline
family), while fontWeight is deliberately off. Because there is no composite
baseline token to map to, expected_baseline_var is None for every case: the point
is to confirm the harness (a) does not crash on non-scalar composite values and
(b) does NOT false-accept a partial-agreement composite — it should escalate or
stay unmapped, never deterministic-accept.
"""
from __future__ import annotations

import random

from .._common import SEED, baseline_by_category, clone, build_case

CLASS_NAME = "composite"

_N = 20
_OFF_WEIGHTS = [100, 200, 300, 800, 900]  # weights unlikely to match a body/heading default
_FONT_SIZES = [12, 14, 16, 18, 20, 24, 28, 32]


def _family_strings() -> list[str]:
    """Concrete font-family strings from the baseline (dtcg_type 'string')."""
    fams: list[str] = []
    for r in baseline_by_category("font"):
        v = r["value"]
        if isinstance(v, str) and not v.startswith("{"):  # skip alias references
            fams.append(v)
    # de-dupe, stable order
    seen: set[str] = set()
    out: list[str] = []
    for f in fams:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out or ["dm sans"]  # defensive fallback


def generate() -> list[dict]:
    rng = random.Random(SEED)
    families = _family_strings()
    # source font records (for name/path provenance) — prefer weight-ish tokens
    font_records = sorted(baseline_by_category("font"), key=lambda r: r["name"])
    if not font_records:
        font_records = [{"name": "--helix-font-body", "path": "font/body",
                         "category": "font", "layer": "semantic"}]

    cases: list[dict] = []
    for i in range(_N):
        src = font_records[i % len(font_records)]
        family = families[i % len(families)]
        weight = _OFF_WEIGHTS[i % len(_OFF_WEIGHTS)]
        size = _FONT_SIZES[i % len(_FONT_SIZES)]
        ct = clone(src)
        ct["dtcg_type"] = "typography"
        ct["category"] = "typography"
        ct["value"] = {"fontFamily": family, "fontWeight": weight, "fontSize": size}
        # name references a plausible client typography token
        ct["name"] = f"--client-typography-{i:02d}"
        note = (f"typography partial agree: family={family!r} (matches baseline) "
                f"weight={weight} (mismatch)")
        cases.append(build_case(CLASS_NAME, ct, None, note))
    return cases
