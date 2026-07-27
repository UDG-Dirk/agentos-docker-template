"""Tests for the deterministic Figma extractor (spec figma-extractor-deterministic-v0-1, §8).

Contract (shape + DETERMINISM) + anti-fabrication + partial-failure + typo + Normalizer-coercion,
all deterministic with mocked lanes/Framelink (no PAT/network). Plus a live smoke (skipif no PAT).
"""
from __future__ import annotations

import asyncio
import copy
import os
import sys

import pytest

import agents.figma_extractor.deterministic as d

FILE_KEY = "8qPSyetzviLR6eF6bkpL44"

_GFD_YAML = """
metadata:
  name: Helix_Core-Library
  components:
    57:760: {id: 57:760, name: 'Variant=SolidButton, Size=lg, State=Default', componentSetId: 57:766}
    79:475: {id: 79:475, name: 'Variant=SolidButton, Size=md, State=Hover', componentSetId: 57:766}
globalVars:
  styles:
    fill_30US4P: ['#1971C2']
    fill_X5JMNH: ['#8A38F5']
    layout_WK1G9O: {mode: row, gap: 8px}
    text_ABC: {fontFamily: Roboto, fontSize: 16}
"""


async def _meta(fk):
    return {"payload": {"version": "v1", "last_touched_at": "2026-07-27T00:00:00Z"}, "failure_report": None}


def _sem(status="success", with_typo_set=True, fail=None):
    sets = [{"node_id": "57:766", "name": "Button", "key": "k", "description": "Purpose: button",
             "description_rt": None, "containing_frame": {}}]
    if with_typo_set:
        sets.append({"node_id": "9:9", "name": "Drodown", "key": "k2", "description": None})

    async def sem(fk):
        return {"status": status, "component_sets": sets, "components": [{"name": "Variant=x"}] * 3,
                "styles": [{"name": "shadow/sm", "style_type": "EFFECT", "key": "sk", "node_id": "21:100"}],
                "failure_reports": (fail or [])}
    return sem


async def _binding(fk, ids):
    return {"bindings": {"57:766": {"property_bindings": {"fills[0]": {"variable_id": "VariableID:1:2"}}}},
            "failure_report": None}


async def _gfd(fk, nid):
    return _GFD_YAML


async def _dl(fk, nodes):
    return "Downloaded"


def _run(**over):
    kw = dict(lane_meta=_meta, lane_semantic=_sem(), lane_binding=_binding,
              get_figma_data=_gfd, download_images=_dl, do_assets=False)
    kw.update(over)
    return asyncio.run(d.run_deterministic_extraction(FILE_KEY, **kw))


# ---- contract: shape + determinism ----------------------------------------
def test_output_is_fer_shaped_with_section5_nested():
    r = _run()
    for k in ("file_key", "pages_discovered", "tokens", "components", "assets",
              "typos_detected", "enrichment_coverage"):
        assert k in r, f"missing FER field {k}"
    de = r["deterministic_extraction"]
    assert de["status"] == "success"
    assert de["provenance"]["llm_involvement"] == "none"
    assert de["provenance"]["extraction_pipeline"] == "figma-extractor-deterministic-v0-1"
    assert de["coverage_report"]["component_sets_expected"] == 2


def test_deterministic_same_input_same_output():
    def scrub(x):
        y = copy.deepcopy(x)
        y["deterministic_extraction"]["extracted_at"] = "T"
        return y
    assert scrub(_run()) == scrub(_run())  # bulletproof: identical modulo timestamp


def test_tokens_distilled_deterministically_from_globalvars():
    r = _run()
    toks = {t["style_id"]: t for t in r["tokens"]}
    assert toks["fill_30US4P"]["value"] == "#1971C2" and toks["fill_30US4P"]["category"] == "color"
    assert "shadow/sm" in {t["name"] for t in r["tokens"]}  # Lane 2 named style token


def test_variants_parsed_from_metadata():
    r = _run()
    btn = next(c for c in r["components"] if c["name"] == "Button")
    assert len(btn["variants"]) == 2
    assert {v["state"] for v in btn["variants"]} == {"Default", "Hover"}


# ---- anti-fabrication: every emission traces to a mock payload -------------
def test_anti_fabrication_all_emissions_traceable():
    r = _run()
    gv_ids = {"fill_30US4P", "fill_X5JMNH", "layout_WK1G9O", "text_ABC"}
    style_names = {"shadow/sm"}
    for t in r["tokens"]:
        assert (t["style_id"] in gv_ids) or (t["name"] in style_names) or t["style_id"] in gv_ids \
            or t["name"].split("/")[0] in gv_ids, f"untraceable token {t}"
    roster_ids = {"57:766", "9:9"}
    for c in r["components"]:
        assert c["node_id"] in roster_ids, f"fabricated component node {c}"
    for typo in r["deterministic_extraction"]["typos_detected"]:
        assert typo["node_id"] in roster_ids or typo["node_id"] == "21:100"


# ---- typo detection (deterministic) ----------------------------------------
def test_typo_detection_flags_real_typo():
    r = _run()
    typos = r["deterministic_extraction"]["typos_detected"]
    assert any(t["original"] == "Drodown" and t["suggestion"] == "dropdown" for t in typos)
    assert all(t["detection_method"] == "levenshtein+dictionary" for t in typos)


def test_no_typo_when_clean():
    r = _run(lane_semantic=_sem(with_typo_set=False))
    assert r["deterministic_extraction"]["typos_detected"] == []


# ---- failure handling ------------------------------------------------------
def test_lane2_failure_is_status_failure():
    async def sem_fail(fk):
        return {"status": "failure", "component_sets": [], "components": [], "styles": [], "failure_reports": [
            {"endpoint": "getFileComponentSets", "error_class": "auth", "http_status": 401,
             "message": "x", "attempted_at": "t", "retry_count": 0}]}
    r = _run(lane_semantic=sem_fail)
    assert r["deterministic_extraction"]["status"] == "failure"
    assert r["deterministic_extraction"]["failure_reports"]
    assert r["components"] == []


def test_get_figma_data_error_is_partial_not_raise():
    async def gfd_boom(fk, nid):
        raise RuntimeError("boom")
    r = _run(get_figma_data=gfd_boom)
    de = r["deterministic_extraction"]
    assert de["status"] == "partial"  # roster ok, node query failed
    assert any(f["endpoint"] == "get_figma_data" for f in de["failure_reports"])
    assert de["coverage_report"]["component_sets_failed"]


# ---- 429 backoff on the real get_figma_data wrapper (spec §10.3) -----------
def test_default_gfd_retries_on_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(d, "_GFD_BACKOFF", (0.0, 0.0, 0.0))  # no real sleeping in tests
    calls = {"n": 0}

    class _Block:
        type = "text"

        def __init__(self, t):
            self.text = t

    class _Res:
        def __init__(self, t):
            self.content = [_Block(t)]

    class _Sess:
        async def call_tool(self, name, args):
            calls["n"] += 1
            return _Res("Fetch failed with status 429: Too Many Requests" if calls["n"] < 3 else _GFD_YAML)

    out = asyncio.run(d._default_get_figma_data(_Sess(), FILE_KEY, "57:766"))
    assert calls["n"] == 3 and "globalVars" in out  # retried past two 429s to real data


def test_default_gfd_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr(d, "_GFD_BACKOFF", (0.0, 0.0, 0.0))

    class _Block:
        type = "text"

        def __init__(self):
            self.text = "status 429"

    class _Res:
        def __init__(self):
            self.content = [_Block()]

    class _Sess:
        async def call_tool(self, name, args):
            return _Res()

    out = asyncio.run(d._default_get_figma_data(_Sess(), FILE_KEY, "57:766"))
    assert d._is_rate_limited(out)  # exhausted; returns last (still-429) text, does not hang/raise


# ---- Normalizer contract preservation (runtime coercion) -------------------
def test_normalizer_coerces_deterministic_output():
    r = _run()
    sys.path[:0] = ["agents/token_normalizer", "agents/figma_extractor"]
    from normalizer import FigmaExtractionResult as NF, normalize_tokens  # noqa: E402
    fer = NF(**r)  # extra=ignore drops 'deterministic_extraction'
    nt = normalize_tokens(fer)  # must not raise
    assert nt is not None


# ---- live smoke (real REST/Framelink; needs FIGMA_PAT) ---------------------
_HAS_PAT = bool(os.environ.get("FIGMA_PAT") or os.environ.get("FIGMA_API_KEY"))


@pytest.mark.skipif(not _HAS_PAT, reason="FIGMA_PAT not in env — live smoke skipped")
def test_live_smoke_deterministic():
    from agents.figma_extractor.agent import figma_mcp_tools

    async def go():
        async with figma_mcp_tools:
            return await d.run_deterministic_extraction(FILE_KEY, session=figma_mcp_tools.session, do_assets=False)
    r = asyncio.run(go())
    de = r["deterministic_extraction"]
    assert de["status"] in ("success", "partial"), f"failure_reports={de['failure_reports']}"
    assert de["provenance"]["llm_involvement"] == "none"
    assert de["coverage_report"]["component_sets_expected"] == 27  # Lane 2 authoritative roster
    # every emitted component traces to the Lane 2 roster (anti-fabrication, live)
    roster = {cs["node_id"] for cs in de["component_sets"]}
    assert all(c["node_id"] in roster for c in r["components"])
