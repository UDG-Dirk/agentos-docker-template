"""Tests for Pathway B traversal + Composition-mode extractor + per-client orchestration
(spec: composition-mode-extractor-and-pathway-b, Path C rev.3.1 §5). Deterministic + offline;
bounded live smokes (skipif no PAT) against Helix_Modules + Core.
"""
from __future__ import annotations

import asyncio
import copy
import os

import httpx
import pytest

import agents.figma_extractor.composition_mode as cm
import agents.figma_extractor.pathway_b_traversal as pb

CORE = "8qPSyetzviLR6eF6bkpL44"
MODULES = "qMi5B9YeqAf9Ik1yN6erw4"


# ---- Pathway B traversal (mocked fetchers) ---------------------------------
def _page_tree(page_id, page_name, frames):
    """Build a page wrap {document, components} with `frames` = list of (id,name,type)."""
    children = [{"id": i, "name": n, "type": t, "children": []} for i, n, t in frames]
    return {"document": {"id": page_id, "name": page_name, "type": "CANVAS", "children": children},
            "components": {}}


def _run_pb(pages, node_map, **kw):
    async def fp(fk):
        return pages

    async def fn(fk, nid, depth):
        return node_map[nid]
    return asyncio.run(pb.run_pathway_b("f", fetch_pages=fp, fetch_node=fn, **kw))


def test_pathway_b_traverses_pages_in_order_with_frames_and_remotes():
    pages = [{"id": "p1", "name": "MediaText"}, {"id": "p2", "name": "Header"}]
    n = {
        "p1": {"document": {"id": "p1", "name": "MediaText", "type": "CANVAS",
                            "children": [{"id": "10:1", "name": "Sec", "type": "SECTION",
                                          "children": [{"id": "10:2", "name": "Card", "type": "INSTANCE",
                                                        "children": []}]}]},
               "components": {"10:2": {"remote": True, "key": "kCard", "name": "Card"}}},
        "p2": {"document": {"id": "p2", "name": "Header", "type": "CANVAS",
                            "children": [{"id": "20:1", "name": "Nav", "type": "FRAME", "children": []}]},
               "components": {"20:1x": {"remote": True, "key": "kNav", "name": "Nav"}}},
    }
    r = _run_pb(pages, n)
    assert r["pathway_b_status"] == "success"
    assert [p["page_name"] for p in r["pages"]] == ["MediaText", "Header"]  # page order preserved
    assert r["frame_count_total"] == 3  # Sec+Card + Nav
    assert r["remote_reference_count"] == 2
    keys = {ref["key"] for ref in r["remote_references"]}
    assert keys == {"kCard", "kNav"}
    # anti-fabrication: every remote ref carries source page + node
    for ref in r["remote_references"]:
        assert ref["source_page_name"] in ("MediaText", "Header") and ref["source_node_id"]


def test_pathway_b_empty_file_is_partial():
    r = _run_pb([], {})
    assert r["pathway_b_status"] == "partial"
    assert any(f["error_class"] == "empty_composition_file" for f in r["failure_reports"])


def test_pathway_b_file_inaccessible_is_failure():
    async def fp_boom(fk):
        raise RuntimeError("403")
    r = asyncio.run(pb.run_pathway_b("f", fetch_pages=fp_boom, fetch_node=lambda *a: None))
    assert r["pathway_b_status"] == "failure"
    assert any(f["error_class"] == "file_inaccessible" for f in r["failure_reports"])


def test_pathway_b_depth_bound_fires():
    # deeply nested chain beyond max_depth=3
    deep = {"id": "d0", "name": "d0", "type": "FRAME", "children": []}
    cur = deep
    for i in range(1, 8):
        child = {"id": f"d{i}", "name": f"d{i}", "type": "FRAME", "children": []}
        cur["children"].append(child)
        cur = child
    node_map = {"p1": {"document": {"id": "p1", "name": "P", "type": "CANVAS", "children": [deep]},
                       "components": {}}}
    r = _run_pb([{"id": "p1", "name": "P"}], node_map, max_depth=3)
    assert r["pathway_b_status"] == "partial"
    assert any(f["error_class"] == "traversal_depth_exceeded" for f in r["failure_reports"])


def test_pathway_b_deterministic():
    pages = [{"id": "p1", "name": "A"}]
    n = {"p1": _page_tree("p1", "A", [("1", "F1", "FRAME"), ("2", "F2", "FRAME")])}
    a = _run_pb(pages, n)
    b = _run_pb(pages, n)
    for r in (a, b):
        r["extracted_at"] = "T"
    assert a == b


# ---- composition-mode extractor (mocked pathway_b + resolve) ---------------
def _fake_pb_result(refs, status="success"):
    async def f(fk):
        return {"pathway_b_status": status, "composition_tree": [{"page_id": "p1", "page_name": "P", "frames": []}],
                "pages": [{"page_id": "p1", "page_name": "P", "frame_count": 0, "remote_ref_count": len(refs),
                           "local_component_count": 0, "depth_bounded": False}],
                "page_count": 1, "frame_count_total": 0, "remote_reference_count": len(refs),
                "remote_references": refs, "failure_reports": [], "gaps": []}
    return f


def _fake_resolve(resolved, unresolved):
    async def f(fk, refs, libs):
        events = [{"event_type": "resolution_started"}]
        events += [{"event_type": "resolved_reference", "reference_key": k} for k in resolved]
        events += [{"event_type": "unresolved_reference", "reference_key": k} for k in unresolved]
        summary = {"references_total": len(resolved) + len(unresolved), "references_resolved": len(resolved),
                   "references_unresolved": len(unresolved), "resolution_rate_pct": 0.0}
        events.append({"event_type": "resolution_complete", "resolution_summary": summary})
        return {"events": events, "resolution_complete": events[-1]}
    return f


def test_composition_mode_orchestrates_pathway_b_and_lane6():
    refs = [{"key": "kA", "source_node_id": "1", "source_page_name": "P"},
            {"key": "kB", "source_node_id": "2", "source_page_name": "P"}]
    r = asyncio.run(cm.run_composition_extraction(
        MODULES, registered_libraries=[{"file_key": CORE, "role": "core_foundation", "priority_order": 0}],
        pathway_b=_fake_pb_result(refs), resolve=_fake_resolve(["kA"], ["kB"]), now=lambda: "T"))
    assert r["extraction_mode"] == "composition" and r["file_role"] == "client"
    assert r["remote_reference_count"] == 2
    assert r["resolution_summary"]["references_resolved"] == 1
    assert r["status"] == "partial"  # one unresolved
    assert "component_sets" not in r  # Lane 2 NOT part of composition mode
    assert r["reconciliation_contract"]["policy_name"] == "authoritative_catalog_with_usage_evidence_fallback"
    assert r["provenance"]["llm_involvement"] == "none"


def test_composition_mode_emit_callback_receives_events_in_order():
    refs = [{"key": "kA", "source_node_id": "1", "source_page_name": "P"}]
    seen = []

    async def stub_fetch(lib_key):  # offline library map so the real resolve_stream runs + emits
        return {"lastModified": "L", "map": {"kA": {"name": "A", "node_id": "1:1", "kind": "component"}},
                "source": "fresh_fetch"}
    # a library IS registered -> full composition mode -> real resolve_stream emits through `emit`
    r = asyncio.run(cm.run_composition_extraction(
        MODULES, registered_libraries=[{"file_key": "lib", "role": "core_foundation", "priority_order": 0}],
        pathway_b=_fake_pb_result(refs), fetch_library_map=stub_fetch, emit=seen.append, now=lambda: "T"))
    types = [e["event_type"] for e in seen]
    assert types[0] == "resolution_started" and types[-1] == "resolution_complete"
    assert r["resolution_events"] == seen  # emitted == collected, same order
    assert r["extraction_mode"] == "composition"  # library registered -> NOT composition-only


# ---- per-client orchestration (mocked core + composition) ------------------
def _fake_core(val="core"):
    async def f(core_key):
        return {"deterministic_extraction": {"status": "success"}, "marker": val}
    return f


def _fake_comp():
    async def f(client_key, registered):
        return {"extraction_mode": "composition", "registered_seen": registered, "status": "success"}
    return f


def test_per_client_unifies_core_and_composition_with_registered_libs():
    r = asyncio.run(cm.extract_client_design_system(
        CORE, MODULES, additional_library_keys=["lib3"], core_last_modified="L",
        core_extractor=_fake_core(), composition_extractor=_fake_comp(), use_core_cache=False, now=lambda: "T"))
    assert r["workflow"] == "extract_client_design_system"
    assert r["core_extraction"]["marker"] == "core"
    reg = r["registered_libraries"]
    assert reg[0]["file_key"] == CORE and reg[0]["priority_order"] == 0
    assert reg[1]["file_key"] == "lib3" and reg[1]["role"] == "additional_library"
    assert r["client_extraction"]["registered_seen"] == reg  # Core + additional passed to composition


def test_core_extraction_cache_hit_and_miss():
    cm._CORE_CACHE.clear()
    calls = {"n": 0}

    async def counting_core(core_key):
        calls["n"] += 1
        return {"marker": calls["n"]}

    # first run: miss (lastModified L1)
    r1 = asyncio.run(cm.extract_client_design_system(CORE, MODULES, core_last_modified="L1",
                     core_extractor=counting_core, composition_extractor=_fake_comp(), now=lambda: "T"))
    # second run: same lastModified -> cache HIT, core_extractor NOT called again
    r2 = asyncio.run(cm.extract_client_design_system(CORE, MODULES, core_last_modified="L1",
                     core_extractor=counting_core, composition_extractor=_fake_comp(), now=lambda: "T"))
    # third run: lastModified advances -> cache MISS, re-extract
    r3 = asyncio.run(cm.extract_client_design_system(CORE, MODULES, core_last_modified="L2",
                     core_extractor=counting_core, composition_extractor=_fake_comp(), now=lambda: "T"))
    assert r1["core_extraction_cache"] == "miss" and r2["core_extraction_cache"] == "hit"
    assert r3["core_extraction_cache"] == "miss"
    assert calls["n"] == 2  # core extracted twice (L1 once, L2 once), reused on the hit


def test_per_client_cross_run_identical():
    def run():
        return asyncio.run(cm.extract_client_design_system(
            CORE, MODULES, core_last_modified="L", core_extractor=_fake_core(),
            composition_extractor=_fake_comp(), use_core_cache=False, now=lambda: "T"))
    assert run() == run()


# ---- live smokes (bounded; need FIGMA_PAT) ---------------------------------
_HAS_PAT = bool(os.environ.get("FIGMA_PAT") or os.environ.get("FIGMA_API_KEY"))


@pytest.mark.skipif(not _HAS_PAT, reason="FIGMA_PAT not in env — live smoke skipped")
def test_live_smoke_composition_extraction_modules():
    """Bounded live: real Pathway B on 2 Modules pages + real Lane 6 vs Core."""
    import httpx as _hx

    async def go():
        async with _hx.AsyncClient(headers={"X-Figma-Token": os.environ.get("FIGMA_PAT") or os.environ.get("FIGMA_API_KEY")},
                                   timeout=30) as c:
            async def fp(_fk):
                return [{"id": "2:4", "name": "C_001_MediaText"}, {"id": "86:4379", "name": "C_002_HeroTeaser"}]
            pbres = await pb.run_pathway_b(MODULES, client=c, fetch_pages=fp)
            return await cm.run_composition_extraction(
                MODULES, registered_libraries=[{"file_key": CORE, "role": "core_foundation", "priority_order": 0}],
                client=c, pathway_b=lambda _fk: _wrap(pbres))
    r = asyncio.run(go())
    assert r["extraction_mode"] == "composition"
    assert r["remote_reference_count"] >= 8  # MediaText ~9 + HeroTeaser ~2
    assert r["resolution_summary"]["references_resolved"] >= 6  # ~7/9 resolve to Core
    assert r["provenance"]["llm_involvement"] == "none"


async def _wrap(v):
    return v


# ---- Pathway B 429 backoff (task pathway-b-429-backoff-hardening) ----------
def test_get_with_backoff_retries_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(pb, "_BACKOFF", (0.0, 0.0, 0.0))  # no real sleeping
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(429 if calls["n"] < 3 else 200, json={"ok": True})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await pb._get_with_backoff(c, "https://x/y", {})
    r = asyncio.run(go())
    assert calls["n"] == 3 and r.status_code == 200  # retried past two 429s


def test_get_with_backoff_exhausts_and_raises(monkeypatch):
    monkeypatch.setattr(pb, "_BACKOFF", (0.0, 0.0))

    def handler(req):
        return httpx.Response(429, text="Too Many Requests")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await pb._get_with_backoff(c, "https://x/y", {})
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(go())  # exhausted -> raises -> caller records malformed_node_data (fail-loud)


def test_run_pathway_b_recovers_page_from_429(monkeypatch):
    monkeypatch.setattr(pb, "_BACKOFF", (0.0, 0.0, 0.0))
    node_calls = {"n": 0}

    def handler(req):
        path = req.url.path
        if path.endswith("/files/F"):  # pages fetch (depth=1)
            return httpx.Response(200, json={"document": {"id": "d", "name": "doc", "type": "DOCUMENT",
                                                          "children": [{"id": "p1", "name": "P1"}]}})
        if path.endswith("/nodes"):
            node_calls["n"] += 1
            if node_calls["n"] < 3:  # 429 twice, then 200
                return httpx.Response(429, text="Too Many Requests")
            return httpx.Response(200, json={"nodes": {"p1": {"document": {
                "id": "p1", "name": "P1", "type": "CANVAS",
                "children": [{"id": "1", "name": "F", "type": "FRAME", "children": []}]}, "components": {}}}})
        return httpx.Response(404)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await pb.run_pathway_b("F", client=c)
    r = asyncio.run(go())
    assert r["pathway_b_status"] == "success"  # page recovered via backoff, NOT failed
    assert r["page_count"] == 1 and r["frame_count_total"] == 1
    assert node_calls["n"] == 3 and not r["failure_reports"]


@pytest.mark.skipif(not _HAS_PAT, reason="FIGMA_PAT not in env — live smoke skipped")
def test_live_smoke_per_client_end_to_end():
    """End-to-end orchestration with a STUBBED Core extractor (Core proven elsewhere; keeps smoke bounded)
    + REAL composition extraction + REAL Lane 6 against Modules->Core."""
    async def stub_core(core_key):
        return {"deterministic_extraction": {"status": "success", "note": "stubbed for bounded smoke"}}

    async def bounded_comp(client_key, registered):
        import httpx as _hx
        async with _hx.AsyncClient(headers={"X-Figma-Token": os.environ.get("FIGMA_PAT") or os.environ.get("FIGMA_API_KEY")},
                                   timeout=30) as c:
            async def fp(_fk):
                return [{"id": "2:4", "name": "C_001_MediaText"}]
            pbres = await pb.run_pathway_b(client_key, client=c, fetch_pages=fp)
            return await cm.run_composition_extraction(client_key, registered_libraries=registered,
                                                       client=c, pathway_b=lambda _fk: _wrap(pbres))
    r = asyncio.run(cm.extract_client_design_system(
        CORE, MODULES, core_last_modified="smoke", core_extractor=stub_core,
        composition_extractor=bounded_comp, use_core_cache=False))
    assert r["client_extraction"]["resolution_summary"]["references_resolved"] >= 6
    assert r["registered_libraries"][0]["file_key"] == CORE
