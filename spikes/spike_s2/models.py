"""
Spike S2 — typed inter-step data models for the HELIX pipeline-plumbing test.

These mirror the real UC2 pipeline shape (Figma -> tokens -> scaffold) but carry
DUMMY data. No real Figma calls. Used to test whether Agno's Workflow primitive
can carry typed data between Steps, a Team inside a Step, an HITL gate, and
session_state config.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ProjectConfig(BaseModel):
    """Simulates session_state project configuration (per-run knobs)."""

    figma_file_key: str
    figma_pat: str = Field(description="in real pipeline: injected, never logged")
    storybook_repo_url: str
    cms_repo_url: str
    cms_type: Literal["storyblok", "contentful"]
    framework: Literal["vue", "webcomponents", "react"]


class FigmaExtractionResult(BaseModel):
    """Output of Step 1: raw extraction (Team-inside-Step, multi-pull consensus)."""

    tokens_raw: dict = Field(default_factory=dict, description='e.g. {"colors": {...}, "typography": {...}}')
    components_raw: list[dict] = Field(default_factory=list, description='e.g. [{"name":"Button","variants":[...]}]')
    extraction_runs: int = Field(default=0, description="how many multi-pull runs were synthesized")
    consensus_confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class NormalizedTokens(BaseModel):
    """Output of Step 2: normalized DTCG tokens."""

    dtcg_tokens: dict = Field(default_factory=dict, description="simulated W3C Design Tokens structure")
    typos_detected: list[str] = Field(default_factory=list, description='e.g. ["disbled -> disabled"]')
    token_count: int = 0


class ScaffoldedOutput(BaseModel):
    """Output of Step 3: generated code."""

    vue_components: list[dict] = Field(default_factory=list, description='[{"name":"Button.vue","code":"..."}]')
    storybook_stories: list[dict] = Field(default_factory=list)
    cms_bloks: list[dict] = Field(default_factory=list, description="simulated Storyblok bloks")
