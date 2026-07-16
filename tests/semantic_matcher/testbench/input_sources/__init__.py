"""Input sources for the 3b testbench.

An input source turns some ground-truth origin (synthetic mutations, hand-labelled
golden sets, regression cases from HARDEN) into a stream of ``TestCase`` objects.
The harness selects one by name via ``--input-source`` and consumes it through the
``TestCaseSource`` protocol — it never needs to know the origin.

Registry (name -> source):
    mutations    MutationSource     — the 6 synthetic mutation generators (default)
    golden_sets  GoldenSetSource    — client hand-labelled sets (placeholder, empty)
    regression   RegressionSource   — per-bug regression cases (placeholder, empty)

Adding a source type: implement ``iter_test_cases() -> Iterator[TestCase]`` (see
``_common.TestCaseSource``) and register it in ``REGISTRY`` below. Directory-based
sources (golden_sets, regression) just need a module exposing ``generate()``
(list of ``build_case`` dicts) or ``iter_test_cases()`` dropped into their folder.
"""
from __future__ import annotations

import importlib
import pkgutil
from typing import Iterator

from ._common import TestCase, TestCaseSource, build_case  # noqa: F401 (re-exported)

MUTATION_CLASSES = [
    "name_only", "value_drift", "provenance_stripped",
    "name_collision", "cross_category", "composite",
]


def _cases_from_module(mod) -> Iterator[TestCase]:
    """Adapt a source module (generate()->list[dict] or iter_test_cases()) to TestCase."""
    if hasattr(mod, "iter_test_cases"):
        yield from mod.iter_test_cases()
        return
    for c in mod.generate():
        if isinstance(c, TestCase):
            yield c
        else:  # build_case dict -> TestCase (mutation_class/note -> metadata)
            meta = {k: v for k, v in c.items()
                    if k not in ("client_token", "expected_baseline_var")}
            yield TestCase(client_token=c["client_token"],
                           expected_baseline_var=c["expected_baseline_var"],
                           metadata=meta)


class MutationSource:
    """The six synthetic mutation generators, as one source."""

    name = "mutations"

    def iter_test_cases(self) -> Iterator[TestCase]:
        for m in MUTATION_CLASSES:
            mod = importlib.import_module(f"{__name__}.mutations.{m}")
            yield from _cases_from_module(mod)


class _DirectorySource:
    """Generic source: every non-private module in ``subpackage`` contributes cases.

    Empty directory -> yields nothing (the harness reports that cleanly).
    """

    def __init__(self, subpackage: str, display: str) -> None:
        self.name = display
        self._subpackage = subpackage

    def iter_test_cases(self) -> Iterator[TestCase]:
        pkg = importlib.import_module(f"{__name__}.{self._subpackage}")
        for info in pkgutil.iter_modules(pkg.__path__):
            if info.name.startswith("_"):
                continue
            mod = importlib.import_module(f"{__name__}.{self._subpackage}.{info.name}")
            yield from _cases_from_module(mod)


class GoldenSetSource(_DirectorySource):
    def __init__(self) -> None:
        super().__init__("golden_sets", "golden_sets")


class RegressionSource(_DirectorySource):
    def __init__(self) -> None:
        super().__init__("regression", "regression")


REGISTRY: dict[str, type] = {
    "mutations": MutationSource,
    "golden_sets": GoldenSetSource,
    "regression": RegressionSource,
}

# per-source hint shown when a source is empty
EMPTY_HINT = {
    "golden_sets": "No golden sets configured; see input_sources/golden_sets/README.md",
    "regression": "No regression cases configured; see input_sources/regression/README.md",
}


def get_source(name: str) -> TestCaseSource:
    if name not in REGISTRY:
        raise ValueError(f"unknown input source '{name}'; choose from {sorted(REGISTRY)}")
    return REGISTRY[name]()
