"""3d Component Code Generator — Phase 2 agentic generation + structural validation gate.

Path B (from-spec) for elements with no baseline to fork. Behind an injectable Mock/Real split
(SP-20) so CI runs a deterministic oracle (no live LLM/cost); real runs use an Agno agent
(env-gated ``COMP_CODE_GEN_USE_REAL_AGENT=1``). Every LLM-generated element passes a DETERMINISTIC
structural validation gate (Adjustment 1 / VT-17) before inclusion; a gate failure triggers ONE
retry, then SP-6 flag-for-review (never fabricate, never crash).

Variant axes parsed from thin Figma metadata (P3: they arrive as ``name``-string fragments, with
source typos like "Activ"/"Focu") are normalised before rendering.

SP-9: model from ``default_chat_model`` (OPENAI_MODEL_ID); no ``seed`` (Anthropic route rejects it —
3c hardening finding); temperature=0.
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional, Protocol

from pydantic import BaseModel

from agents.component_code_generator.models import StructuralGateResult
from agents.component_code_generator.scaffolding import pascal_case, slugify
from agents.theme_generator.models import Confidence

# Canonical interactive-state vocabulary → normalise the P3 typos ("Activ"/"Focu") + casing.
_CANONICAL_STATES = ("Default", "Hover", "Active", "Focus", "Disabled", "Pressed", "Selected")
_STATE_TYPO_MAP = {"activ": "Active", "focu": "Focus", "defualt": "Default", "hoover": "Hover",
                   "disable": "Disabled", "diabled": "Disabled"}


def normalize_variant_value(value: str) -> str:
    """Normalise a single variant value: fix known state typos, canonical-case known states."""
    v = (value or "").strip()
    low = v.lower()
    if low in _STATE_TYPO_MAP:
        return _STATE_TYPO_MAP[low]
    for canon in _CANONICAL_STATES:
        if low == canon.lower():
            return canon
    return v


def parse_variant_axes(variant_names: list[str]) -> dict[str, list[str]]:
    """Parse Figma variant-leaf name strings (e.g. "State=Hover, Size=md") into {axis: [values]}.

    Values are typo-normalised (P3). Deterministic; order-preserving; dedup per axis.
    """
    axes: dict[str, list[str]] = {}
    for name in variant_names or []:
        for part in str(name).split(","):
            if "=" not in part:
                continue
            axis, _, val = part.partition("=")
            axis = axis.strip()
            val = normalize_variant_value(val)
            if not axis or not val:
                continue
            bucket = axes.setdefault(axis, [])
            if val not in bucket:
                bucket.append(val)
    return axes


# --------------------------------------------------------------------------- #
# Structural validation gate (Adjustment 1 / VT-17) — deterministic
# --------------------------------------------------------------------------- #
def _balanced(src: str) -> bool:
    pairs = {")": "(", "]": "[", "}": "{"}
    stack = []
    for ch in src:
        if ch in "([{":
            stack.append(ch)
        elif ch in pairs:
            if not stack or stack.pop() != pairs[ch]:
                return False
    return not stack


def run_structural_gate(source: str, *, customer_slug: str, element_slug: str) -> StructuralGateResult:
    """Deterministic structural checks on generated Lit+TS (Adjustment 1). Best-effort TS parse
    (balanced delimiters + required tokens; full ``tsc`` is a hardening upgrade [U])."""
    slug = slugify(customer_slug)
    exp_tag = f"{slug}-{element_slug}"
    exp_class = pascal_case(exp_tag) + "Element"
    failures: list[str] = []

    ts_ok = bool(source) and _balanced(source) and "export class" in source and "render" in source
    if not ts_ok:
        failures.append("ts_parseable: unbalanced delimiters or missing export class/render")

    lit_ok = ('@customElement(' in source and "extends LitElement" in source
              and 'from "lit"' in source and "@property" in source)
    if not lit_ok:
        failures.append("lit_pattern: missing @customElement/@property/LitElement/lit import")

    m_tag = re.search(r'@customElement\(\s*["\']([a-z0-9-]+)["\']\s*\)', source)
    tag = m_tag.group(1) if m_tag else None
    naming_ok = tag == exp_tag and exp_class in source
    if not naming_ok:
        failures.append(f"naming: expected tag {exp_tag!r} + class {exp_class!r}")

    token_ok = "--helix-" not in source and (
        ("var(--" not in source) or re.search(rf"var\(--{re.escape(slug)}-", source) is not None)
    if not token_ok:
        failures.append("token_namespace: uses --helix- or a non-customer token namespace")

    reg_ok = tag == exp_tag and exp_class in source  # @customElement matches file/class
    if not reg_ok:
        failures.append("registration: @customElement tag must match class/file")

    return StructuralGateResult(applied=True, ts_parseable=ts_ok, lit_pattern_ok=lit_ok,
                                naming_ok=naming_ok, token_namespace_ok=token_ok,
                                registration_ok=reg_ok, failures=failures)


# --------------------------------------------------------------------------- #
# Generator interface + Mock/Real (SP-20)
# --------------------------------------------------------------------------- #
class GeneratedElement(BaseModel):
    """One from-spec generation result (also the real agent's output schema)."""

    source: str
    confidence: Confidence = "medium"
    rationale: str = ""


class GenerationInput(BaseModel):
    """Thin from-spec input for a Path-B element."""

    slot: str
    customer_slug: str
    variant_axes: dict[str, list[str]] = {}
    tokens_consumed: list[str] = []


class Generator(Protocol):
    def generate(self, spec: GenerationInput) -> GeneratedElement: ...


def _render_from_spec(spec: GenerationInput) -> str:
    """Deterministic from-spec Lit skeleton that PASSES the structural gate.

    Emits a LitElement with one ``@property`` per variant axis + a host style referencing a
    customer token. Used verbatim by MockGenerator, and as the shape the real agent must produce.
    """
    slug = slugify(spec.customer_slug)
    element_slug = slugify(spec.slot)
    tag = f"{slug}-{element_slug}"
    cls = pascal_case(tag) + "Element"
    props = []
    for axis, values in spec.variant_axes.items():
        prop = re.sub(r"[^a-zA-Z0-9]", "", axis[:1].lower() + axis[1:]) or "variant"
        default = values[0] if values else ""
        props.append(f'    @property({{ reflect: true }})\n    {prop} = "{default}";')
    props_block = "\n\n".join(props) or '    @property({ reflect: true })\n    variant = "";'
    return (
        'import { LitElement, html, css } from "lit";\n'
        'import { customElement, property } from "lit/decorators.js";\n\n'
        f'// from-spec skeleton (no baseline equivalent); axes: {sorted(spec.variant_axes) or "none"}\n'
        f'@customElement("{tag}")\n'
        f'export class {cls} extends LitElement {{\n'
        f'{props_block}\n\n'
        f'    static styles = css`:host {{ color: var(--{slug}-colors-text-primary); display: block; }}`;\n\n'
        f'    render() {{ return html`<div class="{element_slug}"><slot></slot></div>`; }}\n'
        f'}}\n'
    )


class MockGenerator:
    """Deterministic oracle (CI default) — emits a gate-passing from-spec skeleton. No LLM."""

    name = "mock"

    def generate(self, spec: GenerationInput) -> GeneratedElement:
        src = _render_from_spec(spec)
        # richer spec → higher confidence (still from-spec, so capped at high)
        conf: Confidence = "high" if spec.variant_axes else "medium"
        return GeneratedElement(source=src, confidence=conf,
                                rationale=f"from-spec skeleton ({len(spec.variant_axes)} variant axes)")


_FROM_SPEC_INSTRUCTIONS = (
    "You generate a single Lit + TypeScript web component from a thin spec (no baseline exists). "
    "Output ONLY the component source. It MUST: import from 'lit' and 'lit/decorators.js'; use "
    "@customElement with the EXACT tag given; export a class extending LitElement named EXACTLY as "
    "given; declare one @property({reflect:true}) per variant axis; use ONLY var(--{customer}-*) "
    "CSS custom properties (never --helix-); implement render(). Do not invent behaviour beyond the "
    "spec. If the spec is too thin to build meaningfully, still emit a minimal valid skeleton."
)


class AgentGenerator:
    """Real from-spec generator — Agno agent, structured output (live runs only; env-gated)."""

    name = "agent"

    def __init__(self, seed: int | None = None) -> None:
        from agno.agent import Agent

        from app.settings import default_chat_model
        self._seed = seed  # recorded, NOT sent (Anthropic route rejects seed — 3c hardening)
        self._agent = Agent(name="3d-from-spec-generator",
                            model=default_chat_model(temperature=0.0),
                            instructions=[_FROM_SPEC_INSTRUCTIONS],
                            output_schema=GeneratedElement)
        self._in = 0
        self._out = 0

    def generate(self, spec: GenerationInput) -> GeneratedElement:
        slug = slugify(spec.customer_slug)
        element_slug = slugify(spec.slot)
        tag = f"{slug}-{element_slug}"
        cls = pascal_case(tag) + "Element"
        prompt = (f"Component: {spec.slot}\nExact tag: {tag}\nExact class: {cls}\n"
                  f"Customer token prefix: --{slug}-\nVariant axes: {spec.variant_axes}\n"
                  f"Tokens available: {spec.tokens_consumed}\nGenerate the Lit component.")
        from agents._shared.observability import extract_usage
        try:
            ro = self._agent.run(input=prompt)
        except Exception as e:  # noqa: BLE001 — SP-6: never crash the run
            return GeneratedElement(source="", confidence="unresolved",
                                    rationale=f"agent run failed: {e!r}")
        i, o = extract_usage(ro)
        self._in += i
        self._out += o
        res = getattr(ro, "content", None)
        if not isinstance(res, GeneratedElement):
            return GeneratedElement(source="", confidence="unresolved",
                                    rationale=f"non-schema output ({type(res).__name__})")
        return res

    def usage(self) -> tuple[int, int]:
        return self._in, self._out
