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

import re
from typing import Optional, Protocol

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

    # Lit essentials — quote-agnostic import (real LLMs emit single quotes), and @property is
    # OPTIONAL: a valid component can have zero reactive properties (SP-26: live-run 2026-07-31 found
    # real claude-sonnet-4-6 output failing here on `from 'lit'` + a prop-less HeaderLogo).
    lit_ok = ('@customElement(' in source and "extends LitElement" in source
              and re.search(r"from\s+['\"]lit['\"]", source) is not None)
    if not lit_ok:
        failures.append("lit_pattern: missing @customElement / extends LitElement / lit import")

    m_tag = re.search(r'@customElement\(\s*["\']([a-z0-9-]+)["\']\s*\)', source)
    tag = m_tag.group(1) if m_tag else None
    naming_ok = tag == exp_tag and exp_class in source
    if not naming_ok:
        failures.append(f"naming: expected tag {exp_tag!r} + class {exp_class!r}")

    # every var(--…) token must be in the CUSTOMER namespace. Substring-checking "--helix-" would
    # false-positive when the slug itself contains "helix" (e.g. helix-modules-int) — so check the
    # prefix of each token instead.
    bad_tokens = [t for t in re.findall(r"var\(--[a-z0-9-]+", source) if not t.startswith(f"var(--{slug}-")]
    token_ok = not bad_tokens
    if not token_ok:
        failures.append(f"token_namespace: non-customer token(s) {bad_tokens[:3]}")

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
    """Thin from-spec input for a Path-B element.

    Track D v0.2.5: ``figma_context`` is an OPTIONAL distillation of the enriched composition_tree
    (Rank 1 text / Rank 2 auto-layout / Rank 3 property schema) for this element. Absent → Path-B
    behaves exactly as v0.2 (BC-A). Present → the generator grounds output in real design data.
    """

    slot: str
    customer_slug: str
    variant_axes: dict[str, list[str]] = {}
    tokens_consumed: list[str] = []
    figma_context: Optional[dict] = None


_MAX_TEXT_SAMPLES = 12


def distill_element_context(frames: list[dict] | None) -> Optional[dict]:
    """Distil an organism's enriched composition frames (Track D Phase 2a/2b output) into a compact
    from-spec context. Consumes ``text_content`` / ``auto_layout`` / ``component_property_definitions``
    produced by the extractor. Returns None when the frames carry no enrichment (BC-A fallback).

    Deterministic + order-preserving. The organism ROOT's auto-layout wins (shallowest frame that has
    one); text copy is deduped + bounded; property definitions from any COMPONENT_SET/COMPONENT merge.
    """
    if not frames:
        return None
    ctx: dict = {}
    texts: list[str] = []
    root_layout: Optional[dict] = None
    root_depth: Optional[int] = None
    prop_defs: dict = {}
    for fr in frames:
        if not isinstance(fr, dict):
            continue
        tc = fr.get("text_content")
        if isinstance(tc, dict) and tc.get("characters"):
            chars = str(tc["characters"]).strip()
            if chars and chars not in texts:
                texts.append(chars)
        al = fr.get("auto_layout")
        if isinstance(al, dict) and al.get("layout_mode"):
            d = fr.get("depth")
            if root_depth is None or (isinstance(d, int) and d < root_depth):
                root_layout, root_depth = al, d if isinstance(d, int) else root_depth
        pd = fr.get("component_property_definitions")
        if isinstance(pd, dict):
            for name, d in pd.items():
                if name not in prop_defs and isinstance(d, dict):
                    prop_defs[name] = d
    if texts:
        ctx["text_samples"] = texts[:_MAX_TEXT_SAMPLES]
    if root_layout:
        ctx["auto_layout"] = root_layout
    if prop_defs:
        ctx["property_definitions"] = prop_defs
    return ctx or None


class Generator(Protocol):
    def generate(self, spec: GenerationInput) -> GeneratedElement: ...


def _variant_union(values: list[str]) -> str:
    """A TypeScript string-union annotation from variant values (deterministic, deduped in order)."""
    seen = [v for v in dict.fromkeys(values) if v]
    return " | ".join(f'"{v}"' for v in seen)


def _host_style(slug: str, auto_layout: Optional[dict]) -> str:
    """Host CSS. With Track D auto_layout, ground it in the REAL layout (direction + numeric gap/pad
    from Figma — honest values, not fabricated tokens); else the plain block default."""
    if auto_layout and auto_layout.get("layout_mode") in ("HORIZONTAL", "VERTICAL"):
        direction = "row" if auto_layout["layout_mode"] == "HORIZONTAL" else "column"
        decls = [f"color: var(--{slug}-colors-text-primary)", "display: flex", f"flex-direction: {direction}"]
        gap = auto_layout.get("item_spacing")
        if isinstance(gap, (int, float)):
            decls.append(f"gap: {gap}px")
        pads = [auto_layout.get(k) for k in ("padding_top", "padding_right", "padding_bottom", "padding_left")]
        if any(isinstance(p, (int, float)) for p in pads):
            decls.append("padding: " + " ".join(f"{(p if isinstance(p,(int,float)) else 0)}px" for p in pads))
        return "; ".join(decls) + ";"
    return f"color: var(--{slug}-colors-text-primary); display: block;"


def _render_from_spec(spec: GenerationInput) -> str:
    """Deterministic from-spec Lit skeleton that PASSES the structural gate.

    Emits a LitElement with one ``@property`` per variant axis + a host style referencing a customer
    token. Track D v0.2.5: when ``figma_context`` is present, props gain typed unions from the real
    property schema, the host style is grounded in the real auto-layout, and the real text copy is
    recorded as a comment. Used verbatim by MockGenerator, and as the shape the real agent produces.
    """
    slug = slugify(spec.customer_slug)
    element_slug = slugify(spec.slot)
    tag = f"{slug}-{element_slug}"
    cls = pascal_case(tag) + "Element"
    ctx = spec.figma_context or {}
    prop_defs = ctx.get("property_definitions") or {}

    props = []
    for axis, values in spec.variant_axes.items():
        prop = re.sub(r"[^a-zA-Z0-9]", "", axis[:1].lower() + axis[1:]) or "variant"
        default = values[0] if values else ""
        # Track D (Rank 3 ONLY): a typed union is added when the REAL property schema supplies
        # variant_options. Without figma_context the property stays bare — byte-identical to v0.2
        # (keeps the Phase-4 State-B baseline uncontaminated / BC-A).
        schema_opts = None
        for pname, pdef in prop_defs.items():
            if isinstance(pdef, dict) and pname.split("#")[0].strip().lower() == axis.lower():
                schema_opts = [normalize_variant_value(v) for v in (pdef.get("variant_options") or [])]
                break
        union = _variant_union(schema_opts) if schema_opts else ""
        annot = f": {union}" if union else ""
        values_comment = f'    // {axis}: {" | ".join(values)}\n' if values else ""
        props.append(f'{values_comment}    @property({{ reflect: true }})\n    {prop}{annot} = "{default}";')
    props_block = "\n\n".join(props) or '    @property({ reflect: true })\n    variant = "";'

    text_comment = ""
    if ctx.get("text_samples"):
        joined = " | ".join(str(t).replace("\n", " ")[:40] for t in ctx["text_samples"][:6])
        text_comment = f'// design copy (Figma): {joined}\n'
    layout_note = ""
    if ctx.get("auto_layout", {}).get("layout_mode"):
        layout_note = f'// layout (Figma): {ctx["auto_layout"]["layout_mode"]}\n'

    return (
        'import { LitElement, html, css } from "lit";\n'
        'import { customElement, property } from "lit/decorators.js";\n\n'
        f'// from-spec skeleton (no baseline equivalent); axes: {sorted(spec.variant_axes) or "none"}\n'
        f'{layout_note}{text_comment}'
        f'@customElement("{tag}")\n'
        f'export class {cls} extends LitElement {{\n'
        f'{props_block}\n\n'
        f'    static styles = css`:host {{ {_host_style(slug, ctx.get("auto_layout"))} }}`;\n\n'
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


# Gold-standard reference (v0.2 Path-2): a representative excerpt of a REAL, complete helix-code
# organism (feature/organisms/MediaText — 310 LOC). Teaches the conventions Phase-5 shells lacked:
# JSDoc @element/@attr, typed @property unions with @attr mapping, @state + slotchange handlers,
# named-slot composition, token-based responsive CSS, per-variant render helpers, tagname map.
_GOLD_REFERENCE = '''\
import { LitElement, html, css } from "lit";
import { customElement, property, state } from "lit/decorators.js";
import type { MediaTextAlignment } from "./types.js";

/**
 * A full-width organism pairing a media element with a content block in
 * responsive layouts across all four design-system breakpoints.
 * @element hx-media-text
 * @attr {'right'|'left'|'large'} image-alignment - Image position / layout variant.
 * @slot heading-group - consumer projects an hx-heading-group here
 * @slot image - consumer projects an hx-image here
 */
@customElement("hx-media-text")
export class HxMediaText extends LitElement {
    /** Controls the image position and layout variant. @attr image-alignment */
    @property({ attribute: "image-alignment", reflect: true })
    imageAlignment: MediaTextAlignment = "right";

    @state() private _hasCaption = false;
    private _onCaptionSlotChange(e: Event) {
        const slot = e.target as HTMLSlotElement;
        this._hasCaption = slot.assignedNodes({ flatten: true }).length > 0;
    }

    static styles = css`
        :host { display: block; padding-block: calc(var(--helix-dimension-spacing-module-md, 56) * 1px); }
        .row { display: flex; gap: calc(var(--helix-dimension-spacing-components-md, 24) * 1px); }
    `;

    render() {
        return this.imageAlignment === "large" ? this._renderLarge() : this._renderSideBySide();
    }
    private _renderSideBySide() {
        return html`
            <div class="module-inner"><div class="row">
                <div class="content-col">
                    <slot name="heading-group"></slot>
                    <slot name="copy-group"></slot>
                    <slot name="button-group"></slot>
                </div>
                <div class="image-col"><slot name="image"></slot></div>
            </div></div>`;
    }
    private _renderLarge() { return html`<div class="module-inner"><slot></slot></div>`; }
}
declare global { interface HTMLElementTagNameMap { "hx-media-text": HxMediaText; } }
'''

_FROM_SPEC_INSTRUCTIONS = (
    "You generate a single Lit + TypeScript web component from a thin spec (no baseline exists). "
    "Output ONLY the component source. It MUST: import from 'lit' and 'lit/decorators.js'; use "
    "@customElement with the EXACT tag given; export a class extending LitElement named EXACTLY as "
    "given; use ONLY var(--{customer}-*) CSS custom properties (never --helix-); implement render(). "
    "\n\nMATCH THE CONVENTIONS in this gold-standard reference organism (adapt structure to the given "
    "component — do NOT copy its name/tag/tokens): a JSDoc header with @element and @attr/@slot tags; "
    "a typed @property union per variant axis with an @attr mapping; @state + slotchange handlers for "
    "internal state; NAMED SLOTS so consumers project the design system's atoms/molecules "
    "(heading-group, copy-group, button-group, image, etc.); token-based responsive CSS; and "
    "per-variant private render helpers when a layout axis warrants it. Aim for a genuinely functional "
    "component, not a bare wrapper — but do NOT fabricate behaviour the spec doesn't imply.\n\n"
    "When a FIGMA CONTEXT block is provided (real design data extracted from the source file), GROUND "
    "your output in it — do not ignore it and do not invent beyond it:\n"
    "  - text_samples: the real copy this component displays. Use it for default slot content / labels "
    "/ aria text where natural (keep it as light default content; consumers can override via slots).\n"
    "  - auto_layout: the real layout. Map layout_mode HORIZONTAL/VERTICAL to a flex row/column, and "
    "use item_spacing/padding as the gap/padding intent (prefer customer spacing tokens if provided in "
    "'Tokens available'; otherwise the numeric px are the design intent).\n"
    "  - property_definitions: the real component property SCHEMA. Emit a typed @property union per "
    "VARIANT property using its variant_options; add BOOLEAN/TEXT props with sensible @attr mappings.\n\n"
    "=== GOLD REFERENCE (convention only; different component) ===\n"
    f"{_GOLD_REFERENCE}\n=== END REFERENCE ==="
)


def _render_figma_context(ctx: Optional[dict]) -> str:
    """Compact, deterministic textual rendering of the distilled figma_context for the agent prompt.
    Empty string when there's no enrichment (BC-A → prompt is byte-identical to v0.2)."""
    if not ctx:
        return ""
    lines = ["\n=== FIGMA CONTEXT (real design data — use it) ==="]
    if ctx.get("auto_layout"):
        al = ctx["auto_layout"]
        pad = {k: al[k] for k in ("padding_top", "padding_right", "padding_bottom", "padding_left") if k in al}
        lines.append(f"auto_layout: mode={al.get('layout_mode')} item_spacing={al.get('item_spacing')} padding={pad or 'none'}")
    if ctx.get("property_definitions"):
        for name, d in ctx["property_definitions"].items():
            if isinstance(d, dict):
                opts = d.get("variant_options")
                lines.append(f"property: {name} type={d.get('type')}" + (f" options={opts}" if opts else ""))
    if ctx.get("text_samples"):
        lines.append("text_samples: " + " | ".join(str(t).replace('\n', ' ')[:60] for t in ctx["text_samples"]))
    lines.append("=== END FIGMA CONTEXT ===")
    return "\n".join(lines)


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
                  f"Tokens available: {spec.tokens_consumed}"
                  f"{_render_figma_context(spec.figma_context)}\nGenerate the Lit component.")
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
