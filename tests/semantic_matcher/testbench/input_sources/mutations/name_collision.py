"""name_collision mutation generator.

20 collision PAIRS -> 40 client tokens (2 per pair). Each pair picks two
DIFFERENT baseline records A and B of the SAME dtcg_type but DIFFERENT
values. Both client tokens in a pair get the SAME mutated `name` (a generic
collision name), but each keeps its OWN value/path/layer/category. This
forces FM-3b-5: the name alone says "match X", but the value must
disambiguate which real baseline token (A or B) the client actually is.
"""
from __future__ import annotations

import random

from .._common import SEED, baseline_by_type, clone, build_case

CLASS_NAME = "name_collision"

_PAIRS_PER_TYPE = {
    "color": 10,
    "number": 10,
}


def _make_pairs(dtcg_type: str, rng: random.Random, n_pairs: int) -> list[tuple[dict, dict]]:
    """Pick n_pairs of (A, B) baseline records of dtcg_type, A != B, values differ."""
    pool = sorted(baseline_by_type(dtcg_type), key=lambda r: r["name"])
    picks = rng.sample(pool, 2 * n_pairs)
    picked_names = {r["name"] for r in picks}
    leftovers = sorted(
        (r for r in pool if r["name"] not in picked_names),
        key=lambda r: r["name"],
    )

    pairs: list[tuple[dict, dict]] = []
    for i in range(n_pairs):
        a = picks[2 * i]
        b = picks[2 * i + 1]
        # Guard: if by chance A and B carry the same value, swap B for a
        # deterministic leftover record with a differing value.
        if a["value"] == b["value"]:
            replacement = None
            for candidate in leftovers:
                if candidate["value"] != a["value"]:
                    replacement = candidate
                    break
            if replacement is not None:
                leftovers.remove(replacement)
                b = replacement
        pairs.append((a, b))
    return pairs


def generate() -> list[dict]:
    rng = random.Random(SEED)

    all_pairs: list[tuple[dict, dict]] = []
    for dtcg_type in sorted(_PAIRS_PER_TYPE):
        n_pairs = _PAIRS_PER_TYPE[dtcg_type]
        all_pairs.extend(_make_pairs(dtcg_type, rng, n_pairs))

    cases = []
    for i, (a, b) in enumerate(all_pairs, start=1):
        collision_name = f"--helix-color-token-{i}"

        ct_a = clone(a)
        ct_a["name"] = collision_name
        cases.append(
            build_case(CLASS_NAME, ct_a, a["name"], f"collision pair {i} side A")
        )

        ct_b = clone(b)
        ct_b["name"] = collision_name
        cases.append(
            build_case(CLASS_NAME, ct_b, b["name"], f"collision pair {i} side B")
        )

    return cases
