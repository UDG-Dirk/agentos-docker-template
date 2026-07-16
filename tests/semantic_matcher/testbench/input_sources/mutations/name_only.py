"""name_only mutation generator — 3b Mutation Testbench.

Produces ~30 cases where ONLY the `name` (the --helix-* css var text) is
mutated. `value`, `path`, `category`, `layer`, and `dtcg_type` are copied
verbatim from a real baseline record — the point of this class is to
check whether the matcher can still recover the correct baseline token
purely from the non-name signals (value/path/layer/category) when the
NAME lies to varying degrees.

Four transforms are exercised in roughly even proportion (8/8/7/7 = 30):
  1. strip the `--helix-` prefix entirely
  2. permute segments (swap the last two, category segment stays first)
  3. synonym substitution (replace the first segment matching the table)
  4. truncation (drop the last segment)

Ground truth is known by construction: every case's `expected_baseline_var`
is the ORIGINAL record's `name`, because only the name field changed —
token identity is unambiguous, only its surface spelling is not.
"""
from __future__ import annotations

import random

from .._common import baseline_by_type, build_case, clone, SEED

CLASS_NAME = "name_only"

# Synonym substitution table. "Apply first that matches a segment" means:
# scan the name's segments left-to-right and replace the first one found
# in this table (dict iteration order here doubles as priority order for
# ties, though segment position is what actually decides in practice).
_SYNONYMS = {
    "primary": "main",
    "secondary": "alt",
    "brand": "marque",
    "background": "bg",
    "error": "danger",
    "default": "base",
}

_PREFIX = "--helix-"


def _segments(name: str) -> list[str]:
    """Dash-separated segments of the css var name AFTER the --helix- prefix.

    e.g. "--helix-color-brand-primary" -> ["color", "brand", "primary"]
    """
    body = name[len(_PREFIX):] if name.startswith(_PREFIX) else name
    return [s for s in body.split("-") if s]


def _strip_prefix(name: str) -> str | None:
    """Transform 1: drop the leading `--helix-` entirely."""
    if not name.startswith(_PREFIX):
        return None
    return name[len(_PREFIX):]


def _permute(name: str) -> str | None:
    """Transform 2: swap the last two segments; category segment stays first.

    e.g. "--helix-color-brand-primary" -> "--helix-color-primary-brand"
    Needs >= 3 segments so the category (segment 0) is left untouched and
    there are two trailing segments to actually swap.
    """
    segs = _segments(name)
    if len(segs) < 3:
        return None
    segs[-1], segs[-2] = segs[-2], segs[-1]
    return _PREFIX + "-".join(segs)


def _synonym_substitute(name: str) -> str | None:
    """Transform 3: replace the FIRST segment that matches `_SYNONYMS`."""
    segs = _segments(name)
    for i, seg in enumerate(segs):
        if seg in _SYNONYMS:
            segs[i] = _SYNONYMS[seg]
            return _PREFIX + "-".join(segs)
    return None


def _truncate(name: str) -> str | None:
    """Transform 4: drop the last segment.

    Needs >= 2 segments so at least one segment (the category) survives.
    """
    segs = _segments(name)
    if len(segs) < 2:
        return None
    return _PREFIX + "-".join(segs[:-1])


# (note, transform fn, target case count) — 8 + 8 + 7 + 7 = 30
_TRANSFORMS = [
    ("stripped --helix- prefix", _strip_prefix, 8),
    ("permuted segments", _permute, 8),
    ("synonym substitution", _synonym_substitute, 7),
    ("truncated last segment", _truncate, 7),
]


def generate() -> list[dict]:
    """Return ~30 name_only cases spread across the four transforms above."""
    rng = random.Random(SEED)

    # Mixed pool: color + number baseline records only (per contract).
    pool = sorted(
        baseline_by_type("color") + baseline_by_type("number"),
        key=lambda r: r["name"],
    )

    used_names: set[str] = set()  # keep source records distinct across transforms
    cases: list[dict] = []

    for note, transform, count in _TRANSFORMS:
        # Candidates: not already used by an earlier transform in this run,
        # AND the transform must actually produce a mutated name for them.
        candidates = sorted(
            (r for r in pool if r["name"] not in used_names and transform(r["name"]) is not None),
            key=lambda r: r["name"],
        )
        picks = rng.sample(candidates, min(count, len(candidates)))
        for r in picks:
            used_names.add(r["name"])
            new_name = transform(r["name"])
            ct = clone(r)  # value/path/category/layer/dtcg_type stay INTACT
            ct["name"] = new_name
            cases.append(build_case(CLASS_NAME, ct, r["name"], note))

    return cases
