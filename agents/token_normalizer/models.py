"""Token Normalizer — typed I/O schema (HELIX UC2 pipeline Step 2).

This module is the FROZEN CONTRACT for the Token Normalizer. It is consumed by
three independent surfaces that must not drift:

  - ``normalizer.py``  — produces ``NormalizedTokens``
  - ``tests/``         — asserts against these shapes
  - downstream steps   — Style Dictionary + the future Semantic Matcher agent

Spec source of truth: ``helix-poc-agno:agents:token-normalizer:step1-spec`` (v2).
Input contract: ``agents/figma_extractor/models.py`` (``FigmaExtractionResult``).

Design notes
------------
* ``token_tree`` is a plain ``dict`` (not a typed Pydantic tree) because DTCG
  group nesting is arbitrary-depth and serialises directly to ``.tokens.json``.
* DTCG ``$value`` payloads ARE typed (the models below) so the pipeline builds
  them safely; they are flattened to dicts via ``model_dump(exclude_none=True)``
  only at tree-assembly time, keeping the wire format clean (no ``null`` noise).
* Provenance lives in ``$extensions.de.msqdx.helix`` on every leaf; QA-only
  fields (``original_value``, ``suspected_typo``) live in the report ONLY.
"""
from __future__ import annotations

from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

NORMALIZER_VERSION = "0.1.0"
VENDOR_KEY = "de.msqdx.helix"

# --- enumerations -----------------------------------------------------------

# DTCG $type values for which we have evidence in run_sequential_003. DTCG also
# defines duration/cubicBezier/strokeStyle/border/transition/gradient — add when
# evidence appears (see spec §"DTCG $type ENUM").
DTCGType = Literal[
    "color",
    "dimension",
    "fontFamily",
    "fontWeight",
    "number",
    "typography",
    "shadow",
]

# How confident the normaliser is in a token's type/path assignment.
NormalizationConfidence = Literal["authoritative", "high", "medium", "unresolved"]

# Provenance: where the canonical path / the $type each came from.
PathSource = Literal["enrichment", "css-var-parsed", "manual"]
TypeSource = Literal["enrichment", "value-pattern", "name-convention", "manual"]

DimensionUnit = Literal["px", "rem", "em", "%"]


# --- DTCG value types -------------------------------------------------------
# Each models a single DTCG `$value`. They are dumped with exclude_none=True so
# optional fields (alpha, letterSpacing, ...) simply vanish when absent.


class DTCGColorValue(BaseModel):
    """DTCG color value (spec §9 color). Components are sRGB in the 0..1 range."""

    model_config = ConfigDict(extra="forbid")

    colorSpace: Literal["srgb"] = "srgb"
    components: list[float] = Field(..., min_length=3, max_length=3)
    alpha: Optional[float] = None  # omitted when fully opaque
    hex: Optional[str] = None  # convenience mirror, e.g. "#ffffff"


class DTCGDimensionValue(BaseModel):
    """DTCG dimension value (spec §9.2). Unit widened to em/% for source fidelity."""

    model_config = ConfigDict(extra="forbid")

    value: float
    unit: DimensionUnit = "px"


class DTCGFontFamilyValue(BaseModel):
    """DTCG fontFamily value — a single family or a fallback stack."""

    model_config = ConfigDict(extra="forbid")

    value: Union[str, list[str]]


class DTCGFontWeightValue(BaseModel):
    """DTCG fontWeight value — numeric (100..900) preferred, string for named weights."""

    model_config = ConfigDict(extra="forbid")

    value: Union[int, str]


class DTCGNumberValue(BaseModel):
    """DTCG number value — a unitless scalar."""

    model_config = ConfigDict(extra="forbid")

    value: float


class DTCGTypographyValue(BaseModel):
    """DTCG composite typography value (spec §9.8)."""

    model_config = ConfigDict(extra="forbid")

    fontFamily: Union[str, list[str]]
    fontWeight: Union[int, str]
    fontSize: DTCGDimensionValue
    lineHeight: DTCGDimensionValue
    letterSpacing: Optional[DTCGDimensionValue] = None  # absent on some composites


class DTCGShadowValue(BaseModel):
    """DTCG composite shadow value (spec §9.6). A token's $value may be a list of these."""

    model_config = ConfigDict(extra="forbid")

    color: DTCGColorValue
    offsetX: DTCGDimensionValue
    offsetY: DTCGDimensionValue
    blur: DTCGDimensionValue
    spread: DTCGDimensionValue


# --- provenance / audit -----------------------------------------------------


class HelixProvenance(BaseModel):
    """Payload stored at ``$extensions.de.msqdx.helix`` on every leaf token.

    Deliberately excludes ``original_value`` and ``suspected_typo`` — those are
    QA artifacts and live in the report only (spec AGREED decision #3, CT-15).
    """

    model_config = ConfigDict(extra="forbid")

    confidence: NormalizationConfidence
    sourceName: str  # original CSS-var name, e.g. "--color-control-background-default"
    enrichmentPath: Optional[str] = None  # original slash-path, e.g. "colors/control/background/default"
    originalCategory: Optional[str] = None  # pre-collapse type, e.g. "borderRadius"
    normalizerVersion: str = NORMALIZER_VERSION


class TokenNormalizationEntry(BaseModel):
    """One row of the per-token audit trail (report only — never in the tree)."""

    model_config = ConfigDict(extra="forbid")

    canonical_path: str  # DTCG dotted path, e.g. "color.control.background.default"
    dtcg_type: Optional[DTCGType] = None  # None only when unresolved
    confidence: NormalizationConfidence
    source_name: str
    enrichment_path: Optional[str] = None
    path_source: PathSource
    type_source: Optional[TypeSource] = None
    suspected_typo: Optional[str] = None  # report-only
    original_value: Optional[str] = None  # raw value before normalisation (report-only)
    notes: Optional[str] = None


class DuplicateValueGroup(BaseModel):
    """A set of tokens that resolved to the same value (semantic aliases, kept)."""

    model_config = ConfigDict(extra="forbid")

    value: str  # normalised value key (lowercased / trimmed)
    paths: list[str]  # canonical paths that share this value
    count: int


class PathCollision(BaseModel):
    """Two distinct tokens that derived an identical canonical path (suffixed)."""

    model_config = ConfigDict(extra="forbid")

    attempted_path: str
    resolved_path: str  # path after numeric-suffix disambiguation
    source_name: str


class NormalizationReport(BaseModel):
    """Quality metrics + full audit trail for one normalisation run."""

    model_config = ConfigDict(extra="forbid")

    # coverage
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    tokens_with_enrichment: int = 0
    tokens_without_enrichment: int = 0

    # confidence distribution (CT-12: these four sum to total_output_tokens)
    authoritative_count: int = 0
    high_confidence_count: int = 0
    medium_confidence_count: int = 0
    unresolved_count: int = 0

    # distributions
    type_distribution: dict[str, int] = Field(default_factory=dict)

    # issues
    typos_flagged: list[str] = Field(default_factory=list)
    duplicate_value_groups: list[DuplicateValueGroup] = Field(default_factory=list)
    composites_decomposed: list[str] = Field(default_factory=list)  # canonical paths
    path_collisions: list[PathCollision] = Field(default_factory=list)
    fallback_warnings: list[str] = Field(default_factory=list)  # e.g. unknown enrichment_type

    # gaps
    unresolved_tokens: list[TokenNormalizationEntry] = Field(default_factory=list)
    enrichment_only_tokens: list[str] = Field(default_factory=list)

    # full per-token audit trail
    all_entries: list[TokenNormalizationEntry] = Field(default_factory=list)


class NormalizedTokens(BaseModel):
    """Top-level Token Normalizer output (Workflow Step 2 content)."""

    model_config = ConfigDict(extra="forbid")

    token_tree: dict = Field(default_factory=dict)  # DTCG group hierarchy → .tokens.json
    normalization_report: NormalizationReport = Field(default_factory=NormalizationReport)
    component_token_map: dict[str, list[str]] = Field(default_factory=dict)
    source_file_key: str = ""
    source_enrichment_coverage: float = 0.0
    normalizer_version: str = NORMALIZER_VERSION
