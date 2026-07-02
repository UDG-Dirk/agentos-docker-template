"""Shared test utilities for the Token Normalizer harness.

Two helpers used across the contract/behavioral tests:

* ``resolve_dtcg_path(tree, dotted_path)`` — walk an arbitrary-depth DTCG group
  dict by dot-separated segments and return the *leaf* node (a dict carrying
  ``$value``) or ``None``. Used by CT-14, BT-2, BT-3, BT-4.
* ``build_extraction(token_dicts, ...)`` — a minimal, extensible synthetic
  fixture factory that assembles a ``FigmaExtractionResult`` from a list of
  crafted ``TokenEntry`` dicts. Used by the edge-case tests BT-10..BT-15.

The factory imports ``FigmaExtractionResult`` lazily so this module stays
import-safe even before ``sys.path`` is patched at collection time (the test
module patches the path before any factory call happens).
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

# DTCG marker keys (kept here so both helpers and tests share one definition).
DTCG_VALUE_KEY = "$value"


def resolve_dtcg_path(tree: dict, dotted_path: str) -> Optional[dict]:
    """Traverse ``tree`` by ``dotted_path`` (segments split on ``.``).

    Returns the resolved node IF it is a leaf token (a dict containing
    ``$value``); otherwise returns ``None``. Tolerant of:

    * arbitrary nesting depth,
    * a final segment that lands on a group rather than a leaf (-> None),
    * any segment that is missing or whose node is not a dict (-> None),
    * an empty / falsy path (-> None).

    The traversal is purely structural — it does NOT apply any group-name
    normalisation (e.g. ``colors`` -> ``color``). Callers that need to be
    tolerant of that normalisation should try both spellings (see BT-2).
    """
    if not dotted_path or not isinstance(tree, dict):
        return None
    node: Any = tree
    for segment in dotted_path.split("."):
        if not isinstance(node, dict) or segment not in node:
            return None
        node = node[segment]
    if isinstance(node, dict) and DTCG_VALUE_KEY in node:
        return node
    return None


def iter_leaves(tree: dict) -> Iterable[tuple[str, dict]]:
    """Yield ``(dotted_path, leaf_dict)`` for every leaf (node with ``$value``).

    A node is a leaf as soon as it carries ``$value``; we do not descend past
    it. ``$``-prefixed metadata keys ($type, $description, $extensions, ...) on
    group nodes are skipped during descent so they are never treated as groups.
    """
    def _walk(node: Any, prefix: str):
        if not isinstance(node, dict):
            return
        if DTCG_VALUE_KEY in node:
            yield prefix, node
            return
        for key, child in node.items():
            if key.startswith("$"):
                continue  # group-level DTCG metadata, not a child group/leaf
            child_path = f"{prefix}.{key}" if prefix else key
            yield from _walk(child, child_path)

    yield from _walk(tree, "")


# --------------------------------------------------------------------------- factory

def make_token(
    name: str,
    value: str,
    *,
    category: str = "other",
    enrichment_match: Optional[str] = None,
    enrichment_type: Optional[str] = None,
    suspected_typo: Optional[str] = None,
    style_id: Optional[str] = None,
) -> dict:
    """Build one ``TokenEntry``-shaped dict. Keyword-only beyond name/value so
    crafted edge cases read clearly at the call site (BT-10..BT-15)."""
    return {
        "name": name,
        "value": value,
        "style_id": style_id,
        "category": category,
        "enrichment_match": enrichment_match,
        "enrichment_type": enrichment_type,
        "suspected_typo": suspected_typo,
    }


def build_extraction(
    token_dicts: Iterable[dict],
    *,
    file_key: str = "SYNTHETIC",
    components: Optional[list[dict]] = None,
    enrichment_coverage: float = 0.0,
    typos_detected: Optional[list[str]] = None,
):
    """Assemble a ``FigmaExtractionResult`` from crafted ``TokenEntry`` dicts.

    Imported lazily because both agent dirs are placed on ``sys.path`` by the
    test module at collection time; importing at module top would be fragile.
    """
    from normalizer import FigmaExtractionResult  # type: ignore  # single import surface

    return FigmaExtractionResult(
        file_key=file_key,
        tokens=list(token_dicts),
        components=components or [],
        extraction_runs=1,
        consensus_confidence=1.0,
        gaps_detected=[],
        typos_detected=typos_detected or [],
        enrichment_coverage=enrichment_coverage,
    )
