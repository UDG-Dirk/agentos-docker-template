"""Token Normalizer — deterministic 7-phase pipeline (HELIX UC2, Workflow Step 2).

Pure Python, zero LLM. Consumes ``FigmaExtractionResult`` (Step 1 output) and
produces a W3C DTCG (Design Tokens Community Group format) 2025.10 compliant ``token_tree`` plus a ``NormalizationReport``.

DTCG scope is finite BY DECISION (helix-poc-agno:decision:token-normalizer-dtcg-scope-2026-07-30,
DES + FE-DEV consulted): the four handled $types are color, dimension, shadow, and typography
(the last a composite covering fontFamily/fontWeight/number sub-values). Six types are deliberately
out of scope — duration, cubicBezier, transition (motion), strokeStyle, border (decompose to
primitives), and gradient (a real but currently-unused gap). Anything unhandled routes to UNRESOLVED
(fail-loud, never guessed). See README "DTCG Scope" for the rationale and revisit conditions.

Pipeline (spec §"NORMALIZATION PIPELINE"):
  1. Type Assignment      — enrichment_type (authoritative) → value/name inference
  2. Path Derivation      — enrichment slash-path → dots, else CSS-var name → dots
  3+4. Value Normalization & Composite Decomposition — per-type parsers
  5. Tree Assembly        — nested dict + $extensions provenance on every leaf
  6. Report Generation    — coverage, confidence/type distribution, issues, audit
  (7. HITL review gate is a Workflow-level concern, not this function — see workflow_step.py)

Design decisions worth flagging (see README "Design Decisions"):
* DD-1 $type is LEAF-EXPLICIT (no group-level inheritance) — unambiguous + simplest.
* DD-2 UNRESOLVED tokens are recorded in the report (with raw value), NOT inserted
  into the DTCG tree, so ``token_tree`` stays spec-valid (every leaf has a real
  $type). This is a spec interpretation — surfaced for HITL/QA confirmation.

QUARANTINE — enrichment is currently INACTIVE in the live pipeline.
  Phases 1 & 2 read ``enrichment_type`` / ``enrichment_match`` as the *authoritative*
  type/path signal, but the deterministic Figma extractor never populates those fields
  (only the legacy LLM ``agent.py``, kept as dead code, ever did). So every token today
  arrives WITHOUT enrichment and the code always takes the value/name-inference fallback.
  These read-paths are retained DELIBERATELY (enrichment may become real if Code Connect /
  Variable slash-path metadata is wired later) — do not delete them. As a tripwire,
  ``normalize_tokens`` logs a WARNING if any token *does* arrive with enrichment (unexpected
  in the current pipeline). See agents/figma_extractor/README.md "enrichment fields are
  currently inactive" and TokenEntry.enrichment_* in figma_extractor/models.py.

Public API: ``normalize_tokens(extraction) -> NormalizedTokens``.
"""
from __future__ import annotations

import importlib.util as _ilu
import json
import logging
import re
import sys
from pathlib import Path
from typing import Optional, Union

# Import hygiene: BOTH this agent and figma_extractor ship a module named
# ``models``. We put ONLY this dir on sys.path (so ``import models`` is ours),
# and load the figma_extractor input schema by explicit file path under a
# distinct name — so the two never collide regardless of caller sys.path order.
_HELIX_ROOT = Path(__file__).resolve().parents[2]
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))


def _load_figma_models():
    path = _HELIX_ROOT / "agents" / "figma_extractor" / "models.py"
    spec = _ilu.spec_from_file_location("helix_figma_models", path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ImportError(f"cannot load figma models from {path}")
    mod = _ilu.module_from_spec(spec)
    # Register before exec so pydantic can resolve `from __future__ annotations`
    # forward refs (PageInfo, TokenEntry, ...) against this module's namespace.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    mod.FigmaExtractionResult.model_rebuild()
    return mod


# Re-exported as the single, collision-free import surface for callers/tests:
#   from normalizer import normalize_tokens, NormalizedTokens, FigmaExtractionResult, TokenEntry
_figma_models = _load_figma_models()
FigmaExtractionResult = _figma_models.FigmaExtractionResult
TokenEntry = _figma_models.TokenEntry
ComponentEntry = _figma_models.ComponentEntry

from models import (  # noqa: E402
    NORMALIZER_VERSION,
    VENDOR_KEY,
    DTCGFontFamilyValue,
    DTCGFontWeightValue,
    DTCGNumberValue,
    DuplicateValueGroup,
    HelixProvenance,
    NormalizationReport,
    NormalizedTokens,
    PathCollision,
    TokenNormalizationEntry,
)
from parsers.color import parse_color  # noqa: E402
from parsers.dimension import parse_dimension  # noqa: E402
from parsers.paths import css_var_to_dotted, enrichment_path_to_dotted  # noqa: E402
from parsers.shadow import parse_box_shadow  # noqa: E402
from parsers.typography import parse_font_family, parse_typography  # noqa: E402

log = logging.getLogger("token_normalizer")

# enrichment_type → DTCG $type (spec mapping table). borderRadius collapses to
# dimension; the original category is preserved in $extensions.originalCategory.
ENRICHMENT_TO_DTCG = {
    "color": "color",
    "dimension": "dimension",
    "fontFamily": "fontFamily",
    "fontWeight": "fontWeight",
    "borderRadius": "dimension",
    "fontStyle": "typography",
    "effect": "shadow",
}

# Extractor category → DTCG $type for name-convention inference ("other" omitted).
_CATEGORY_TO_DTCG = {
    "color": "color",
    "typography": "typography",
    "spacing": "dimension",
    "sizing": "dimension",
    "effect": "shadow",
}

_HEX_RE = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_DIM_RE = re.compile(r"^-?\d*\.?\d+\s*(px|rem|em|%)$", re.IGNORECASE)
_INT_RE = re.compile(r"^\d+$")


# --- Phase 1: type assignment ----------------------------------------------


def _infer_type_from_value(value: str) -> Optional[str]:
    v = value.strip()
    low = v.lower()
    if ";" in v and "font-family" in low:
        return "typography"
    if low.startswith("box-shadow"):
        return "shadow"
    if _HEX_RE.match(v) or low.startswith("rgb"):
        return "color"
    if _DIM_RE.match(v):
        return "dimension"
    if _INT_RE.match(v):
        return "number"
    return None


def _infer_type_from_name(name: str, category: str) -> Optional[str]:
    n = name.lower()
    if "font-family" in n:
        return "fontFamily"
    if "font-weight" in n:
        return "fontWeight"
    if n.startswith("--color"):
        return "color"
    if n.startswith("--typography"):
        return "typography"
    if n.startswith(("--effect", "--shadow")):
        return "shadow"
    if n.startswith(
        ("--spacing", "--sizing", "--layout", "--dimension", "--radius", "--stroke")
    ) or "radius" in n or "stroke" in n:
        return "dimension"
    return _CATEGORY_TO_DTCG.get(category)


def _assign_type(tok) -> tuple[Optional[str], str, Optional[str], Optional[str]]:
    """Return (dtcg_type, confidence, type_source, fallback_warning)."""
    # QUARANTINE (see module docstring): no token carries enrichment_type today → this authoritative
    # branch is dead; the live path falls through to value/name inference. Retained for a future wiring.
    et = (tok.enrichment_type or "").strip()
    if et and et in ENRICHMENT_TO_DTCG:
        return ENRICHMENT_TO_DTCG[et], "authoritative", "enrichment", None

    warning = (
        f"unknown enrichment_type {et!r} on {tok.name!r}; fell back to inference"
        if et
        else None
    )
    from_value = _infer_type_from_value(tok.value)
    from_name = _infer_type_from_name(tok.name, tok.category)

    if from_value and from_name and from_value == from_name:
        return from_value, "high", "value-pattern", warning
    if from_value:  # value evidence wins when signals disagree or only one exists
        return from_value, "medium", "value-pattern", warning
    if from_name:
        return from_name, "medium", "name-convention", warning
    return None, "unresolved", None, warning


# --- Phase 3+4: value normalization & composite decomposition ---------------


def _normalize_value(dtcg_type: str, raw: str):
    v = raw.strip()
    if dtcg_type == "color":
        return parse_color(v)
    if dtcg_type == "dimension":
        return parse_dimension(v)
    if dtcg_type == "fontFamily":
        return parse_font_family(v)
    if dtcg_type == "fontWeight":
        if v.isdigit():
            return DTCGFontWeightValue(value=int(v))
        return DTCGFontWeightValue(value=v) if v else None
    if dtcg_type == "number":
        try:
            return DTCGNumberValue(value=float(v))
        except ValueError:
            return None
    if dtcg_type == "typography":
        return parse_typography(v)
    if dtcg_type == "shadow":
        return parse_box_shadow(v)
    return None


def _dump_value(value) -> Union[dict, list]:
    if isinstance(value, list):
        return [v.model_dump(exclude_none=True) for v in value]
    return value.model_dump(exclude_none=True)


# --- Phase 2 helper: path derivation ----------------------------------------


def _derive_path(tok) -> tuple[str, str]:
    # QUARANTINE (see module docstring): no token carries enrichment_match today → dead path, retained.
    enrichment = (tok.enrichment_match or "").strip()
    if enrichment:
        return enrichment_path_to_dotted(enrichment), "enrichment"
    return css_var_to_dotted(tok.name), "css-var-parsed"


# --- Phase 5: tree assembly --------------------------------------------------


def _insert_leaf(
    tree: dict,
    dotted_path: str,
    leaf: dict,
    source_name: str,
    collisions: list[PathCollision],
) -> str:
    """Insert ``leaf`` at ``dotted_path``; disambiguate collisions with a numeric
    suffix on the final segment. Returns the actually-used dotted path."""
    segs = dotted_path.split(".")
    node = tree
    parents = segs[:-1]
    walked: list[str] = []
    for seg in parents:
        child = node.get(seg)
        if isinstance(child, dict) and "$value" not in child:
            node = child
        elif child is None:
            new: dict = {}
            node[seg] = new
            node = new
        else:
            # A leaf already occupies a slot we need as a group → suffix and log.
            suffixed = _unique_key(node, seg)
            collisions.append(
                PathCollision(
                    attempted_path=dotted_path,
                    resolved_path=".".join(walked + [suffixed] + segs[len(walked) + 1 :]),
                    source_name=source_name,
                )
            )
            new = {}
            node[suffixed] = new
            node = new
            seg = suffixed
        walked.append(seg)

    last = segs[-1]
    existing = node.get(last)
    if isinstance(existing, dict) and "$value" in existing:
        last = _unique_key(node, last)
        collisions.append(
            PathCollision(
                attempted_path=dotted_path,
                resolved_path=".".join(walked + [last]),
                source_name=source_name,
            )
        )
    node[last] = leaf
    return ".".join(walked + [last])


def _unique_key(node: dict, key: str) -> str:
    i = 2
    while f"{key}-{i}" in node:
        i += 1
    return f"{key}-{i}"


# --- orchestration -----------------------------------------------------------


def _norm_value_key(value: str) -> str:
    return value.strip().lower()


def normalize_tokens(extraction) -> NormalizedTokens:
    """Normalize a ``FigmaExtractionResult`` into DTCG ``NormalizedTokens``.

    Deterministic: identical input always yields identical output.
    """
    report = NormalizationReport()
    tree: dict = {}
    name_to_path: dict[str, str] = {}  # css-var name / enrichment path → tree path
    value_to_paths: dict[str, list[str]] = {}
    collisions = report.path_collisions

    for tok in extraction.tokens:
        report.total_input_tokens += 1
        has_enrichment = bool((tok.enrichment_match or "").strip())
        if has_enrichment:
            report.tokens_with_enrichment += 1
        else:
            report.tokens_without_enrichment += 1

        dtcg_type, confidence, type_source, warning = _assign_type(tok)
        if warning:
            report.fallback_warnings.append(warning)

        dotted_path, path_source = _derive_path(tok)
        original_category = (tok.enrichment_type or tok.category or None)

        normalized = _normalize_value(dtcg_type, tok.value) if dtcg_type else None
        if dtcg_type is None or normalized is None:
            # UNRESOLVED → report only, never into the DTCG tree (DD-2).
            confidence = "unresolved"
            entry = TokenNormalizationEntry(
                canonical_path=dotted_path,
                dtcg_type=None,
                confidence="unresolved",
                source_name=tok.name,
                enrichment_path=tok.enrichment_match or None,
                path_source=path_source,
                type_source=type_source,
                suspected_typo=tok.suspected_typo,
                original_value=tok.value,
                notes="value unparseable" if dtcg_type else "type unresolved",
            )
            report.all_entries.append(entry)
            report.unresolved_tokens.append(entry)
            report.unresolved_count += 1
            report.type_distribution["unresolved"] = (
                report.type_distribution.get("unresolved", 0) + 1
            )
            # still indexed so a component referencing it degrades gracefully (skipped)
            continue

        provenance = HelixProvenance(
            confidence=confidence,  # type: ignore[arg-type]
            sourceName=tok.name,
            enrichmentPath=tok.enrichment_match or None,
            originalCategory=original_category,
        )
        leaf = {
            "$value": _dump_value(normalized),
            "$type": dtcg_type,
            "$extensions": {VENDOR_KEY: provenance.model_dump(exclude_none=True)},
        }
        final_path = _insert_leaf(tree, dotted_path, leaf, tok.name, collisions)

        # indices
        name_to_path[tok.name] = final_path
        if has_enrichment:
            name_to_path.setdefault(tok.enrichment_match, final_path)
        value_to_paths.setdefault(_norm_value_key(tok.value), []).append(final_path)

        # audit + tallies
        report.all_entries.append(
            TokenNormalizationEntry(
                canonical_path=final_path,
                dtcg_type=dtcg_type,  # type: ignore[arg-type]
                confidence=confidence,  # type: ignore[arg-type]
                source_name=tok.name,
                enrichment_path=tok.enrichment_match or None,
                path_source=path_source,
                type_source=type_source,
                suspected_typo=tok.suspected_typo,
                original_value=tok.value,
            )
        )
        report.type_distribution[dtcg_type] = report.type_distribution.get(dtcg_type, 0) + 1
        if confidence == "authoritative":
            report.authoritative_count += 1
        elif confidence == "high":
            report.high_confidence_count += 1
        elif confidence == "medium":
            report.medium_confidence_count += 1
        if dtcg_type in ("typography", "shadow"):
            report.composites_decomposed.append(final_path)

    report.total_output_tokens = len(report.all_entries)

    # QUARANTINE tripwire (see module docstring): enrichment is inactive today, so if a token DID
    # carry it, warn loudly — non-fatal; the read-paths handle it, this only flags the assumption changed.
    if report.tokens_with_enrichment:
        log.warning(
            "enrichment is unexpectedly ACTIVE: %d/%d tokens carried enrichment_match/type. The "
            "deterministic extractor is not supposed to populate enrichment today (see normalizer "
            "module docstring / figma_extractor README). If Code Connect / Variable-slash-path "
            "enrichment was intentionally wired, remove this quarantine tripwire.",
            report.tokens_with_enrichment, report.total_input_tokens,
        )

    # duplicate value groups (semantic aliases — observed, NOT merged)
    for value_key, paths in value_to_paths.items():
        if len(paths) > 1:
            report.duplicate_value_groups.append(
                DuplicateValueGroup(value=value_key, paths=sorted(paths), count=len(paths))
            )
    report.duplicate_value_groups.sort(key=lambda g: (-g.count, g.value))

    # typos: extractor-level + per-token
    typos: list[str] = list(extraction.typos_detected or [])
    for tok in extraction.tokens:
        if tok.suspected_typo:
            typos.append(f"{tok.name}: {tok.suspected_typo}")
    report.typos_flagged = typos

    # component → canonical token paths (reconcile both naming worlds)
    component_token_map: dict[str, list[str]] = {}
    for comp in extraction.components:
        resolved = [name_to_path[c] for c in comp.tokens_consumed if c in name_to_path]
        component_token_map[comp.name] = resolved

    return NormalizedTokens(
        token_tree=tree,
        normalization_report=report,
        component_token_map=component_token_map,
        source_file_key=extraction.file_key,
        source_enrichment_coverage=extraction.enrichment_coverage,
        normalizer_version=NORMALIZER_VERSION,
    )


# --- CLI: run against a Figma Extractor run file ----------------------------


def _load_extraction(run_path: Path):
    payload = json.loads(run_path.read_text())
    return FigmaExtractionResult(**payload["result"])


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run the Token Normalizer on a Figma Extractor run.")
    parser.add_argument(
        "--input",
        default=str(_HELIX_ROOT / "agents" / "figma_extractor" / "runs" / "run_sequential_003.json"),
        help="path to a Figma Extractor run JSON ({result, _meta} wrapper)",
    )
    parser.add_argument(
        "--outdir",
        default=str(Path(__file__).resolve().parent / "runs"),
        help="output directory for .tokens.json + report",
    )
    args = parser.parse_args(argv)

    extraction = _load_extraction(Path(args.input))
    result = normalize_tokens(extraction)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "design.tokens.json").write_text(json.dumps(result.token_tree, indent=2))
    (outdir / "normalization_report.json").write_text(
        json.dumps(result.normalization_report.model_dump(), indent=2)
    )
    (outdir / "normalized_tokens.json").write_text(json.dumps(result.model_dump(), indent=2))

    rep = result.normalization_report
    print(
        f"tokens in={rep.total_input_tokens} out={rep.total_output_tokens} | "
        f"authoritative={rep.authoritative_count} high={rep.high_confidence_count} "
        f"medium={rep.medium_confidence_count} unresolved={rep.unresolved_count}"
    )
    print(f"type_distribution={rep.type_distribution}")
    print(f"components={ {k: len(v) for k, v in result.component_token_map.items()} }")
    print(f"wrote → {outdir}/design.tokens.json (+ report, + full output)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
