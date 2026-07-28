"""Tests for Lane 6 — Cross-File Library Resolution (spec v0.1.1 §8, streaming-aware).

Deterministic + offline (mocked library maps via injected fetch). Streaming order + content verified.
A single live smoke (skipif no PAT) resolves Helix_Modules' MediaText remote refs against Core.
"""
from __future__ import annotations

import asyncio
import copy
import os

import httpx
import pytest

import agents.figma_extractor.cross_file_resolution as cfr

COMP = "comp_file_key"
CORE = "core_key"
LIB2 = "lib2_key"

# library maps keyed by component `key`
_MAPS = {
    CORE: {"kButton": {"name": "Button", "node_id": "57:766", "kind": "component_set"},
           "kCard": {"name": "Card", "node_id": "80:1", "kind": "component_set"},
           "kDup": {"name": "CoreDup", "node_id": "1:1", "kind": "component"}},
    LIB2: {"kIcon": {"name": "Icon", "node_id": "9:9", "kind": "component"},
           "kDup": {"name": "Lib2Dup", "node_id": "2:2", "kind": "component"}},
}


def _fetch(maps=None, last="L0"):
    maps = maps or _MAPS

    async def f(lib_key):
        if lib_key not in maps:
            raise RuntimeError(f"library {lib_key} inaccessible")
        return {"lastModified": last, "map": maps[lib_key], "source": "fresh_fetch"}
    return f


def _libs(*keys):
    roles = {0: "core_foundation"}
    return [{"file_key": k, "role": roles.get(i, "additional_library"), "priority_order": i}
            for i, k in enumerate(keys)]


def _run(refs, libs, **kw):
    kw.setdefault("now", lambda: "T")
    kw.setdefault("fetch_library_map", _fetch())

    async def go():
        return [e async for e in cfr.resolve_stream(COMP, refs, libs, **kw)]
    return asyncio.run(go())


def _types(events):
    return [e["event_type"] for e in events]


# ---- streaming contract ----------------------------------------------------
def test_full_stream_in_order_all_types():
    refs = [{"key": "kButton", "source_node_id": "10:1", "source_page_name": "MediaText"},
            {"key": "kCard", "source_node_id": "10:2", "source_page_name": "MediaText"}]
    ev = _run(refs, _libs(CORE))
    t = _types(ev)
    assert t[0] == "resolution_started" and t[-1] == "resolution_complete"
    assert "library_registered" in t
    assert t.count("resolved_reference") == 2
    started = ev[0]
    assert started["references_total"] == 2 and started["streaming_deterministic_order"] == "page_then_depth_first"
    comp = ev[-1]["resolution_summary"]
    assert comp["references_resolved"] == 2 and comp["resolution_rate_pct"] == 100.0
    # sequence numbers monotonic across resolved/unresolved
    seqs = [e["sequence"] for e in ev if "sequence" in e]
    assert seqs == sorted(seqs) == [1, 2]


def test_partial_library_yields_unresolved_and_partial_complete():
    refs = [{"key": "kButton", "source_node_id": "1", "source_page_name": "P"},
            {"key": "kMissing", "source_node_id": "2", "source_page_name": "P"}]
    ev = _run(refs, _libs(CORE))
    assert _types(ev).count("resolved_reference") == 1
    unres = [e for e in ev if e["event_type"] == "unresolved_reference"]
    assert len(unres) == 1 and unres[0]["reference_key"] == "kMissing"
    assert unres[0]["checked_libraries"] == [CORE]
    assert ev[-1]["resolution_summary"]["references_unresolved"] == 1
    assert ev[-1]["resolution_summary"]["resolution_rate_pct"] == 50.0


def test_registration_order_priority_and_collision():
    refs = [{"key": "kDup", "source_node_id": "1", "source_page_name": "P"}]
    ev = _run(refs, _libs(CORE, LIB2))  # both have kDup; Core priority 0
    coll = [e for e in ev if e["event_type"] == "library_key_collision"]
    assert len(coll) == 1 and coll[0]["resolved_via"] == CORE
    resolved = [e for e in ev if e["event_type"] == "resolved_reference"][0]
    assert resolved["target_library_file"] == CORE and resolved["target_component_name"] == "CoreDup"


def test_library_registration_failed_others_continue():
    refs = [{"key": "kIcon", "source_node_id": "1", "source_page_name": "P"}]
    ev = _run(refs, _libs("MISSING_LIB", LIB2))  # first lib fails, second registers
    assert any(e["event_type"] == "library_registration_failed" and e["file_key"] == "MISSING_LIB" for e in ev)
    assert any(e["event_type"] == "library_registered" and e["file_key"] == LIB2 for e in ev)
    assert any(e["event_type"] == "resolved_reference" for e in ev)  # kIcon resolves via LIB2


# ---- cycle detection (bounded direct + one-hop) ----------------------------
def test_library_dependency_cycle_detected_no_infinite_loop():
    refs = [{"key": "kButton", "source_node_id": "1", "source_page_name": "P"}]
    ev = _run(refs, _libs(CORE, LIB2), library_edges={CORE: [LIB2], LIB2: [CORE]})
    cyc = [e for e in ev if e["event_type"] == "library_dependency_cycle"]
    assert len(cyc) == 1 and cyc[0]["cycle_path"] == sorted([CORE, LIB2]) + [sorted([CORE, LIB2])[0]]
    assert cyc[0]["action"] == "cycle_broken_at_second_visit"
    assert any(e["event_type"] == "resolved_reference" for e in ev)  # other refs still resolve


# ---- UNBOUNDED N scaling ---------------------------------------------------
@pytest.mark.parametrize("n", [2, 5, 10, 20, 50])
def test_unbounded_n_libraries_all_resolve(n):
    maps = {f"lib{i}": {f"k{i}": {"name": f"C{i}", "node_id": f"{i}:0", "kind": "component"}} for i in range(n)}
    libs = _libs(*[f"lib{i}" for i in range(n)])
    refs = [{"key": f"k{i}", "source_node_id": f"s{i}", "source_page_name": "P"} for i in range(n)]
    ev = _run(refs, libs, fetch_library_map=_fetch(maps))
    summ = ev[-1]["resolution_summary"]
    assert summ["references_resolved"] == n and summ["references_total"] == n
    assert len(summ["per_library_metrics"]) == n
    assert sum(m["references_resolved"] for m in summ["per_library_metrics"].values()) == n


# ---- third-library-suspect clustering (>=3) --------------------------------
def test_third_library_suspect_fires_at_threshold():
    refs = [{"key": f"m{i}", "name": f"Foo=variant{i}", "source_node_id": str(i), "source_page_name": "P"}
            for i in range(3)]  # 3 unresolved sharing name prefix 'Foo'
    ev = _run(refs, _libs(CORE))
    sus = [e for e in ev if e["event_type"] == "third_library_suspect"]
    assert len(sus) == 1 and len(sus[0]["unresolved_references_in_cluster"]) == 3
    assert sus[0]["sensitivity_threshold_met"] == ">=3 clustered keys"


def test_no_suspect_below_threshold():
    refs = [{"key": "m1", "name": "Foo=a", "source_node_id": "1", "source_page_name": "P"},
            {"key": "m2", "name": "Bar=b", "source_node_id": "2", "source_page_name": "P"}]
    ev = _run(refs, _libs(CORE))
    assert not any(e["event_type"] == "third_library_suspect" for e in ev)


# ---- anti-fabrication ------------------------------------------------------
def test_anti_fabrication_resolved_trace_and_unresolved_exhaustive():
    refs = [{"key": "kButton", "source_node_id": "1", "source_page_name": "P"},
            {"key": "kGhost", "source_node_id": "2", "source_page_name": "P"}]
    ev = _run(refs, _libs(CORE, LIB2))
    for e in ev:
        if e["event_type"] == "resolved_reference":
            tgt = _MAPS[e["target_library_file"]][e["reference_key"]]
            assert e["target_component_id"] == tgt["node_id"] and e["target_component_name"] == tgt["name"]
        if e["event_type"] == "unresolved_reference":
            assert set(e["checked_libraries"]) == {CORE, LIB2}  # exhausted ALL registered libs


def test_zero_references_immediate_complete():
    ev = _run([], _libs(CORE))
    assert _types(ev)[0] == "resolution_started" and _types(ev)[-1] == "resolution_complete"
    c = ev[-1]["resolution_summary"]
    assert c["references_total"] == 0 and c["resolution_rate_pct"] == 100.0
    assert not any(e["event_type"] in ("resolved_reference", "unresolved_reference") for e in ev)


# ---- cross-run consistency (streaming: identical order + content) ----------
def test_cross_run_identical_stream():
    refs = [{"key": "kButton", "source_node_id": "1", "source_page_name": "P"},
            {"key": "kMiss", "source_node_id": "2", "source_page_name": "P"},
            {"key": "kCard", "source_node_id": "3", "source_page_name": "P"}]
    a = _run(refs, _libs(CORE))
    b = _run(refs, _libs(CORE))
    assert a == b  # now() is constant → byte-identical event stream in identical order


def test_dedup_by_key_preserves_first_order():
    refs = [{"key": "kButton", "source_node_id": "1", "source_page_name": "P"},
            {"key": "kButton", "source_node_id": "9", "source_page_name": "P2"},  # dup key
            {"key": "kCard", "source_node_id": "3", "source_page_name": "P"}]
    ev = _run(refs, _libs(CORE))
    assert ev[0]["references_total"] == 2  # deduped
    resolved = [e for e in ev if e["event_type"] == "resolved_reference"]
    assert [r["source_node_id"] for r in resolved] == ["1", "3"]  # first occurrence kept


# ---- batch bridge (backward-compat §14) ------------------------------------
def test_internal_link_reference_classified_not_resolved():
    refs = [{"key": "link:1->2", "source_node_id": "1", "source_page_name": "Header",
             "ref_type": "internal_link", "link_kind": "reaction", "destination": "2"},
            {"key": "kButton", "source_node_id": "3", "source_page_name": "P"}]  # component -> resolves
    ev = _run(refs, _libs(CORE))
    il = [e for e in ev if e["event_type"] == "internal_link_reference"]
    assert len(il) == 1 and il[0]["reference_key"] == "link:1->2" and il[0]["link_kind"] == "reaction"
    assert not any(e["event_type"] == "unresolved_reference" for e in ev)  # link NOT counted unresolved
    assert any(e["event_type"] == "resolved_reference" for e in ev)         # kButton resolved
    s = ev[-1]["resolution_summary"]
    assert s["internal_link_references"] == 1 and s["references_resolved"] == 1 and s["references_unresolved"] == 0
    assert s["resolution_rate_pct"] == 100.0  # over component refs only (1/1), link excluded


def test_internal_links_excluded_from_third_library_clustering():
    refs = [{"key": f"link:{i}", "source_node_id": str(i), "source_page_name": "H",
             "ref_type": "internal_link", "name": "Nav"} for i in range(3)]
    ev = _run(refs, _libs(CORE))
    assert not any(e["event_type"] == "third_library_suspect" for e in ev)  # links don't cluster as 3rd-lib
    assert not any(e["event_type"] == "unresolved_reference" for e in ev)
    assert ev[-1]["resolution_summary"]["internal_link_references"] == 3


def test_walk_internal_links_detects_reaction_and_hyperlink():
    node = {"id": "n1", "name": "Nav", "reactions": [{"action": {"type": "NODE", "destinationId": "p2:0"}}],
            "children": [{"id": "t1", "name": "Link", "type": "TEXT",
                          "style": {"hyperlink": {"nodeID": "p3:0"}}, "children": []}]}
    links = cfr._walk_internal_links(node, "Header")
    assert {ln["link_kind"] for ln in links} == {"reaction", "hyperlink"} and len(links) == 2
    assert all(ln["ref_type"] == "internal_link" for ln in links)


def test_component_variant_key_stays_unresolved_not_internal_link():
    # regression for the lane-6 escalation: the 2 Modules keys are component variants (ref_type=component),
    # NOT internal links -> must remain unresolved_reference, never reclassified.
    # NB: placeholder key (not the real 40-char hex — avoids gitleaks generic-secret flag; the test
    # only asserts a component ref with no Core match stays unresolved, so the value is a label).
    refs = [{"key": "componentVariantKeyNotInCore", "source_node_id": "x",
             "source_page_name": "MediaText", "name": "Size=lg, Strong=True, Spacing=False",
             "ref_type": "component"}]
    ev = _run(refs, _libs(CORE))
    assert any(e["event_type"] == "unresolved_reference" for e in ev)
    assert not any(e["event_type"] == "internal_link_reference" for e in ev)


def test_batch_bridge_aggregates_stream():
    refs = [{"key": "kButton", "source_node_id": "1", "source_page_name": "P"}]
    out = asyncio.run(cfr.resolve(COMP, refs, _libs(CORE), now=lambda: "T", fetch_library_map=_fetch()))
    assert out["resolution_complete"]["event_type"] == "resolution_complete"
    assert out["events"][0]["event_type"] == "resolution_started"


# ---- cache correctness (httpx MockTransport) -------------------------------
def _mock_client(last_modified):
    def handler(req):
        p = req.url.path
        if p.endswith(("/components",)):
            return httpx.Response(200, json={"meta": {"components": [{"key": "kA", "name": "A", "node_id": "1:1"}]}})
        if p.endswith("/component_sets"):
            return httpx.Response(200, json={"meta": {"component_sets": []}})
        return httpx.Response(200, json={"lastModified": last_modified})  # /files/{key}?depth=1
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_cache_hit_then_miss_on_lastmodified_change():
    cfr._LIB_CACHE.clear()

    async def go():
        async with _mock_client("2026-01-01T00:00:00Z") as c1:
            r1 = await cfr._default_fetch_library_map(CORE, c1)
            r2 = await cfr._default_fetch_library_map(CORE, c1)  # same lastModified → cache_hit
        async with _mock_client("2026-02-02T00:00:00Z") as c2:
            r3 = await cfr._default_fetch_library_map(CORE, c2)  # advanced → fresh_fetch
        return r1, r2, r3
    r1, r2, r3 = asyncio.run(go())
    assert r1["source"] == "fresh_fetch" and r2["source"] == "cache_hit" and r3["source"] == "fresh_fetch"
    assert r1["map"] == r2["map"] == r3["map"]


# ---- live smoke (Helix_Modules + Core; needs FIGMA_PAT) --------------------
_HAS_PAT = bool(os.environ.get("FIGMA_PAT") or os.environ.get("FIGMA_API_KEY"))
_MODULES = "qMi5B9YeqAf9Ik1yN6erw4"
_CORE_REAL = "8qPSyetzviLR6eF6bkpL44"


@pytest.mark.skipif(not _HAS_PAT, reason="FIGMA_PAT not in env — live smoke skipped")
def test_live_smoke_modules_mediatext_against_core():
    real_core_lib = [{"file_key": _CORE_REAL, "role": "core_foundation", "priority_order": 0}]

    async def go():
        refs = await cfr.enumerate_remote_references(_MODULES, ["2:4"])  # C_001_MediaText page
        # real fetch against Core (drop the mocked fetch — use the default REST fetcher)
        return refs, await cfr.resolve(_MODULES, refs, real_core_lib)
    refs, out = asyncio.run(go())
    summ = out["resolution_complete"]["resolution_summary"]
    assert summ["references_total"] >= 8  # ~9 remote instances on MediaText
    assert summ["references_resolved"] >= 6  # ~7/9 resolve to Core (2026-07-28 empirical)
    assert out["resolution_complete"]["provenance"]["llm_involvement"] == "none"
