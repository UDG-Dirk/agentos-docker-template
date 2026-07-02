"""Figma Extractor — typed I/O schema (HELIX UC2 pipeline Step 1).

Schema is the contract from agents:figma-extractor:step1-spec. The next pipeline
step (Token Normalizer) consumes FigmaExtractionResult, so field names/types are
load-bearing — do not rename without updating the spec + downstream steps.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class PageInfo(BaseModel):
    name: str
    node_id: str
    page_type: Literal["foundation", "component", "other"]  # classified by the extractor
    child_count: int = 0


class TokenEntry(BaseModel):
    name: str  # reconstructed CSS-var name from Framelink (e.g. --color-primary-500)
    value: str  # resolved value (e.g. #3388F0)
    style_id: Optional[str] = None  # Figma style ID for cross-referencing
    category: Literal["color", "typography", "spacing", "sizing", "effect", "other"] = "other"
    enrichment_match: Optional[str] = None  # matched Variable slash-path (e.g. colors/interactive/surface/default)
    enrichment_type: Optional[str] = None  # $type from enrichment catalog (e.g. "color", "dimension")
    suspected_typo: Optional[str] = None  # e.g. "disbled -> disabled"


class ComponentVariant(BaseModel):
    variant: str = ""  # e.g. "Solid", "Ghost", "Outline"
    size: str = ""  # e.g. "sm", "md", "lg", "xl"
    state: str = ""  # e.g. "Default", "Hover", "Active", "Focus", "Disabled"
    node_id: str = ""


class ComponentEntry(BaseModel):
    name: str  # e.g. "Button"
    node_id: str  # component set node
    variants: list[ComponentVariant] = Field(default_factory=list)
    props: Optional[dict] = None  # from Code Connect (enrichment) — TS props type
    code_connect_snippet: Optional[str] = None  # Vue snippet from enrichment
    designer_instructions: Optional[str] = None  # agent-instructions from component description
    tokens_consumed: list[str] = Field(default_factory=list)  # token names referenced by this component


class AssetEntry(BaseModel):
    original_url: str = ""  # MCP ephemeral URL (reference only)
    local_path: str = ""  # downloaded file path (or "DOWNLOAD_FAILED")
    node_id: str = ""
    format: str = ""  # png, svg, etc.


class FigmaExtractionResult(BaseModel):
    file_key: str
    pages_discovered: list[PageInfo] = Field(default_factory=list)
    tokens: list[TokenEntry] = Field(default_factory=list)
    components: list[ComponentEntry] = Field(default_factory=list)
    assets: list[AssetEntry] = Field(default_factory=list)
    extraction_runs: int = 1  # 1 for sequential, N for broadcast
    consensus_confidence: float = 1.0  # 1.0 for sequential, computed for broadcast
    gaps_detected: list[str] = Field(default_factory=list)  # missing Variables, empty pages, failures
    typos_detected: list[str] = Field(default_factory=list)  # suspected typos in token names
    enrichment_coverage: float = 0.0  # % of enrichment Variables matched in extraction
