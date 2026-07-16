"""Shared contract for the 3b mutation testbench.

ALL mutation generators AND the harness import from here so the token shape is
identical everywhere. Do not fork these shapes in a generator.

------------------------------------------------------------------------------
CANONICAL TOKEN RECORD (the testbench lingua franca)
------------------------------------------------------------------------------
Both baseline candidates and synthesized client tokens are flat dicts:

    {
      "name":      str,            # --helix-* css var name  (NAME signal)
      "path":      str | None,     # slash path, lowercase    (PATH signal; None => no path signal)
      "category":  str,            # path segment 0            (category pre-filter)
      "layer":     "primitive" | "semantic" | None,   # LAYER signal (None => no layer signal)
      "dtcg_type": str,            # "color" | "number" | "string" | ...  (VALUE branch selector)
      "value":     <see below>,    # VALUE signal
    }

VALUE shapes by dtcg_type (as they appear in the real helix baseline @220a327):
  - "color":  {"colorSpace":"srgb","components":[r,g,b] floats 0..1,"alpha":float,"hex":"#RRGGBB"}
  - "number": a scalar int/float (helix is unitless; dimensions/spacing/radius are numbers)
  - "string": a str (e.g. font family "dm sans")

------------------------------------------------------------------------------
GENERATOR CONTRACT
------------------------------------------------------------------------------
Each generator module exposes:
    CLASS_NAME: str                 # e.g. "name_only"
    def generate() -> list[dict]    # each item built via build_case(...)

A case dict (build_case output):
    {
      "mutation_class":       str,          # == CLASS_NAME
      "client_token":         dict,         # canonical record above (the MUTATED token)
      "expected_baseline_var": str | None,  # ground truth: the --helix-* it SHOULD map to,
                                            #   or None if it should be UNMAPPED by construction
      "note":                 str,          # short human description of the mutation
    }

Ground truth is known BY CONSTRUCTION: you start from a real baseline record,
mutate it, and you already know which baseline var it derived from (or that it
should not map at all). Keep generators deterministic (seed any RNG with SEED).

------------------------------------------------------------------------------
FORMAL INPUT-SOURCE INTERFACE
------------------------------------------------------------------------------
Mutations are ONE input source among several (golden sets, regression cases).
Every source yields the same neutral unit — ``TestCase`` — and conforms to the
``TestCaseSource`` protocol (see ``input_sources/__init__.py`` for the registry
and the concrete sources). The harness consumes ``TestCase`` uniformly and never
needs to know which source produced it:

    class TestCaseSource(Protocol):
        name: str
        def iter_test_cases(self) -> Iterator[TestCase]: ...

    TestCase(client_token=..., expected_baseline_var=..., metadata={...})

``metadata`` is an open dict for source-specific info (mutation_class, drift
level, golden-set labeler, bug id, ...). The six mutation generators keep their
``generate() -> list[dict]`` shape (via ``build_case``); ``MutationSource`` adapts
those dicts into ``TestCase`` objects, so adding a new source never touches them.
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterator, NamedTuple, Optional, Protocol, runtime_checkable

SEED = 20260715  # deterministic generators


# --------------------------------------------------------------------------- #
# formal input-source interface
# --------------------------------------------------------------------------- #
class TestCase(NamedTuple):
    """One labelled test case, source-agnostic.

    client_token          : canonical token record (the token to be matched)
    expected_baseline_var : ground-truth --helix-* var, or None if UNMAPPED
    metadata              : open dict for source-specific info
                            (e.g. {"mutation_class": ..., "note": ...},
                             {"golden_set": ..., "labeler": ...},
                             {"bug_id": ...})
    """

    client_token: dict
    expected_baseline_var: Optional[str]
    metadata: dict


@runtime_checkable
class TestCaseSource(Protocol):
    """A pluggable provider of labelled test cases for the harness.

    Implementations live under ``input_sources/`` (mutations, golden_sets,
    regression) and are wired into the registry in ``input_sources/__init__.py``.
    """

    name: str

    def iter_test_cases(self) -> Iterator[TestCase]:
        """Yield every TestCase this source provides (possibly none)."""
        ...


_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
BASELINE_FIXTURE = _FIXTURES / "baseline_tokens_enriched_220a327.json"

# type families for the harness category/type pre-filter
_TYPE_FAMILY = {
    "color": "color",
    "number": "numeric",
    "dimension": "numeric",
    "fontWeight": "numeric",
    "string": "string",
    "fontFamily": "string",
}


def type_family(dtcg_type: Optional[str]) -> str:
    return _TYPE_FAMILY.get(dtcg_type or "", "other")


_BASELINE_CACHE: Optional[list[dict]] = None


def load_baseline() -> list[dict]:
    """The 510 enriched baseline records (name/path/category/layer/dtcg_type/value)."""
    global _BASELINE_CACHE
    if _BASELINE_CACHE is None:
        _BASELINE_CACHE = json.loads(BASELINE_FIXTURE.read_text(encoding="utf-8"))
    return _BASELINE_CACHE


def baseline_by_type(dtcg_type: str) -> list[dict]:
    return [r for r in load_baseline() if r["dtcg_type"] == dtcg_type]


def baseline_by_category(category: str) -> list[dict]:
    return [r for r in load_baseline() if r["category"] == category]


def baseline_by_name(name: str) -> Optional[dict]:
    for r in load_baseline():
        if r["name"] == name:
            return r
    return None


def clone(record: dict) -> dict:
    """Deep copy a baseline record so mutations never touch the shared cache."""
    return deepcopy(record)


def build_case(
    mutation_class: str,
    client_token: dict,
    expected_baseline_var: Optional[str],
    note: str,
) -> dict:
    # light structural guard — fail loud in the generator, not deep in the harness
    for k in ("name", "category", "dtcg_type", "value"):
        if k not in client_token:
            raise ValueError(f"client_token missing '{k}': {client_token}")
    if "path" not in client_token:
        client_token["path"] = None
    if "layer" not in client_token:
        client_token["layer"] = None
    return {
        "mutation_class": mutation_class,
        "client_token": client_token,
        "expected_baseline_var": expected_baseline_var,
        "note": note,
    }


# ---- prefilter used by the HARNESS (not by generators) ---------------------
def prefilter_candidates(client_token: dict, baseline: list[dict]) -> list[dict]:
    """Candidates in the client's declared CATEGORY (spec: 'pre-filtered by category').

    Category-grain (color / colors / component_colors / dimension / ...) keeps the
    candidate pool small enough that top1-top2 margins are meaningful, which is the
    whole premise of the margin-based deterministic gate. cross_category mutants
    carry a LIED category, so they land in the wrong pool; the harness value/type
    post-validator then rejects them (expected unmapped). Falls back to the type
    family only if the declared category matches nothing (robustness).
    """
    cat = client_token.get("category")
    hits = [r for r in baseline if r.get("category") == cat]
    if hits:
        return hits
    fam = type_family(client_token.get("dtcg_type"))
    return [r for r in baseline if type_family(r["dtcg_type"]) == fam]
