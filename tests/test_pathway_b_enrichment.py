"""Track D v0.2.5 Phase 2a — Rank 1 (text_content) + Rank 2 (auto_layout) enrichment.

Unit-level tests over the pure extractors + `_walk_frames`, plus integration through
`run_pathway_b` (injected fetchers, no network). Backward-compat: nodes with neither text nor
auto-layout keep the exact v0.1 frame shape {id,name,type,depth}; new fields are additive (BC-A).
"""

from __future__ import annotations

import asyncio

from agents.figma_extractor import pathway_b_traversal as pb

# --------------------------------------------------------------------------- #
# Rank 1 — text_content (TEXT nodes)
# --------------------------------------------------------------------------- #


def test_text_content_extracted_with_style():
    node = {
        "id": "1",
        "name": "Heading",
        "type": "TEXT",
        "characters": "Hello world",
        "style": {
            "fontFamily": "Inter",
            "fontSize": 24.0,
            "fontWeight": 700,
            "lineHeightPx": 32.0,
            "lineHeightUnit": "PIXELS",
            "textAlignHorizontal": "LEFT",
        },
    }
    tc = pb._extract_text_content(node)
    assert tc == {
        "characters": "Hello world",
        "style": {
            "font_family": "Inter",
            "font_size": 24.0,
            "font_weight": 700,
            "line_height": {"unit": "PIXELS", "px": 32.0},
            "text_align": "LEFT",
        },
    }


def test_text_content_none_for_non_text_node():
    assert pb._extract_text_content({"type": "FRAME", "characters": "x"}) is None


def test_text_content_none_when_no_characters():
    assert pb._extract_text_content({"type": "TEXT", "characters": ""}) is None
    assert pb._extract_text_content({"type": "TEXT"}) is None


def test_text_content_characters_only_when_no_style():
    assert pb._extract_text_content({"type": "TEXT", "characters": "Bare"}) == {"characters": "Bare"}


# --------------------------------------------------------------------------- #
# Rank 2 — auto_layout (nodes with layoutMode)
# --------------------------------------------------------------------------- #


def test_auto_layout_extracted():
    node = {
        "type": "FRAME",
        "layoutMode": "HORIZONTAL",
        "paddingTop": 8,
        "paddingLeft": 16,
        "itemSpacing": 4,
        "primaryAxisSizingMode": "AUTO",
        "counterAxisAlignItems": "CENTER",
    }
    al = pb._extract_auto_layout(node)
    assert al == {
        "layout_mode": "HORIZONTAL",
        "padding_top": 8,
        "padding_left": 16,
        "item_spacing": 4,
        "primary_axis_sizing": "AUTO",
        "counter_axis_align": "CENTER",
    }


def test_auto_layout_none_when_mode_none_or_absent():
    assert pb._extract_auto_layout({"type": "FRAME", "layoutMode": "NONE"}) is None
    assert pb._extract_auto_layout({"type": "FRAME"}) is None


def test_auto_layout_layout_mode_always_present_even_if_no_padding():
    assert pb._extract_auto_layout({"type": "FRAME", "layoutMode": "VERTICAL"}) == {"layout_mode": "VERTICAL"}


# --------------------------------------------------------------------------- #
# _walk_frames emission + enrichment
# --------------------------------------------------------------------------- #


def _frames(doc):
    return [fr for fr, hit in pb._walk_frames(doc, "P", max_depth=20) if not hit]


def test_walk_emits_text_node_with_content():
    doc = {
        "id": "p",
        "type": "CANVAS",
        "children": [
            {"id": "t1", "name": "Label", "type": "TEXT", "characters": "Buy now", "style": {"fontSize": 14.0}}
        ],
    }
    frames = _frames(doc)
    assert len(frames) == 1
    assert frames[0]["type"] == "TEXT"
    assert frames[0]["text_content"] == {"characters": "Buy now", "style": {"font_size": 14.0}}


def test_walk_attaches_auto_layout_to_frame():
    doc = {
        "id": "p",
        "type": "CANVAS",
        "children": [
            {"id": "f1", "name": "Row", "type": "FRAME", "layoutMode": "HORIZONTAL", "itemSpacing": 8, "children": []}
        ],
    }
    frames = _frames(doc)
    assert frames[0]["auto_layout"] == {"layout_mode": "HORIZONTAL", "item_spacing": 8}


def test_walk_backward_compat_plain_frame_unchanged():
    # A frame with neither text nor auto-layout keeps the exact v0.1 shape (no extra keys).
    doc = {
        "id": "p",
        "type": "CANVAS",
        "children": [
            {
                "id": "s",
                "name": "Sec",
                "type": "SECTION",
                "children": [{"id": "c", "name": "Card", "type": "INSTANCE", "children": []}],
            }
        ],
    }
    frames = _frames(doc)
    assert frames == [
        {"id": "s", "name": "Sec", "type": "SECTION", "depth": 1},
        {"id": "c", "name": "Card", "type": "INSTANCE", "depth": 2},
    ]


def test_walk_nested_text_under_autolayout_both_surface():
    doc = {
        "id": "p",
        "type": "CANVAS",
        "children": [
            {
                "id": "f",
                "name": "Btn",
                "type": "COMPONENT",
                "layoutMode": "HORIZONTAL",
                "paddingTop": 12,
                "children": [{"id": "t", "name": "T", "type": "TEXT", "characters": "OK"}],
            }
        ],
    }
    frames = _frames(doc)
    assert frames[0]["auto_layout"]["layout_mode"] == "HORIZONTAL"
    assert frames[0]["auto_layout"]["padding_top"] == 12
    assert frames[1]["text_content"] == {"characters": "OK"}


# --------------------------------------------------------------------------- #
# SP-6 per-node fail-loud on enrichment error
# --------------------------------------------------------------------------- #


def test_enrichment_error_flags_frame_and_fails_loud(monkeypatch):
    def boom(node):
        raise ValueError("bad style")

    monkeypatch.setattr(pb, "_extract_auto_layout", boom)

    async def fp(fk):
        return [{"id": "p1", "name": "P1"}]

    async def fn(fk, nid, d):
        return {
            "document": {
                "id": "p1",
                "name": "P1",
                "type": "CANVAS",
                "children": [{"id": "n1", "name": "N", "type": "FRAME", "children": []}],
            },
            "components": {},
        }

    r = asyncio.run(pb.run_pathway_b("f", fetch_pages=fp, fetch_node=fn))
    assert r["pathway_b_status"] == "partial"
    assert any(fr.get("error_class") == "malformed_node_data" for fr in r["failure_reports"])
    frame = r["composition_tree"][0]["frames"][0]
    assert "enrichment_error" in frame and "bad style" in frame["enrichment_error"]


# --------------------------------------------------------------------------- #
# Integration: enrichment flows through run_pathway_b end-to-end
# --------------------------------------------------------------------------- #


def test_run_pathway_b_enriched_tree_end_to_end():
    async def fp(fk):
        return [{"id": "p1", "name": "Organisms"}]

    async def fn(fk, nid, d):
        return {
            "document": {
                "id": "p1",
                "name": "Organisms",
                "type": "CANVAS",
                "children": [
                    {
                        "id": "10:1",
                        "name": "MediaText",
                        "type": "COMPONENT",
                        "layoutMode": "VERTICAL",
                        "itemSpacing": 24,
                        "children": [
                            {
                                "id": "10:2",
                                "name": "Title",
                                "type": "TEXT",
                                "characters": "Headline",
                                "style": {"fontSize": 32.0, "fontWeight": 700},
                            }
                        ],
                    }
                ],
            },
            "components": {},
        }

    r = asyncio.run(pb.run_pathway_b("f", fetch_pages=fp, fetch_node=fn))
    assert r["pathway_b_status"] == "success"
    frames = r["composition_tree"][0]["frames"]
    assert r["frame_count_total"] == 2
    comp = next(f for f in frames if f["type"] == "COMPONENT")
    txt = next(f for f in frames if f["type"] == "TEXT")
    assert comp["auto_layout"] == {"layout_mode": "VERTICAL", "item_spacing": 24}
    assert txt["text_content"]["characters"] == "Headline"
    assert txt["text_content"]["style"] == {"font_size": 32.0, "font_weight": 700}


# =========================================================================== #
# Phase 2b — Rank 3 (component/variant schema + values)
# =========================================================================== #

def test_component_property_definitions_on_component_set():
    node = {"type": "COMPONENT_SET", "componentPropertyDefinitions": {
        "Size": {"type": "VARIANT", "defaultValue": "md", "variantOptions": ["sm", "md", "lg"]},
        "Disabled": {"type": "BOOLEAN", "defaultValue": False},
        "Label#1:0": {"type": "TEXT", "defaultValue": "Button"}}}
    cpd = pb._extract_component_property_definitions(node)
    assert cpd == {
        "Size": {"type": "VARIANT", "default_value": "md", "variant_options": ["sm", "md", "lg"]},
        "Disabled": {"type": "BOOLEAN", "default_value": False},
        "Label#1:0": {"type": "TEXT", "default_value": "Button"}}


def test_component_property_definitions_accepts_standalone_component():
    node = {"type": "COMPONENT", "componentPropertyDefinitions": {"On": {"type": "BOOLEAN", "defaultValue": True}}}
    assert pb._extract_component_property_definitions(node) == {"On": {"type": "BOOLEAN", "default_value": True}}


def test_component_property_definitions_none_cases():
    assert pb._extract_component_property_definitions({"type": "INSTANCE", "componentPropertyDefinitions": {"x": {}}}) is None
    assert pb._extract_component_property_definitions({"type": "COMPONENT_SET"}) is None
    assert pb._extract_component_property_definitions({"type": "COMPONENT_SET", "componentPropertyDefinitions": {}}) is None


def test_component_properties_on_instance():
    node = {"type": "INSTANCE", "componentProperties": {
        "Size": {"type": "VARIANT", "value": "lg"},
        "Disabled": {"type": "BOOLEAN", "value": False}}}
    assert pb._extract_component_properties(node) == {
        "Size": {"type": "VARIANT", "value": "lg"},
        "Disabled": {"type": "BOOLEAN", "value": False}}


def test_component_properties_none_for_non_instance():
    assert pb._extract_component_properties({"type": "COMPONENT_SET", "componentProperties": {"x": {"value": 1}}}) is None
    assert pb._extract_component_properties({"type": "INSTANCE"}) is None


def test_variant_properties_on_instance():
    assert pb._extract_variant_properties({"type": "INSTANCE", "variantProperties": {"State": "Hover"}}) == {"State": "Hover"}
    assert pb._extract_variant_properties({"type": "INSTANCE"}) is None  # [U] TD-2 samples had none
    assert pb._extract_variant_properties({"type": "FRAME", "variantProperties": {"x": "y"}}) is None


def test_walk_attaches_rank3_to_component_set_and_instance():
    doc = {"id": "p", "type": "CANVAS", "children": [
        {"id": "cs", "name": "Button", "type": "COMPONENT_SET",
         "componentPropertyDefinitions": {"Size": {"type": "VARIANT", "variantOptions": ["sm", "lg"]}},
         "children": [
             {"id": "i", "name": "Button/lg", "type": "INSTANCE",
              "componentProperties": {"Size": {"type": "VARIANT", "value": "lg"}},
              "variantProperties": {"Size": "lg"}, "children": []}]}]}
    frames = _frames(doc)
    cs = next(f for f in frames if f["type"] == "COMPONENT_SET")
    inst = next(f for f in frames if f["type"] == "INSTANCE")
    assert cs["component_property_definitions"]["Size"]["variant_options"] == ["sm", "lg"]
    assert inst["component_properties"]["Size"] == {"type": "VARIANT", "value": "lg"}
    assert inst["variant_properties"] == {"Size": "lg"}


def test_rank3_does_not_leak_onto_plain_frames():
    # A plain FRAME never gets Rank 3 fields even if it spuriously carries the raw keys.
    doc = {"id": "p", "type": "CANVAS", "children": [
        {"id": "f", "name": "F", "type": "FRAME",
         "componentProperties": {"X": {"value": 1}}, "children": []}]}
    frames = _frames(doc)
    assert "component_properties" not in frames[0]
    assert frames[0] == {"id": "f", "name": "F", "type": "FRAME", "depth": 1}
