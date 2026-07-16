"""provenance_stripped mutation generator.

Three sub-classes x 10 cases = 30 total. Starting from real baseline records
(mixed color + number), we strip provenance SIGNALS (layer and/or path) while
keeping name + value + dtcg_type + category intact. Ground truth is always the
origin record's name: the point of this class is measuring whether name+value
alone still carry the match once layer/path signals vanish.
"""
from __future__ import annotations

import random

from .._common import SEED, baseline_by_type, clone, build_case

CLASS_NAME = "provenance_stripped"


def _strip_layer(client_token: dict) -> None:
    client_token["layer"] = None


def _strip_path(client_token: dict) -> None:
    client_token["path"] = None


def _strip_both(client_token: dict) -> None:
    client_token["layer"] = None
    client_token["path"] = None


_SUB_CLASSES = (
    ("layer stripped", _strip_layer),
    ("path stripped", _strip_path),
    ("layer+path stripped", _strip_both),
)


def generate() -> list[dict]:
    rng = random.Random(SEED)
    pool = sorted(
        baseline_by_type("color") + baseline_by_type("number"),
        key=lambda r: r["name"],
    )
    picks = rng.sample(pool, 30)

    cases: list[dict] = []
    for i, (note, mutate) in enumerate(_SUB_CLASSES):
        group = picks[i * 10 : (i + 1) * 10]
        for r in group:
            client_token = clone(r)
            mutate(client_token)
            cases.append(build_case(CLASS_NAME, client_token, r["name"], note))
    return cases
