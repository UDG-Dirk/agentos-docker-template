"""Adversarial MUTATION tests for the HELIX Token Normalizer (UC2 pipeline Step 2).

PURPOSE
-------
This file is purely ADDITIVE. It complements ``tests/test_token_normalizer.py``
(contract/behavioral/regression) with a focused *mutation-testing* battery: we
take the real ``run_sequential_003`` extraction, ``copy.deepcopy`` it, apply ONE
surgical mutation, re-run ``normalize_tokens`` and assert the normaliser degrades
*gracefully* — no exception, no silent drop, and (critically) no MISCLASSIFICATION
of an out-of-scope value into the wrong DTCG ``$type``.

The Token Normalizer ships seven value parsers (color / dimension / fontFamily /
fontWeight / typography / shadow / number). Real client Figma files inevitably
carry value shapes those parsers do not cover (hsl/oklch colours, calc()
dimensions, gradients, CSS keywords, border shorthands, ...). The architecture's
promise is that such values route to the UNRESOLVED bucket — preserved in the
audit trail, listed in ``report.unresolved_tokens``, and kept OUT of the typed
``token_tree``. These mutations adversarially probe that promise.

Frozen public surface (single import surface — DO NOT ``from models import``):

    from normalizer import normalize_tokens, FigmaExtractionResult, TokenEntry

Each test is named ``test_M{n}_...`` and cites its mutation's verbatim
expectation in its docstring. Every test exercises ``normalize_tokens`` inside a
helper — a raised exception fails the test naturally, which IS the intended
"no exception" assertion (made explicit in comments at each call site).

Conventions: pydantic v2 / Python 3.12 / pytest 9. Tolerant where the spec says
"OR acceptable" (M1, M2) — we assert the *union* of acceptable outcomes and
never overfit.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

# ----------------------------------------------------------------------------- paths
# Mirror the sys.path bootstrap from tests/test_token_normalizer.py (~L67-86):
# put BOTH agent dirs + the tests dir on sys.path so imports resolve under any
# pytest rootdir / invocation. Idempotent.
HELIX_ROOT = Path(__file__).resolve().parents[1]  # helix-poc-agno/
NORMALIZER_DIR = HELIX_ROOT / "agents" / "token_normalizer"
EXTRACTOR_DIR = HELIX_ROOT / "agents" / "figma_extractor"
for _p in (str(NORMALIZER_DIR), str(EXTRACTOR_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# tests/ dir importable for the shared helpers regardless of rootdir/invocation.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from _normalizer_helpers import iter_leaves, resolve_dtcg_path  # noqa: E402

RUN_003 = EXTRACTOR_DIR / "runs" / "run_sequential_003.json"

VENDOR_KEY = "de.msqdx.helix"
DIMENSION_UNITS = {"px", "rem", "em", "%"}
ABS = 1e-3  # float comparison epsilon


# ----------------------------------------------------------------------------- base payload
def _load_base_result() -> dict:
    """Return the raw ``result`` dict from run_sequential_003, or skip the whole
    module if the input run file is missing (per spec: pytest.skip)."""
    if not RUN_003.exists():
        pytest.skip(f"input run not found: {RUN_003}")
    return json.loads(RUN_003.read_text())["result"]


@pytest.fixture(scope="session")
def base_result() -> dict:
    """The real run_003 ``result`` dict (read once). Mutators deep-copy this so a
    single test can never bleed mutations into another."""
    return _load_base_result()


# ----------------------------------------------------------------------------- helpers
def _helix_ext(leaf: dict) -> dict:
    """Return the helix provenance block for a leaf, or {} if absent."""
    return (leaf.get("$extensions") or {}).get(VENDOR_KEY) or {}


def first_token_with(result: dict, predicate: Callable[[dict], bool]) -> dict:
    """Return the FIRST token dict in ``result['tokens']`` matching ``predicate``.

    Raises ``pytest.skip`` (not an error) if the base run no longer contains a
    matching token, so the suite stays meaningful if the fixture data evolves.
    """
    for tok in result.get("tokens", []):
        if predicate(tok):
            return tok
    pytest.skip("base run_003 has no token matching the required predicate")


def _normalize_mutation(
    base_result: dict,
    mutate: Callable[[dict], None],
) -> Any:
    """Deep-copy the base ``result``, apply ONE ``mutate(result)`` in place, build
    a ``FigmaExtractionResult`` and run ``normalize_tokens``.

    The ``normalize_tokens`` call is intentionally NOT wrapped in try/except: if
    the normaliser raises on the mutated input, this helper propagates the
    exception and the calling test FAILS — that is precisely the "assert no
    exception is raised" guarantee every mutation requires. Imports are local so
    a missing implementation yields a clean collection-time error.
    """
    from normalizer import FigmaExtractionResult, normalize_tokens  # type: ignore

    mutated = copy.deepcopy(base_result)
    mutate(mutated)
    extraction = FigmaExtractionResult(**mutated)
    # NO try/except by design — a raised exception here fails the test (no-exception contract).
    return normalize_tokens(extraction)


def _append_token(result: dict, token: dict) -> None:
    """Append a synthetic TokenEntry-shaped dict to ``result['tokens']``.

    We mirror the full TokenEntry key-set the run file uses (name/value/style_id/
    category/enrichment_match/enrichment_type/suspected_typo) so pydantic accepts
    it identically to the real tokens.
    """
    base = {
        "name": token["name"],
        "value": token["value"],
        "style_id": token.get("style_id"),
        "category": token.get("category", "other"),
        "enrichment_match": token.get("enrichment_match"),
        "enrichment_type": token.get("enrichment_type"),
        "suspected_typo": token.get("suspected_typo"),
    }
    result.setdefault("tokens", []).append(base)


def _tree_source_names(tree: dict) -> set:
    """sourceName of every typed leaf actually placed in ``token_tree`` (via the
    helix provenance block — iter_leaves skips $-prefixed keys during descent)."""
    names: set = set()
    for _path, leaf in iter_leaves(tree):
        src = _helix_ext(leaf).get("sourceName")
        if src:
            names.add(src)
    return names


def _entries_for(report, source_name: str) -> list:
    """All audit entries (report.all_entries) for a given source_name."""
    return [e for e in report.all_entries if e.source_name == source_name]


def _leaf_type_for_source(tree: dict, source_name: str) -> Optional[str]:
    """Return the ``$type`` of the typed leaf whose helix.sourceName matches, or
    None if that source produced no leaf in the tree."""
    for _path, leaf in iter_leaves(tree):
        if _helix_ext(leaf).get("sourceName") == source_name:
            return leaf.get("$type")
    return None


def assert_unresolved(result_obj, source_name: str) -> None:
    """Reusable adversarial assertion: ``source_name`` must be UNRESOLVED.

    Checks, per the spec:
      1. an ``all_entries`` audit row for ``source_name`` exists with
         ``confidence == "unresolved"`` (all rows for that source, to be strict),
      2. it is listed in ``report.unresolved_tokens``,
      3. the token is NOT present as a typed leaf in ``token_tree`` (verified via
         iter_leaves + ``$extensions[de.msqdx.helix]["sourceName"]``).
    """
    report = result_obj.normalization_report
    entries = _entries_for(report, source_name)
    assert entries, f"{source_name}: no audit entry in report.all_entries"
    assert all(e.confidence == "unresolved" for e in entries), (
        f"{source_name}: expected unresolved confidence, got "
        f"{[e.confidence for e in entries]}"
    )
    assert any(e.source_name == source_name for e in report.unresolved_tokens), (
        f"{source_name}: not listed in report.unresolved_tokens"
    )
    assert source_name not in _tree_source_names(result_obj.token_tree), (
        f"{source_name}: leaked into token_tree (unresolved tokens must be report-only)"
    )


# ----------------------------------------------------------------------------- predicates
def _is_color(tok: dict) -> bool:
    return tok.get("enrichment_type") == "color"


def _is_dimension(tok: dict) -> bool:
    return tok.get("enrichment_type") == "dimension"


def _is_enriched(tok: dict) -> bool:
    """Both enrichment_type AND enrichment_match present (authoritative candidate)."""
    return bool(tok.get("enrichment_type")) and bool(tok.get("enrichment_match"))


def _is_typography_composite(tok: dict) -> bool:
    """A typography composite carries a CSS 'font-family:' declaration in value."""
    return "font-family:" in str(tok.get("value", "")).lower()


# ========================================================================= MUTATIONS
class TestTokenNormalizerMutations:
    """Ten single-mutation adversarial cases (M1..M10). Each deep-copies the real
    run_003 result, mutates ONE token (or appends one), re-normalises, and asserts
    graceful degradation. No mutation may crash the normaliser."""

    # ------------------------------------------------------------------ M1
    def test_M1_hsl_color_value(self, base_result):
        """M1 — HSL color: a color token's value becomes 'hsl(210, 50%, 60%)'.

        Expectation (TOLERANT): the normaliser may either classify it as a valid
        color leaf OR route it to unresolved — EITHER is acceptable. The hard
        requirement is simply: no exception. We assert the UNION of acceptable
        outcomes and never overfit to one branch.
        """
        target = first_token_with(base_result, _is_color)
        name = target["name"]

        def mutate(result: dict) -> None:
            tok = first_token_with(result, lambda t: t["name"] == name)
            tok["value"] = "hsl(210, 50%, 60%)"

        # No exception: _normalize_mutation does not swallow errors (see helper).
        res = _normalize_mutation(base_result, mutate)

        entries = _entries_for(res.normalization_report, name)
        assert entries, f"{name}: no audit entry produced for the mutated token"
        leaf_type = _leaf_type_for_source(res.token_tree, name)

        accepted_color = leaf_type == "color"  # valid color leaf, OR ...
        accepted_unresolved = any(
            e.confidence == "unresolved" for e in entries
        ) and leaf_type is None  # ... unresolved & not in the tree
        assert accepted_color or accepted_unresolved, (
            f"{name}: M1 expected EITHER a color leaf OR unresolved; "
            f"leaf_type={leaf_type!r}, confidences={[e.confidence for e in entries]}"
        )

    # ------------------------------------------------------------------ M2
    def test_M2_oklch_color_value(self, base_result):
        """M2 — OKLCH color: a color token's value becomes 'oklch(0.7 0.15 210)'.

        Expectation: unresolved (the expected outcome). The critical invariant is
        that it is NOT misclassified as a 'dimension' (oklch's leading float must
        not be mistaken for a dimension). If it lands in the tree at all, its
        $type must not be 'dimension'. No exception.
        """
        target = first_token_with(base_result, _is_color)
        name = target["name"]

        def mutate(result: dict) -> None:
            tok = first_token_with(result, lambda t: t["name"] == name)
            tok["value"] = "oklch(0.7 0.15 210)"

        res = _normalize_mutation(base_result, mutate)  # no exception by design

        leaf_type = _leaf_type_for_source(res.token_tree, name)
        # Hard anti-misclassification guard (holds regardless of tolerance):
        assert leaf_type != "dimension", (
            f"{name}: M2 oklch value was misclassified as a dimension"
        )
        # Expected outcome: unresolved (report-only, not a typed leaf).
        assert_unresolved(res, name)

    # ------------------------------------------------------------------ M3
    def test_M3_calc_dimension_value(self, base_result):
        """M3 — calc() dimension: a dimension token's value becomes
        'calc(100% - 16px)'. Expectation: unresolved, no exception. A calc()
        expression is outside the dimension parser's scope and must route to the
        unresolved bucket rather than half-parse a number+unit out of it.
        """
        target = first_token_with(base_result, _is_dimension)
        name = target["name"]

        def mutate(result: dict) -> None:
            tok = first_token_with(result, lambda t: t["name"] == name)
            tok["value"] = "calc(100% - 16px)"

        res = _normalize_mutation(base_result, mutate)  # no exception by design
        assert_unresolved(res, name)

    # ------------------------------------------------------------------ M4
    def test_M4_gradient_value(self, base_result):
        """M4 — gradient: a color token's value becomes
        'linear-gradient(90deg, #ffffff, #000000)'. Expectation: unresolved and
        NOT a color leaf (the embedded hex codes must not trick the color parser
        into emitting a color). No exception.
        """
        target = first_token_with(base_result, _is_color)
        name = target["name"]

        def mutate(result: dict) -> None:
            tok = first_token_with(result, lambda t: t["name"] == name)
            tok["value"] = "linear-gradient(90deg, #ffffff, #000000)"

        res = _normalize_mutation(base_result, mutate)  # no exception by design

        leaf_type = _leaf_type_for_source(res.token_tree, name)
        assert leaf_type != "color", (
            f"{name}: M4 gradient value was misclassified as a color leaf"
        )
        assert_unresolved(res, name)

    # ------------------------------------------------------------------ M5
    def test_M5_keyword_on_dimension(self, base_result):
        """M5 — keyword on dimension: a dimension token's value becomes the CSS
        keyword 'normal'. Expectation: unresolved, and NOT misclassified as a
        fontFamily (a bare word must not be coerced into a font family). No
        exception.
        """
        target = first_token_with(base_result, _is_dimension)
        name = target["name"]

        def mutate(result: dict) -> None:
            tok = first_token_with(result, lambda t: t["name"] == name)
            tok["value"] = "normal"

        res = _normalize_mutation(base_result, mutate)  # no exception by design

        leaf_type = _leaf_type_for_source(res.token_tree, name)
        assert leaf_type != "fontFamily", (
            f"{name}: M5 keyword 'normal' was misclassified as a fontFamily"
        )
        assert_unresolved(res, name)

    # ------------------------------------------------------------------ M6
    def test_M6_border_shorthand_appended(self, base_result):
        """M6 — border shorthand: APPEND a synthetic
        {--border-card-default: '1px solid #333333', category 'other', no
        enrichment}. Expectation: unresolved — must NOT be partially parsed as a
        dimension by greedily grabbing the leading '1px'. No exception.
        """
        name = "--border-card-default"

        def mutate(result: dict) -> None:
            _append_token(
                result,
                {
                    "name": name,
                    "value": "1px solid #333333",
                    "category": "other",
                    "enrichment_match": None,
                    "enrichment_type": None,
                },
            )

        res = _normalize_mutation(base_result, mutate)  # no exception by design

        leaf_type = _leaf_type_for_source(res.token_tree, name)
        assert leaf_type != "dimension", (
            f"{name}: M6 border shorthand was partially parsed as a dimension"
        )
        assert_unresolved(res, name)

    # ------------------------------------------------------------------ M7
    def test_M7_missing_enrichment_type_keeps_match(self, base_result):
        """M7 — enrichment_type cleared, enrichment_match kept: pick an enriched
        token (type AND match present), set enrichment_type=None but KEEP
        enrichment_match.

        Expectation: confidence is NOT 'authoritative' (the type was the
        authoritative signal); BUT the canonical path is still derived FROM the
        retained enrichment_match. We accept EITHER of two equivalent proofs:
          (a) the audit entry's path_source == 'enrichment', OR
          (b) a leaf resolves at the enrichment slash-path -> dotted form
              (tolerating the known leading 'colors' -> 'color' group rename).
        No exception.
        """
        target = first_token_with(base_result, _is_enriched)
        name = target["name"]
        match = target["enrichment_match"]

        def mutate(result: dict) -> None:
            tok = first_token_with(result, lambda t: t["name"] == name)
            tok["enrichment_type"] = None  # drop the authoritative type signal
            # enrichment_match deliberately left intact

        res = _normalize_mutation(base_result, mutate)  # no exception by design

        entries = _entries_for(res.normalization_report, name)
        assert entries, f"{name}: no audit entry produced"
        # Type signal gone -> must not be authoritative anymore.
        assert all(e.confidence != "authoritative" for e in entries), (
            f"{name}: M7 expected non-authoritative confidence once enrichment_type "
            f"is removed; got {[e.confidence for e in entries]}"
        )

        # Proof (a): any entry whose path came from the enrichment match.
        path_from_enrichment = any(e.path_source == "enrichment" for e in entries)

        # Proof (b): the enrichment match still resolves to a leaf at its dotted path
        # (allowing the colors->color group-name normalisation seen in the suite).
        dotted = str(match).replace("/", ".")
        leaf = resolve_dtcg_path(res.token_tree, dotted)
        if leaf is None and dotted.startswith("colors."):
            leaf = resolve_dtcg_path(res.token_tree, "color." + dotted[len("colors."):])
        path_from_match = leaf is not None

        assert path_from_enrichment or path_from_match, (
            f"{name}: M7 canonical path was not derived from the retained "
            f"enrichment_match ({match!r}); path_sources="
            f"{[e.path_source for e in entries]}, dotted={dotted!r} did not resolve"
        )

    # ------------------------------------------------------------------ M8
    def test_M8_empty_string_enrichment_match(self, base_result):
        """M8 — empty-string enrichment_match: pick an enriched token, set
        enrichment_match='' (empty string, NOT None). The empty string must be
        handled without exception and the PATH must fall back to the CSS-var name
        (the match is treated as absent). Type-confidence stays AUTHORITATIVE — see
        the reconciled note below.
        """
        target = first_token_with(base_result, _is_enriched)
        name = target["name"]

        def mutate(result: dict) -> None:
            tok = first_token_with(result, lambda t: t["name"] == name)
            tok["enrichment_match"] = ""  # empty string, not None

        res = _normalize_mutation(base_result, mutate)  # no exception by design

        entries = _entries_for(res.normalization_report, name)
        assert entries, f"{name}: no audit entry produced"
        for e in entries:
            # Empty match treated as absent: path not sourced from enrichment...
            assert not e.enrichment_path, (
                f"{name}: M8 empty enrichment_match must be treated as absent "
                f"(enrichment_path={e.enrichment_path!r})"
            )
            assert e.path_source != "enrichment", (
                f"{name}: M8 path_source must not be 'enrichment' for an empty match"
            )
        # RECONCILED (bröther Code, re: probe finding M8): per spec step1-spec
        # Phase 1, TYPE-confidence is driven by enrichment_TYPE; enrichment_MATCH
        # drives only the PATH. This mutation empties the match but leaves
        # enrichment_type intact, so the token stays an AUTHORITATIVE `color` — only
        # its path falls back to the css-var name (asserted above). The original
        # "not authoritative" expectation assumed enrichment was removed entirely;
        # that "no enrichment at all" case (type absent -> not authoritative) is
        # covered by BT-14 in the main suite. We pin the spec-compliant behavior.
        # (Surfaced to Desktop in shared-results.)
        assert all(e.confidence == "authoritative" for e in entries), (
            f"{name}: enrichment_type present -> type-confidence stays authoritative; "
            f"got {[e.confidence for e in entries]}"
        )

    # ------------------------------------------------------------------ M9
    def test_M9_typography_unknown_property_ignored(self, base_result):
        """M9 — typography unknown property: pick a typography composite (value
        contains 'font-family:') and set its value to include an unknown
        'font-variant' declaration:
            'font-family: roboto; font-weight: 400; font-size: 14px;
             line-height: 24px; font-variant: small-caps'

        Expectation: the KNOWN fields are parsed (fontFamily roboto, fontWeight
        400, fontSize {14,px}, lineHeight {24,px}); the unknown 'font-variant'
        property is ignored (not surfaced as a typography sub-field); no
        exception. We locate the produced typography leaf by helix.sourceName.
        """
        target = first_token_with(base_result, _is_typography_composite)
        name = target["name"]
        new_value = (
            "font-family: roboto; font-weight: 400; font-size: 14px; "
            "line-height: 24px; font-variant: small-caps"
        )

        def mutate(result: dict) -> None:
            tok = first_token_with(result, lambda t: t["name"] == name)
            tok["value"] = new_value

        res = _normalize_mutation(base_result, mutate)  # no exception by design

        # Find the typography leaf this source produced.
        typo_leaf = None
        for _path, leaf in iter_leaves(res.token_tree):
            if (
                leaf.get("$type") == "typography"
                and _helix_ext(leaf).get("sourceName") == name
            ):
                typo_leaf = leaf
                break
        assert typo_leaf is not None, (
            f"{name}: M9 expected a typography leaf for the mutated composite"
        )
        val = typo_leaf["$value"]
        assert isinstance(val, dict), f"{name}: typography $value not a dict"

        # Known fields parsed correctly.
        fam = val["fontFamily"]
        fam_norm = fam if isinstance(fam, str) else (fam[0] if fam else "")
        assert str(fam_norm).strip().lower() == "roboto", f"{name}: fontFamily != roboto"
        assert int(val["fontWeight"]) == 400, f"{name}: fontWeight != 400"
        assert val["fontSize"]["value"] == pytest.approx(14, abs=ABS), f"{name}: fontSize value"
        assert val["fontSize"]["unit"] == "px", f"{name}: fontSize unit"
        assert val["lineHeight"]["value"] == pytest.approx(24, abs=ABS), f"{name}: lineHeight value"
        assert val["lineHeight"]["unit"] == "px", f"{name}: lineHeight unit"

        # Unknown 'font-variant' must be ignored — never leaks into the typed value.
        assert "fontVariant" not in val, f"{name}: M9 unknown font-variant leaked as fontVariant"
        assert "font-variant" not in val, f"{name}: M9 unknown font-variant leaked verbatim"
        assert "small-caps" not in json.dumps(val), (
            f"{name}: M9 unknown 'small-caps' value leaked into the typography $value"
        )

    # ------------------------------------------------------------------ M10
    def test_M10_duplicate_canonical_path_collision(self, base_result):
        """M10 — duplicate canonical path: APPEND a synthetic token engineered to
        resolve to the SAME canonical path as an existing enriched token (reuse
        its name + enrichment_match + enrichment_type, but a DIFFERENT value).

        Expectation: the second token gets a numeric-suffixed path; a collision is
        logged in ``report.path_collisions``; NO silent overwrite (both source
        names survive as distinct leaves); no exception.
        """
        original = first_token_with(base_result, _is_enriched)
        orig_name = original["name"]
        dup_name = orig_name + "-dup"  # distinct source_name so we can prove no overwrite

        def mutate(result: dict) -> None:
            # Re-fetch the original from the COPY to read its (copied) fields.
            src = first_token_with(result, lambda t: t["name"] == orig_name)
            _append_token(
                result,
                {
                    "name": dup_name,
                    # Different value, but same enrichment match/type -> same canonical path.
                    "value": "#abcdef" if src.get("enrichment_type") == "color" else src["value"],
                    "category": src.get("category", "other"),
                    "enrichment_match": src.get("enrichment_match"),
                    "enrichment_type": src.get("enrichment_type"),
                },
            )

        res = _normalize_mutation(base_result, mutate)  # no exception by design
        report = res.normalization_report

        # A collision must be logged (the two tokens contend for one canonical path).
        assert report.path_collisions, (
            "M10 expected a logged path_collision for the duplicate canonical path"
        )
        # The collision should reference the duplicate's resolution (suffixed path).
        collided = [
            c for c in report.path_collisions
            if c.source_name in {orig_name, dup_name}
        ]
        assert collided, (
            f"M10 path_collisions logged but none reference {orig_name!r}/{dup_name!r}: "
            f"{[(c.source_name, c.attempted_path, c.resolved_path) for c in report.path_collisions]}"
        )
        # No silent overwrite: BOTH source names survive as distinct leaves.
        tree_sources = _tree_source_names(res.token_tree)
        assert {orig_name, dup_name} <= tree_sources, (
            f"M10 a colliding token was dropped instead of suffixed; "
            f"tree sources missing one of {{{orig_name!r}, {dup_name!r}}}"
        )
        # And the suffixed resolved_path must differ from the attempted path (real disambiguation).
        for c in collided:
            if c.source_name == dup_name:
                assert c.resolved_path != c.attempted_path, (
                    f"M10 duplicate {dup_name!r} resolved_path equals attempted_path "
                    f"({c.resolved_path!r}) — no numeric-suffix disambiguation applied"
                )
