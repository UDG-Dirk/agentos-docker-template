"""Token Normalizer test harness — HELIX UC2 pipeline Step 2.

Public API under test (frozen):

    from normalizer import normalize_tokens          # agents/token_normalizer/
    result = normalize_tokens(extraction)            # FigmaExtractionResult
    # -> models.NormalizedTokens

Test-ID -> criteria map
-----------------------
SMOKE
  ST-1  normalize_tokens(run_003) -> NormalizedTokens, no exception
  ST-2  token_tree is a non-empty dict
  ST-3  report.total_input_tokens > 0
  ST-4  normalizer_version is set (non-empty)
  ST-5  determinism: run twice -> equal .model_dump()
CONTRACT
  CT-1  json.dumps(token_tree) succeeds
  CT-2  every leaf has both $value and $type
  CT-3  every $type in the DTCG enum
  CT-4  every leaf has $extensions[de.msqdx.helix] w/ confidence/sourceName/normalizerVersion
  CT-5  helix.confidence in {authoritative,high,medium,unresolved}
  CT-6  color leaves: colorSpace, components[3] in 0..1 (+-1e-3), optional hex/alpha
  CT-7  dimension leaves: numeric value + unit in {px,rem,em,%}
  CT-8  typography leaves: fontFamily, fontWeight, fontSize(dim), lineHeight(dim)
  CT-9  shadow leaves: dict|list[dict], each color/offsetX/offsetY/blur/spread
  CT-10 report.total_input_tokens == len(input tokens)
  CT-11 len(report.all_entries) == report.total_output_tokens
  CT-12 authoritative+high+medium+unresolved == total_output_tokens
  CT-13 set(component_token_map keys) == set(input component names)
  CT-14 every component_token_map path resolves via resolve_dtcg_path
  CT-15 no leaf helix ext carries originalValue / suspectedTypo
BEHAVIORAL
  BT-1  enrichment_type tokens -> authoritative + mapped $type (count == 74)
  BT-2  enrichment_match -> canonical dotted path (colors->color tolerated)
  BT-3  no enrichment_match -> css-var-derived path, enrichmentPath null/absent
  BT-4  semantic aliases preserved (value #1971c2 -> 6 leaves)
  BT-5  5 typography composites decomposed (PARAMETRIZED)
  BT-6  2 shadow composites parsed, parenthesis-aware (PARAMETRIZED)
  BT-7  extractor typos in report.typos_flagged; no leaf suspectedTypo
  BT-8  borderRadius -> $type dimension + helix.originalCategory == "borderRadius"
  BT-9  $type inheritance consistency invariant (leaf or ancestor, no contradiction)
  BT-10 synthetic unresolved value preserved + listed, not dropped
  BT-11 synthetic unknown enrichment_type -> fallback warning, not authoritative
  BT-12 synthetic invalid hex / junk -> unresolved, raw preserved, no exception
  BT-13 synthetic path collision -> numeric suffix + logged
  BT-14 synthetic empty enrichment_match treated as absent
  BT-15 empty input -> {} tree, 0 input tokens, no exception
  BT-16 "other"-category + enrichment_type -> mapped $type (never literal "other")
  BT-17 fontFamily "roboto"/"dm sans" valid; "dm sans" not split on space
REGRESSION
  RT-1  golden snapshot (skip if unblessed)

Conventions: pydantic v2 / Python 3.12 / pytest 9. Float compare +-1e-3.
Known run_sequential_003 numbers (74 enrichment tokens, 6x #1971c2, ...) are
explicit regression asserts and flagged with a comment; generic rules are
parametrized/derived from the input rather than hardcoded.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# ----------------------------------------------------------------------------- paths
# Make imports robust: put BOTH agent dirs on sys.path (idempotent). normalizer
# lives in token_normalizer/; FigmaExtractionResult lives in figma_extractor/.
HELIX_ROOT = Path(__file__).resolve().parents[1]  # helix-poc-agno/
NORMALIZER_DIR = HELIX_ROOT / "agents" / "token_normalizer"
EXTRACTOR_DIR = HELIX_ROOT / "agents" / "figma_extractor"
for _p in (str(NORMALIZER_DIR), str(EXTRACTOR_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# tests/ dir importable for the shared helpers regardless of rootdir/invocation.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from _normalizer_helpers import (  # noqa: E402
    build_extraction,
    iter_leaves,
    make_token,
    resolve_dtcg_path,
)

RUN_003 = EXTRACTOR_DIR / "runs" / "run_sequential_003.json"
GOLDEN = NORMALIZER_DIR / "runs" / ".golden.json"

VENDOR_KEY = "de.msqdx.helix"
DTCG_TYPES = {"color", "dimension", "fontFamily", "fontWeight", "number", "typography", "shadow"}
CONFIDENCES = {"authoritative", "high", "medium", "unresolved"}
DIMENSION_UNITS = {"px", "rem", "em", "%"}

# enrichment_type -> DTCG $type (BT-1 / BT-8 / BT-16). borderRadius collapses to
# dimension; fontStyle is the extractor's label for composite typography; effect
# is a shadow.
TYPE_MAP = {
    "color": "color",
    "dimension": "dimension",
    "fontFamily": "fontFamily",
    "fontWeight": "fontWeight",
    "borderRadius": "dimension",
    "fontStyle": "typography",
    "effect": "shadow",
}

ABS = 1e-3  # float comparison epsilon


# ----------------------------------------------------------------------------- fixtures
@pytest.fixture(scope="session")
def normalize():
    """The frozen public entry point. Imported lazily so collection errors are
    clean if the implementation is missing."""
    from normalizer import normalize_tokens  # type: ignore

    return normalize_tokens


@pytest.fixture(scope="session")
def run003_extraction():
    """FigmaExtractionResult built from the real run_sequential_003 payload."""
    if not RUN_003.exists():
        pytest.skip(f"input run not found: {RUN_003}")
    from normalizer import FigmaExtractionResult  # type: ignore  # single import surface

    payload = json.loads(RUN_003.read_text())
    return FigmaExtractionResult(**payload["result"])


@pytest.fixture(scope="session")
def result(normalize, run003_extraction):
    """normalize_tokens(run_003) computed ONCE per session and reused."""
    return normalize(run003_extraction)


@pytest.fixture(scope="session")
def tree(result) -> dict:
    return result.token_tree


@pytest.fixture(scope="session")
def report(result):
    return result.normalization_report


@pytest.fixture(scope="session")
def leaves(tree) -> list[tuple[str, dict]]:
    """All (dotted_path, leaf) pairs in the produced tree."""
    return list(iter_leaves(tree))


# ----------------------------------------------------------------------------- helpers
def _helix_ext(leaf: dict) -> dict:
    """Return the helix provenance block for a leaf, or {} if absent."""
    return (leaf.get("$extensions") or {}).get(VENDOR_KEY) or {}


def _collect_ancestor_types(tree: dict) -> dict:
    """Map dotted leaf path -> list of $type values declared on its ancestor
    GROUP nodes (root..parent). Used by BT-9 to verify no leaf $type contradicts
    an inherited group $type."""
    ancestor_types: dict[str, list[str]] = {}

    def _walk(node, prefix, inherited):
        if not isinstance(node, dict):
            return
        if "$value" in node:
            ancestor_types[prefix] = list(inherited)
            return
        own = node.get("$type")
        next_inherited = inherited + [own] if own else inherited
        for key, child in node.items():
            if key.startswith("$"):
                continue
            child_path = f"{prefix}.{key}" if prefix else key
            _walk(child, child_path, next_inherited)

    _walk(tree, "", [])
    return ancestor_types


def _norm_value(v: str) -> str:
    """BT-4 value normalisation: lowercase + strip."""
    return str(v).strip().lower()


# ========================================================================= SMOKE (ST)
class TestSmoke:
    def test_ST1_returns_normalized_tokens(self, result):
        """ST-1: call succeeds and yields a NormalizedTokens instance."""
        from normalizer import NormalizedTokens  # type: ignore  # single import surface

        assert isinstance(result, NormalizedTokens)

    def test_ST2_token_tree_nonempty_dict(self, tree):
        """ST-2."""
        assert isinstance(tree, dict) and len(tree) > 0

    def test_ST3_total_input_tokens_positive(self, report):
        """ST-3."""
        assert report.total_input_tokens > 0

    def test_ST4_normalizer_version_set(self, result):
        """ST-4."""
        assert result.normalizer_version

    def test_ST5_determinism(self, normalize, run003_extraction):
        """ST-5: identical input -> identical output (compare model_dump)."""
        a = normalize(run003_extraction).model_dump()
        b = normalize(run003_extraction).model_dump()
        assert a == b


# ====================================================================== CONTRACT (CT)
class TestContract:
    def test_CT1_token_tree_json_serialisable(self, tree):
        """CT-1: tree must serialise to .tokens.json without error."""
        json.dumps(tree)

    def test_CT2_every_leaf_has_value_and_type(self, leaves):
        """CT-2: leaf == node with $value; must also carry $type."""
        assert leaves, "no leaves produced"
        for path, leaf in leaves:
            assert "$value" in leaf, f"{path}: missing $value"
            assert "$type" in leaf, f"{path}: leaf missing $type"

    def test_CT3_every_type_in_enum(self, leaves):
        """CT-3."""
        for path, leaf in leaves:
            assert leaf["$type"] in DTCG_TYPES, f"{path}: bad $type {leaf['$type']!r}"

    def test_CT4_every_leaf_has_helix_provenance(self, leaves):
        """CT-4: $extensions[de.msqdx.helix] with confidence/sourceName/normalizerVersion."""
        for path, leaf in leaves:
            ext = _helix_ext(leaf)
            assert ext, f"{path}: missing $extensions[{VENDOR_KEY}]"
            for key in ("confidence", "sourceName", "normalizerVersion"):
                assert ext.get(key), f"{path}: helix.{key} missing/empty"

    def test_CT5_confidence_enum(self, leaves):
        """CT-5."""
        for path, leaf in leaves:
            conf = _helix_ext(leaf).get("confidence")
            assert conf in CONFIDENCES, f"{path}: bad confidence {conf!r}"

    def test_CT6_color_value_shape(self, leaves):
        """CT-6: color leaves -> colorSpace, components[3] in 0..1 (+-1e-3),
        optional hex / alpha(0..1)."""
        colors = [(p, lf) for p, lf in leaves if lf["$type"] == "color"]
        assert colors, "expected at least one color leaf in run_003"
        for path, leaf in colors:
            val = leaf["$value"]
            assert isinstance(val, dict), f"{path}: color $value not a dict"
            assert "colorSpace" in val, f"{path}: color missing colorSpace"
            comps = val.get("components")
            assert isinstance(comps, list) and len(comps) == 3, f"{path}: components not list[3]"
            for c in comps:
                assert isinstance(c, (int, float)), f"{path}: component not numeric: {c!r}"
                assert -ABS <= c <= 1.0 + ABS, f"{path}: component {c} outside 0..1"
            if "alpha" in val and val["alpha"] is not None:
                assert -ABS <= val["alpha"] <= 1.0 + ABS, f"{path}: alpha out of range"
            if "hex" in val and val["hex"] is not None:
                assert isinstance(val["hex"], str), f"{path}: hex not a string"

    def test_CT7_dimension_value_shape(self, leaves):
        """CT-7: dimension leaves -> numeric value + unit in {px,rem,em,%}."""
        dims = [(p, lf) for p, lf in leaves if lf["$type"] == "dimension"]
        assert dims, "expected at least one dimension leaf in run_003"
        for path, leaf in dims:
            val = leaf["$value"]
            assert isinstance(val, dict), f"{path}: dimension $value not a dict"
            assert isinstance(val.get("value"), (int, float)), f"{path}: value not numeric"
            assert val.get("unit") in DIMENSION_UNITS, f"{path}: bad unit {val.get('unit')!r}"

    def test_CT8_typography_value_shape(self, leaves):
        """CT-8: typography leaves -> fontFamily, fontWeight, fontSize(dim), lineHeight(dim)."""
        typos = [(p, lf) for p, lf in leaves if lf["$type"] == "typography"]
        assert typos, "expected at least one typography leaf in run_003"
        for path, leaf in typos:
            val = leaf["$value"]
            assert isinstance(val, dict), f"{path}: typography $value not a dict"
            assert "fontFamily" in val, f"{path}: missing fontFamily"
            assert "fontWeight" in val, f"{path}: missing fontWeight"
            for sub in ("fontSize", "lineHeight"):
                d = val.get(sub)
                assert isinstance(d, dict) and "value" in d and d.get("unit") in DIMENSION_UNITS, (
                    f"{path}: {sub} not a dimension dict"
                )

    def test_CT9_shadow_value_shape(self, leaves):
        """CT-9: shadow leaves -> dict OR list[dict], each w/ color/offsetX/offsetY/blur/spread."""
        shadows = [(p, lf) for p, lf in leaves if lf["$type"] == "shadow"]
        assert shadows, "expected at least one shadow leaf in run_003"
        for path, leaf in shadows:
            val = leaf["$value"]
            layers = val if isinstance(val, list) else [val]
            assert layers, f"{path}: empty shadow value"
            for layer in layers:
                assert isinstance(layer, dict), f"{path}: shadow layer not a dict"
                for key in ("color", "offsetX", "offsetY", "blur", "spread"):
                    assert key in layer, f"{path}: shadow layer missing {key}"

    def test_CT10_total_input_tokens_matches(self, report, run003_extraction):
        """CT-10."""
        assert report.total_input_tokens == len(run003_extraction.tokens)

    def test_CT11_all_entries_matches_output_count(self, report):
        """CT-11."""
        assert len(report.all_entries) == report.total_output_tokens

    def test_CT12_confidence_counts_sum(self, report):
        """CT-12: four confidence buckets sum to total_output_tokens."""
        total = (
            report.authoritative_count
            + report.high_confidence_count
            + report.medium_confidence_count
            + report.unresolved_count
        )
        assert total == report.total_output_tokens

    def test_CT13_component_map_keys_match_input(self, result, run003_extraction):
        """CT-13."""
        expected = {c.name for c in run003_extraction.components}
        assert set(result.component_token_map.keys()) == expected

    def test_CT14_component_map_paths_resolve(self, result, tree):
        """CT-14: every path in component_token_map values resolves to a leaf.
        Tolerate the single known colors->color group-name normalisation."""
        for comp, paths in result.component_token_map.items():
            for path in paths:
                leaf = resolve_dtcg_path(tree, path)
                if leaf is None and path.startswith("colors."):
                    leaf = resolve_dtcg_path(tree, "color." + path[len("colors."):])
                assert leaf is not None, f"{comp}: path {path!r} does not resolve to a leaf"

    def test_CT15_no_qa_fields_in_leaf_extensions(self, leaves):
        """CT-15: originalValue / suspectedTypo are report-only, never on a leaf."""
        for path, leaf in leaves:
            ext = _helix_ext(leaf)
            assert "originalValue" not in ext, f"{path}: leaked originalValue"
            assert "suspectedTypo" not in ext, f"{path}: leaked suspectedTypo"


# ==================================================================== BEHAVIORAL (BT)
class TestBehavioral:
    def test_BT1_enrichment_type_authoritative_and_mapped(self, result, run003_extraction):
        """BT-1: each token with enrichment_type -> authoritative + mapped $type.
        Drive off report.all_entries (canonical_path keyed) and the tree.
        Regression: exactly 74 such tokens in run_003."""
        et_tokens = [t for t in run003_extraction.tokens if t.enrichment_type]
        assert len(et_tokens) == 74  # REGRESSION (run_sequential_003): 74 enriched tokens

        entries = result.normalization_report.all_entries
        by_source: dict[str, list] = {}
        for e in entries:
            by_source.setdefault(e.source_name, []).append(e)

        for tok in et_tokens:
            expected_type = TYPE_MAP[tok.enrichment_type]
            matches = by_source.get(tok.name, [])
            assert matches, f"{tok.name}: no audit entry produced"
            # at least one produced entry for this source must be authoritative + mapped type
            assert any(
                e.confidence == "authoritative" and e.dtcg_type == expected_type for e in matches
            ), (
                f"{tok.name} (enrichment_type={tok.enrichment_type}): expected authoritative "
                f"{expected_type}; got {[(e.confidence, e.dtcg_type) for e in matches]}"
            )

    def test_BT2_enrichment_match_canonical_path(self, tree):
        """BT-2: enrichment_match slash-path -> dotted path; tolerate colors->color.
        Sample: 'colors/control/background/default'."""
        enrichment_path = "colors/control/background/default"
        dotted = enrichment_path.replace("/", ".")
        leaf = resolve_dtcg_path(tree, dotted)
        if leaf is None:
            # known group-name normalisation: leading 'colors' -> 'color'
            leaf = resolve_dtcg_path(tree, "color." + dotted[len("colors."):])
        assert leaf is not None, (
            f"enrichment path {dotted!r} (or colors->color variant) did not resolve"
        )
        assert leaf["$type"] == "color"

    def test_BT3_no_enrichment_path_from_cssvar(self, result, tree):
        """BT-3: '--layout-width-desktop' (1536px, no enrichment_match) -> a
        dimension leaf derived from the css-var name; enrichmentPath null/absent."""
        source = "--layout-width-desktop"
        entry = next(
            (e for e in result.normalization_report.all_entries if e.source_name == source), None
        )
        assert entry is not None, f"{source}: no audit entry"
        # path came from the css-var name, not enrichment (impl may label it
        # 'css-var-parsed' or 'name-convention' — accept either, but never 'enrichment')
        assert entry.path_source != "enrichment", f"{source}: path_source must not be enrichment"
        assert not entry.enrichment_path, f"{source}: enrichment_path should be empty"
        leaf = resolve_dtcg_path(tree, entry.canonical_path)
        assert leaf is not None, f"{source}: leaf {entry.canonical_path!r} not found"
        assert leaf["$type"] == "dimension"
        ext = _helix_ext(leaf)
        assert not ext.get("enrichmentPath"), f"{source}: leaf enrichmentPath should be null/absent"

    def test_BT4_semantic_aliases_preserved(self, leaves, run003_extraction):
        """BT-4: aliases are kept, not deduped. For every hex value V appearing on
        N color input tokens, exactly N color leaves carry V (values normalised
        lowercase+strip). Asserted as a GENERIC rule over all hex values, plus the
        explicit #1971c2 -> 6 regression. We compare on the leaf color hex, the
        stable normalisable representation."""
        from collections import Counter

        def _leaf_hex(leaf):
            v = leaf.get("$value")
            if isinstance(v, dict) and v.get("hex"):
                return _norm_value(v["hex"])
            return None

        # input hex counts: only #rrggbb-style color values (the alias domain)
        input_counts = Counter(
            _norm_value(t.value)
            for t in run003_extraction.tokens
            if t.value.strip().startswith("#")
        )
        leaf_counts = Counter(
            h for _p, lf in leaves if (h := _leaf_hex(lf)) is not None
        )
        # Generic invariant: every input hex appears on exactly as many leaves.
        for hexval, n_in in input_counts.items():
            assert leaf_counts.get(hexval, 0) == n_in, (
                f"alias value {hexval}: {n_in} input tokens but "
                f"{leaf_counts.get(hexval, 0)} leaves carry it"
            )

        # REGRESSION (run_sequential_003): value #1971c2 appears on exactly 6 tokens.
        assert input_counts[_norm_value("#1971c2")] == 6
        assert leaf_counts[_norm_value("#1971c2")] == 6

    # BT-5 — typography composites. fields: family/weight/fontSize px/lineHeight/letterSpacing
    TYPO_CASES = [
        ("body-sm-regular", "roboto", 400, 14, (24, "px"), (0, "px")),
        ("body-sm-strong", "dm sans", 900, 14, (24, "px"), (0, "px")),
        ("body-md-regular", "roboto", 400, 16, (24, "px"), (0, "px")),
        ("link-md-regular", "roboto", 600, 16, (24, "px"), (0, "px")),
        # heading-xs-strong: mixed-unit lineHeight {1.4, em}, NO letterSpacing
        ("heading-xs-strong", "roboto", 900, 20, (1.4, "em"), None),
    ]

    @pytest.mark.parametrize("slug,family,weight,size_px,line,letter", TYPO_CASES)
    def test_BT5_typography_decomposed(self, leaves, slug, family, weight, size_px, line, letter):
        """BT-5 (PARAMETRIZED): each of the 5 typography composites decomposes
        into the expected DTCG sub-fields."""
        token_name = f"--typography-{slug}"
        # find the typography leaf whose helix.sourceName matches the css-var
        candidates = [
            lf for _p, lf in leaves
            if lf["$type"] == "typography" and _helix_ext(lf).get("sourceName") == token_name
        ]
        assert candidates, f"no typography leaf for {token_name}"
        val = candidates[0]["$value"]

        fam = val["fontFamily"]
        fam_norm = fam if isinstance(fam, str) else (fam[0] if fam else "")
        assert _norm_value(fam_norm) == family, f"{slug}: fontFamily {fam!r} != {family!r}"
        # BT-17 reinforcement: 'dm sans' kept intact (not split on space)
        if family == "dm sans":
            assert _norm_value(fam_norm) == "dm sans"

        assert int(val["fontWeight"]) == weight, f"{slug}: fontWeight"
        assert val["fontSize"]["value"] == pytest.approx(size_px, abs=ABS), f"{slug}: fontSize"
        assert val["fontSize"]["unit"] == "px", f"{slug}: fontSize unit"

        ln_val, ln_unit = line
        assert val["lineHeight"]["value"] == pytest.approx(ln_val, abs=ABS), f"{slug}: lineHeight"
        assert val["lineHeight"]["unit"] == ln_unit, f"{slug}: lineHeight unit ({ln_unit})"

        if letter is None:
            # heading-xs-strong has NO letter-spacing in the source
            ls = val.get("letterSpacing")
            assert ls is None, f"{slug}: letterSpacing should be absent, got {ls!r}"
        else:
            ls_val, ls_unit = letter
            ls = val.get("letterSpacing")
            assert ls is not None, f"{slug}: letterSpacing expected"
            assert ls["value"] == pytest.approx(ls_val, abs=ABS), f"{slug}: letterSpacing value"
            assert ls["unit"] == ls_unit, f"{slug}: letterSpacing unit"

    # BT-6 — shadow composites (parenthesis-aware rgba parsing).
    SHADOW_CASES = [
        # name, expected_layer_count, list of (components, alpha, spread_px)
        (
            "--effect-focus-ring",
            2,
            [
                ([0.827, 0.502, 0.043], 1.0, 4),  # rgba(211,128,11,1)
                ([1.0, 1.0, 1.0], 1.0, 2),        # rgba(255,255,255,1)
            ],
        ),
        (
            "--effect-error-ring",
            1,
            [
                ([1.0, 0.788, 0.788], 1.0, 4),    # rgba(255,201,201,1)
            ],
        ),
    ]

    @pytest.mark.parametrize("name,n_layers,layers_spec", SHADOW_CASES)
    def test_BT6_shadow_decomposed(self, leaves, name, n_layers, layers_spec):
        """BT-6 (PARAMETRIZED): both shadow composites parsed correctly,
        parenthesis-aware (commas inside rgba() must not split layers).
        offsets+blur are {0,px}; spread per layer; error-ring may be dict or list[1]."""
        cands = [lf for _p, lf in leaves if _helix_ext(lf).get("sourceName") == name]
        assert cands, f"no shadow leaf for {name}"
        leaf = cands[0]
        assert leaf["$type"] == "shadow"
        val = leaf["$value"]
        layers = val if isinstance(val, list) else [val]
        assert len(layers) == n_layers, f"{name}: expected {n_layers} layer(s), got {len(layers)}"

        for layer, (components, alpha, spread_px) in zip(layers, layers_spec):
            comps = layer["color"]["components"]
            assert len(comps) == 3
            for got, exp in zip(comps, components):
                assert got == pytest.approx(exp, abs=ABS), f"{name}: color comp {got} != {exp}"
            if "alpha" in layer["color"] and layer["color"]["alpha"] is not None:
                assert layer["color"]["alpha"] == pytest.approx(alpha, abs=ABS), f"{name}: alpha"
            # offsets + blur are 0px
            for key in ("offsetX", "offsetY", "blur"):
                assert layer[key]["value"] == pytest.approx(0, abs=ABS), f"{name}: {key} != 0"
                assert layer[key]["unit"] == "px"
            assert layer["spread"]["value"] == pytest.approx(spread_px, abs=ABS), f"{name}: spread"
            assert layer["spread"]["unit"] == "px"

    def test_BT7_typos_flagged_not_on_leaves(self, result, run003_extraction, leaves):
        """BT-7: extractor typos surface in report.typos_flagged; no leaf carries
        a suspectedTypo extension."""
        flagged = result.normalization_report.typos_flagged
        assert isinstance(flagged, list)
        # the extractor recorded suspected_typo on >=1 token; expect normaliser to flag
        typo_tokens = [t for t in run003_extraction.tokens if t.suspected_typo]
        if typo_tokens:
            assert flagged, "extractor reported typos but normaliser flagged none"
        for path, leaf in leaves:
            assert "suspectedTypo" not in _helix_ext(leaf), f"{path}: leaf carries suspectedTypo"

    def test_BT8_borderradius_is_dimension_with_origin(self, result, run003_extraction, tree):
        """BT-8: borderRadius tokens -> $type dimension AND helix.originalCategory
        == 'borderRadius'."""
        br_tokens = [t for t in run003_extraction.tokens if t.enrichment_type == "borderRadius"]
        assert br_tokens, "expected borderRadius tokens in run_003"
        by_source = {e.source_name: e for e in result.normalization_report.all_entries}
        for tok in br_tokens:
            entry = by_source.get(tok.name)
            assert entry is not None, f"{tok.name}: no audit entry"
            assert entry.dtcg_type == "dimension", f"{tok.name}: not dimension"
            leaf = resolve_dtcg_path(tree, entry.canonical_path)
            assert leaf is not None, f"{tok.name}: leaf missing"
            assert leaf["$type"] == "dimension"
            assert _helix_ext(leaf).get("originalCategory") == "borderRadius", (
                f"{tok.name}: helix.originalCategory != borderRadius"
            )

    def test_BT9_type_inheritance_consistency(self, tree, leaves):
        """BT-9: robust invariant — every leaf has a resolvable $type (leaf-explicit
        or inherited from an ancestor group's $type) and no leaf $type contradicts
        an ancestor group $type. This tolerates either impl choice."""
        ancestor_types = _collect_ancestor_types(tree)
        for path, leaf in leaves:
            leaf_type = leaf.get("$type")
            inherited = ancestor_types.get(path, [])
            # resolvable: either leaf-explicit or inheritable
            assert leaf_type or inherited, f"{path}: no resolvable $type (leaf or ancestor)"
            # non-contradiction: if both present, the nearest ancestor $type must match
            if leaf_type and inherited:
                nearest = inherited[-1]
                assert leaf_type == nearest, (
                    f"{path}: leaf $type {leaf_type!r} contradicts ancestor $type {nearest!r}"
                )

    # ---------------------------------------------------------------- synthetic edge cases
    def test_BT10_unresolved_value_preserved(self, normalize):
        """BT-10: junk value, no enrichment -> confidence unresolved, raw $value
        preserved, listed in report.unresolved_tokens, not dropped, no exception."""
        raw = "gradient(something-weird)"
        ext = build_extraction([make_token("--mystery-thing", raw)])
        res = normalize(ext)
        assert res.normalization_report.total_input_tokens == 1
        # the token must not be silently dropped
        unresolved = res.normalization_report.unresolved_tokens
        assert any(e.source_name == "--mystery-thing" for e in unresolved), (
            "unresolved token not listed in report.unresolved_tokens"
        )
        # confidence unresolved somewhere in the audit trail
        entries = [e for e in res.normalization_report.all_entries if e.source_name == "--mystery-thing"]
        assert entries, "no audit entry for unresolved token"
        assert any(e.confidence == "unresolved" for e in entries)
        # raw value preserved (in the leaf $value or the audit original_value)
        leaves_ = list(iter_leaves(res.token_tree))
        leaf_raw = any(lf["$value"] == raw for _p, lf in leaves_)
        entry_raw = any(e.original_value == raw for e in entries)
        assert leaf_raw or entry_raw, "raw value not preserved on leaf or in audit trail"

    def test_BT11_unknown_enrichment_type_fallback(self, normalize):
        """BT-11: enrichment_type 'opacity' (not in mapping) -> fallback warning,
        confidence NOT authoritative."""
        ext = build_extraction(
            [make_token("--surface-opacity-muted", "0.5", enrichment_type="opacity")]
        )
        res = normalize(ext)
        assert res.normalization_report.fallback_warnings, "no fallback_warnings for unknown type"
        entries = [
            e for e in res.normalization_report.all_entries
            if e.source_name == "--surface-opacity-muted"
        ]
        assert entries, "no audit entry"
        assert all(e.confidence != "authoritative" for e in entries), (
            "unknown enrichment_type must not yield authoritative confidence"
        )

    def test_BT12_invalid_values_unresolved(self, normalize):
        """BT-12: invalid hex '#GGG' and junk 'not-a-value-at-all' -> unresolved,
        raw value preserved, no exception."""
        toks = [
            make_token("--color-broken", "#GGG"),
            make_token("--thing-nonsense", "not-a-value-at-all"),
        ]
        res = normalize(build_extraction(toks))
        assert res.normalization_report.total_input_tokens == 2
        for name, raw in (("--color-broken", "#GGG"), ("--thing-nonsense", "not-a-value-at-all")):
            entries = [e for e in res.normalization_report.all_entries if e.source_name == name]
            assert entries, f"{name}: no audit entry"
            assert any(e.confidence == "unresolved" for e in entries), f"{name}: not unresolved"
            leaves_ = list(iter_leaves(res.token_tree))
            leaf_raw = any(lf["$value"] == raw for _p, lf in leaves_)
            entry_raw = any(e.original_value == raw for e in entries)
            assert leaf_raw or entry_raw, f"{name}: raw value not preserved"

    def test_BT13_path_collision_suffixed_and_logged(self, normalize):
        """BT-13: two tokens deriving the SAME canonical path -> second is numeric-
        suffixed and the collision is logged in report.path_collisions."""
        toks = [
            make_token("--color-brand-primary", "#111111", enrichment_match="color/brand/primary",
                       enrichment_type="color"),
            make_token("--brand-primary-color", "#222222", enrichment_match="color/brand/primary",
                       enrichment_type="color"),
        ]
        res = normalize(build_extraction(toks))
        collisions = res.normalization_report.path_collisions
        assert collisions, "no path_collisions logged for duplicate canonical path"
        # both tokens must still produce distinct leaves (none dropped)
        leaves_ = list(iter_leaves(res.token_tree))
        sources = {_helix_ext(lf).get("sourceName") for _p, lf in leaves_}
        assert {"--color-brand-primary", "--brand-primary-color"} <= sources, (
            "a colliding token was dropped instead of suffixed"
        )

    def test_BT14_empty_enrichment_match_treated_as_absent(self, normalize):
        """BT-14: enrichment_match '' (empty) -> path derived from css-var,
        confidence NOT authoritative."""
        ext = build_extraction(
            [make_token("--spacing-gap-md", "16px", enrichment_match="", category="spacing")]
        )
        res = normalize(ext)
        entries = [e for e in res.normalization_report.all_entries if e.source_name == "--spacing-gap-md"]
        assert entries, "no audit entry"
        e = entries[0]
        assert not e.enrichment_path, "empty enrichment_match must be treated as absent"
        assert e.confidence != "authoritative", "empty enrichment_match must not be authoritative"

    def test_BT15_empty_input(self, normalize):
        """BT-15: empty token list -> {} tree, 0 input tokens, no exception."""
        res = normalize(build_extraction([]))
        assert res.token_tree == {}
        assert res.normalization_report.total_input_tokens == 0

    def test_BT16_other_category_uses_mapped_type(self, result, run003_extraction, tree):
        """BT-16: 'other'-category tokens that carry enrichment_type get the mapped
        $type (never a literal 'other' type)."""
        other_typed = [
            t for t in run003_extraction.tokens
            if t.category == "other" and t.enrichment_type
        ]
        assert other_typed, "expected 'other'-category tokens with enrichment_type (e.g. borderRadius)"
        by_source = {e.source_name: e for e in result.normalization_report.all_entries}
        for tok in other_typed:
            entry = by_source.get(tok.name)
            assert entry is not None, f"{tok.name}: no audit entry"
            expected = TYPE_MAP[tok.enrichment_type]
            assert entry.dtcg_type == expected, f"{tok.name}: {entry.dtcg_type!r} != {expected!r}"
            assert entry.dtcg_type != "other"

    def test_BT17_fontfamily_values_and_no_space_split(self, normalize, leaves):
        """BT-17: 'roboto' and 'dm sans' both produce valid family values;
        'dm sans' is NOT split on the space (synthetic + real reinforcement)."""
        toks = [
            make_token("--font-family-a", "roboto", enrichment_match="font/family/a",
                       enrichment_type="fontFamily"),
            make_token("--font-family-b", "dm sans", enrichment_match="font/family/b",
                       enrichment_type="fontFamily"),
        ]
        res = normalize(build_extraction(toks))
        fam_leaves = {
            _helix_ext(lf).get("sourceName"): lf["$value"]
            for _p, lf in iter_leaves(res.token_tree)
            if lf["$type"] == "fontFamily"
        }
        assert "--font-family-a" in fam_leaves and "--font-family-b" in fam_leaves
        a = fam_leaves["--font-family-a"]["value"]
        b = fam_leaves["--font-family-b"]["value"]
        a_norm = a if isinstance(a, str) else " ".join(a)
        b_norm = b if isinstance(b, str) else (b[0] if len(b) == 1 else " ".join(b))
        assert _norm_value(a_norm) == "roboto"
        # 'dm sans' must remain a single family — either a str "dm sans" or a
        # single-element list ["dm sans"], NEVER ["dm", "sans"].
        if isinstance(b, list):
            assert b != ["dm", "sans"], "'dm sans' was incorrectly split on the space"
        assert _norm_value(b_norm) == "dm sans"


# ===================================================================== REGRESSION (RT)
class TestRegression:
    def test_RT1_golden_snapshot(self, tree):
        """RT-1: token_tree must equal the blessed golden tree.

        Re-bless procedure (ONLY when parser rules intentionally change):
            python -c "import json,sys; \\
              sys.path[:0]=['agents/token_normalizer']; \\
              from normalizer import normalize_tokens, FigmaExtractionResult; \\
              p=json.load(open('agents/figma_extractor/runs/run_sequential_003.json'))['result']; \\
              r=normalize_tokens(FigmaExtractionResult(**p)); \\
              json.dump(r.token_tree, open('agents/token_normalizer/runs/.golden.json','w'), \\
                        indent=2)"
        Review the diff, confirm intentional, then commit the new golden.
        """
        if not GOLDEN.exists():
            pytest.skip("golden not blessed yet")
        expected = json.loads(GOLDEN.read_text())
        assert tree == expected


# ============================================================ PARSER SCOPE (BT-19..22)
# Source: code-task:token-normalizer-implementation:addendum-parser-scope (Desktop).
# Real client Figma files will contain value patterns the 7 parsers don't cover.
# These assert the architecture's promise: unknown patterns route to the UNRESOLVED
# bucket gracefully — no exception, no misclassification into a wrong $type.
class TestParserScope:
    @staticmethod
    def _tree_source_names(tree) -> set:
        """sourceName of every typed leaf actually placed in the tree."""
        names = set()
        for _path, leaf in iter_leaves(tree):
            src = leaf.get("$extensions", {}).get(VENDOR_KEY, {}).get("sourceName")
            if src:
                names.add(src)
        return names

    def _assert_unresolved(self, res, name: str):
        entries = [e for e in res.normalization_report.all_entries if e.source_name == name]
        assert entries, f"{name}: no audit entry"
        assert all(e.confidence == "unresolved" for e in entries), f"{name}: not unresolved"
        assert any(e.source_name == name for e in res.normalization_report.unresolved_tokens), (
            f"{name}: not listed in report.unresolved_tokens"
        )
        assert name not in self._tree_source_names(res.token_tree), (
            f"{name}: leaked into token_tree (should be report-only)"
        )

    def test_BT19_hsl_routes_to_unresolved(self, normalize):
        """BT-19: hsl() color value (no enrichment) -> unresolved, no exception."""
        res = normalize(build_extraction([make_token("--color-brand-hsl", "hsl(210, 50%, 60%)", category="color")]))
        self._assert_unresolved(res, "--color-brand-hsl")

    def test_BT20_calc_routes_to_unresolved(self, normalize):
        """BT-20: calc() dimension expression -> unresolved, no exception."""
        res = normalize(build_extraction([make_token("--spacing-gutter-calc", "calc(100% - 16px)", category="spacing")]))
        self._assert_unresolved(res, "--spacing-gutter-calc")

    def test_BT21_gradient_not_misclassified_as_color(self, normalize):
        """BT-21: linear-gradient() -> unresolved, NOT misclassified as color."""
        res = normalize(
            build_extraction([make_token("--color-hero-gradient", "linear-gradient(90deg, #fff, #000)", category="color")])
        )
        self._assert_unresolved(res, "--color-hero-gradient")

    def test_BT22_keyword_not_misclassified_as_fontfamily(self, normalize):
        """BT-22: a CSS-wide keyword ('normal') must NOT become a fontFamily, even
        when the token NAME infers fontFamily. Both the family-named and the
        neutral-named keyword token route to unresolved."""
        toks = [
            make_token("--typography-font-family-kw", "normal", category="typography"),
            make_token("--neutral-keyword", "normal", category="other"),
        ]
        res = normalize(build_extraction(toks))
        self._assert_unresolved(res, "--typography-font-family-kw")
        self._assert_unresolved(res, "--neutral-keyword")
