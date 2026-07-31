"""Track D v0.2.5 Phase 3 — 3d Path-B consumes the enriched composition_tree.

Deterministic (Mock generator; no LLM). Proves: distillation of enriched frames → figma_context;
the from-spec skeleton + agent prompt ground themselves in that context; the structural gate still
passes; Path-A fork logic is unchanged; and absent enrichment falls back to v0.2 behaviour (BC-A).
"""
from __future__ import annotations

from agents.component_code_generator import generate_component_code
from agents.component_code_generator.generation import (
    GenerationInput,
    MockGenerator,
    _render_figma_context,
    _render_from_spec,
    distill_element_context,
    run_structural_gate,
)

# --------------------------------------------------------------------------- #
# distill_element_context — enriched frames → compact context
# --------------------------------------------------------------------------- #

def test_distill_none_when_no_enrichment():
    assert distill_element_context(None) is None
    assert distill_element_context([]) is None
    assert distill_element_context([{"id": "1", "name": "F", "type": "FRAME", "depth": 1}]) is None


def test_distill_collects_text_layout_and_props():
    frames = [
        {"id": "r", "type": "COMPONENT_SET", "depth": 0,
         "component_property_definitions": {"Size": {"type": "VARIANT", "variant_options": ["sm", "lg"]}}},
        {"id": "f", "type": "FRAME", "depth": 1, "auto_layout": {"layout_mode": "VERTICAL", "item_spacing": 24}},
        {"id": "deep", "type": "FRAME", "depth": 3, "auto_layout": {"layout_mode": "HORIZONTAL"}},
        {"id": "t1", "type": "TEXT", "depth": 2, "text_content": {"characters": "Read more"}},
        {"id": "t2", "type": "TEXT", "depth": 2, "text_content": {"characters": "Read more"}},  # dup
        {"id": "t3", "type": "TEXT", "depth": 2, "text_content": {"characters": "Headline"}},
    ]
    ctx = distill_element_context(frames)
    assert ctx["text_samples"] == ["Read more", "Headline"]           # deduped, in order
    assert ctx["auto_layout"] == {"layout_mode": "VERTICAL", "item_spacing": 24}  # shallowest wins (depth 1)
    assert ctx["property_definitions"]["Size"]["variant_options"] == ["sm", "lg"]


def test_distill_text_bounded():
    frames = [{"type": "TEXT", "depth": 1, "text_content": {"characters": f"t{i}"}} for i in range(30)]
    assert len(distill_element_context(frames)["text_samples"]) == 12


# --------------------------------------------------------------------------- #
# _render_from_spec grounds in figma_context, still gate-passes
# --------------------------------------------------------------------------- #

def _spec(ctx=None):
    return GenerationInput(slot="Hero Teaser", customer_slug="acme",
                           variant_axes={"Size": ["sm", "lg"]}, figma_context=ctx)


def test_render_typed_union_from_property_schema():
    ctx = {"property_definitions": {"Size": {"type": "VARIANT", "variant_options": ["sm", "md", "lg"]}}}
    src = _render_from_spec(_spec(ctx))
    assert 'size: "sm" | "md" | "lg" = "sm";' in src
    assert run_structural_gate(src, customer_slug="acme", element_slug="hero-teaser").passed


def test_render_flex_host_from_auto_layout():
    ctx = {"auto_layout": {"layout_mode": "HORIZONTAL", "item_spacing": 8,
                           "padding_top": 4, "padding_right": 4, "padding_bottom": 4, "padding_left": 4}}
    src = _render_from_spec(_spec(ctx))
    assert "display: flex" in src and "flex-direction: row" in src and "gap: 8px" in src
    assert "padding: 4px 4px 4px 4px" in src
    assert run_structural_gate(src, customer_slug="acme", element_slug="hero-teaser").passed


def test_render_text_samples_as_comment():
    ctx = {"text_samples": ["Buy now", "Learn more"]}
    src = _render_from_spec(_spec(ctx))
    assert "design copy (Figma): Buy now | Learn more" in src


def test_render_backward_compat_no_context_is_plain_block():
    src = _render_from_spec(_spec(None))
    assert "display: block;" in src and "display: flex" not in src
    assert 'size = "sm";' in src  # bare property, no type union
    assert run_structural_gate(src, customer_slug="acme", element_slug="hero-teaser").passed


def test_mock_generator_with_context_passes_gate():
    ctx = {"auto_layout": {"layout_mode": "VERTICAL", "item_spacing": 16},
           "property_definitions": {"Size": {"type": "VARIANT", "variant_options": ["sm", "lg"]}}}
    res = MockGenerator().generate(_spec(ctx))
    assert run_structural_gate(res.source, customer_slug="acme", element_slug="hero-teaser").passed


# --------------------------------------------------------------------------- #
# _render_figma_context — agent-prompt block (empty when no enrichment: BC-A)
# --------------------------------------------------------------------------- #

def test_prompt_context_empty_without_enrichment():
    assert _render_figma_context(None) == ""
    assert _render_figma_context({}) == ""


def test_prompt_context_renders_all_ranks():
    block = _render_figma_context({
        "auto_layout": {"layout_mode": "VERTICAL", "item_spacing": 24},
        "property_definitions": {"Size": {"type": "VARIANT", "variant_options": ["sm", "lg"]}},
        "text_samples": ["Headline", "Body"]})
    assert "FIGMA CONTEXT" in block
    assert "mode=VERTICAL" in block and "item_spacing=24" in block
    assert "property: Size type=VARIANT options=['sm', 'lg']" in block
    assert "text_samples: Headline | Body" in block


# --------------------------------------------------------------------------- #
# Integration through generate_component_code (Mock) — enriched vs thin
# --------------------------------------------------------------------------- #

def _pathb_element(figma_meta):
    return {"slot": "HeroTeaser", "baseline_ref": None, "derivation": "customer_passthrough",
            "confidence": "medium", "figma_meta": figma_meta}


def test_pathb_consumes_enriched_frames_end_to_end(tmp_path):
    frames = [
        {"type": "COMPONENT_SET", "depth": 0,
         "component_property_definitions": {"Layout": {"type": "VARIANT", "variant_options": ["a", "b"]}}},
        {"type": "FRAME", "depth": 1, "auto_layout": {"layout_mode": "HORIZONTAL", "item_spacing": 12}},
        {"type": "TEXT", "depth": 2, "text_content": {"characters": "Get started"}},
    ]
    env = generate_component_code(
        customer_slug="acme", scope="msq-dx",
        elements=[_pathb_element({"variant_names": ["Layout=a", "Layout=b"], "frames": frames})],
        timestamp="T", output_dir=str(tmp_path))
    assert env.summary.from_spec == 1 and env.summary.gate_passed == 1
    # the emitted source reflects the enriched context
    ts = next(p for p in tmp_path.rglob("*.ts") if "heroteaser" in p.name.lower())
    src = ts.read_text()
    assert "flex-direction: row" in src and "gap: 12px" in src
    assert "design copy (Figma): Get started" in src


def test_pathb_without_enrichment_still_generates_bc_a():
    env = generate_component_code(
        customer_slug="acme", scope="msq-dx",
        elements=[_pathb_element({"variant_names": ["Size=sm", "Size=lg"]})],  # no frames → no context
        timestamp="T", output_dir=None)
    assert env.summary.from_spec == 1 and env.summary.gate_passed == 1


def test_path_a_fork_unchanged_by_phase3():
    # A baseline-matched element still forks deterministically (Path-A untouched).
    valid_lit = ('import { LitElement, html } from "lit";\n'
                 'import { customElement } from "lit/decorators.js";\n'
                 '@customElement("hx-hero")\n'
                 'export class HxHero extends LitElement { render() { return html`<slot></slot>`; } }\n')
    reader = lambda ref, root: (valid_lit, "master:packages/elements/src/organisms/Hero/Hero.ts")
    env = generate_component_code(
        customer_slug="acme", scope="msq-dx",
        elements=[{"slot": "Hero", "baseline_ref": "Hero", "derivation": "forked_from_baseline",
                   "confidence": "high"}],
        source_reader=reader, timestamp="T")
    assert env.summary.fork_deterministic == 1
    assert env.provenance.elements[0].path == "fork_deterministic"
