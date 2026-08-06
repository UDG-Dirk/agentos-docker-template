"""Overlay Packager — deterministic token-value substitution (v0.3 Phase 2).

Architecture B: customer branding is a token VALUE swap in the fork's Style
Dictionary source. Token NAMES are preserved; ``--helix-*`` CSS var refs are
never renamed (Correction #18, on main via MR !47). 3c no longer runs this as
a separate step (Dirk-ratified 2026-08-06) — the packager owns it directly.

DTCG leaf shape (empirical, helix-code packages/tokens/tokens/semantic-color/
light-mode.tokens.json [210 leaves, depth 5, all $type=color] and
semantic-dimension/Desktop.tokens.json [119 leaves, depth 5, all $type=number]):
a dict carrying ``$value`` is a leaf. ``$value`` is either a literal (an object
for color: {colorSpace, components, alpha, hex}; a plain number for dimension)
or a DTCG alias string ``"{other.token.path}"``. Sibling fields — ``$type``,
and ``$extensions`` (Figma provenance: variableId, scopes, aliasData) — are
never touched. Non-leaf dict keys starting with ``$`` (e.g. a group's own
``$type``/``$description``) are skipped when walking children.

Dependency-free (dataclasses, copy, logging stdlib only) — mirrors
agents/baseline_reader/reader.py's convention.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


class CoverageGateError(Exception):
    """FM1: an sd_source_tree leaf has no customer value and no known_uncovered
    annotation. Fail-loud, never silent (Phase 2 constraint)."""


def _is_leaf(node: Any) -> bool:
    return isinstance(node, dict) and "$value" in node


def _walk_leaves(node: Any, path: list[str], out: list[tuple[str, dict]]) -> None:
    """Depth-first collect of DTCG leaves, in insertion order (dict order)."""
    if _is_leaf(node):
        out.append((".".join(path), node))
        return
    if isinstance(node, dict):
        for key, child in node.items():
            if key.startswith("$"):
                continue
            _walk_leaves(child, path + [key], out)


@dataclass
class KnownUncovered:
    """An explicitly-annotated exception to the FM1 coverage gate."""

    path: str
    reason: str
    owner: str


@dataclass
class CoverageReport:
    total_leaves: int
    matched: int
    unmatched: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    known_uncovered: list[KnownUncovered] = field(default_factory=list)


def substitute_token_values(
    sd_source_tree: dict,
    customer_token_map: dict,
) -> tuple[dict, CoverageReport]:
    """Traverse ``sd_source_tree``; for each leaf whose path matches a key in
    ``customer_token_map``, replace ``$value`` with the customer value.
    Preserves ``$type`` and all other fields; never renames a token path.
    Returns (modified tree — a deep copy, input untouched; coverage report).
    """
    tree = deepcopy(sd_source_tree)
    leaves: list[tuple[str, dict]] = []
    _walk_leaves(tree, [], leaves)

    matched = 0
    unmatched: list[str] = []
    seen_paths: set[str] = set()
    for path, leaf in leaves:
        seen_paths.add(path)
        if path in customer_token_map:
            leaf["$value"] = customer_token_map[path]
            matched += 1
        else:
            unmatched.append(path)

    extra = sorted(p for p in customer_token_map if p not in seen_paths)
    report = CoverageReport(
        total_leaves=len(leaves),
        matched=matched,
        unmatched=sorted(unmatched),
        extra=extra,
    )
    return tree, report


def assert_coverage(
    report: CoverageReport,
    known_uncovered: list[KnownUncovered] | None = None,
) -> CoverageReport:
    """FM1 coverage gate.

    Any ``report.unmatched`` path not covered by a ``known_uncovered``
    annotation -> raise ``CoverageGateError`` (fail-loud, lists the paths).
    ``report.extra`` -> WARN only, never fails (customer map may be richer
    than today's baseline). Mutates + returns ``report`` with
    ``known_uncovered`` attached, so the caller can serialise it into the
    packager envelope for MR body inclusion.
    """
    if known_uncovered is not None:
        report.known_uncovered = list(known_uncovered)

    known_paths = {ku.path for ku in report.known_uncovered}
    truly_unmatched = [p for p in report.unmatched if p not in known_paths]
    if truly_unmatched:
        raise CoverageGateError(
            f"{len(truly_unmatched)} token leaf(ves) have no customer value and "
            f"no known_uncovered annotation: {truly_unmatched}"
        )

    if report.extra:
        logger.warning(
            "customer_token_map has %d path(s) not found in sd_source_tree: %s",
            len(report.extra),
            report.extra,
        )
    return report
