"""Tests for Lane 2 — get_figma_semantic_layer (spec lane-2-semantic-layer-v0-1-draft §8).

Two kinds:
  * Failure-injection (deterministic): httpx.MockTransport programs per-endpoint response
    sequences; ``_sleep`` is monkeypatched to a no-op so retry backoff adds no wall time.
    Exercises the real run_semantic_layer control flow — no network, no PAT.
  * Live smoke (skipif no FIGMA_PAT): real REST against Helix_Core-Library — asserts the
    27 / 418 / 35 baseline + 8 described component_sets + status success.
"""
from __future__ import annotations

import asyncio
import os

import httpx
import pytest

import agents.figma_extractor.semantic_layer as sl

FILE_KEY = "8qPSyetzviLR6eF6bkpL44"


# ---- deterministic failure-injection harness -------------------------------
def _seg(path: str) -> str:
    # ".../files/<key>/<segment>" -> "<segment>"
    return path.rstrip("/").rsplit("/", 1)[-1]


def _meta(seg: str, n: int, described: int = 0) -> dict:
    """Build a {'meta': {<seg>: [...]}} body with n items, `described` of them described."""
    items = []
    for i in range(n):
        it = {"node_id": f"1:{i}", "name": f"{seg}_{i}", "key": f"k{i}"}
        it["description"] = "purpose: x" if i < described else ""
        items.append(it)
    return {"meta": {seg: items}}


def _mock_client(program: dict) -> httpx.AsyncClient:
    """program: {segment: [ (status, json_or_text), ... ]} consumed in order per segment.
    A segment with a single entry repeats it. Raise httpx.TimeoutException by using
    ('timeout', None)."""
    state = {k: 0 for k in program}

    def handler(request: httpx.Request) -> httpx.Response:
        seg = _seg(request.url.path)
        seq = program[seg]
        idx = min(state[seg], len(seq) - 1)
        state[seg] += 1
        status, payload = seq[idx]
        if status == "timeout":
            raise httpx.TimeoutException("simulated timeout", request=request)
        if isinstance(payload, (dict, list)):
            return httpx.Response(status, json=payload, request=request)
        return httpx.Response(status, text=(payload or ""), request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    async def _noop(_):
        return None
    monkeypatch.setattr(sl, "_sleep", _noop)


def _run(program: dict) -> dict:
    async def go():
        async with _mock_client(program) as c:
            return await sl.run_semantic_layer(FILE_KEY, client=c)
    return asyncio.run(go())


# ---- success / coverage / provenance ---------------------------------------
def test_all_success_coverage_and_provenance():
    res = _run({
        "component_sets": [(200, _meta("component_sets", 27, described=8))],
        "components": [(200, _meta("components", 418))],
        "styles": [(200, _meta("styles", 35, described=3))],
    })
    assert res["status"] == "success"
    assert res["failure_reports"] == []
    assert len(res["component_sets"]) == 27
    assert len(res["components"]) == 418
    assert len(res["styles"]) == 35
    cr = res["coverage_report"]
    assert cr["component_sets_total"] == 27 and cr["component_sets_with_descriptions"] == 8
    assert cr["components_total"] == 418
    assert cr["styles_total"] == 35 and cr["styles_with_descriptions"] == 3
    assert res["provenance"]["lane"] == "lane-2-semantic-layer"
    assert res["provenance"]["source_endpoints"] == ["/component_sets", "/components", "/styles"]
    assert res["file_key"] == FILE_KEY and res["extracted_at"]


# ---- 429 → retry then 200 ---------------------------------------------------
def test_429_then_200_retries_to_success():
    res = _run({
        "component_sets": [(200, _meta("component_sets", 1))],
        "components": [(429, "rate limited"), (200, _meta("components", 5))],  # 429 once, then 200
        "styles": [(200, _meta("styles", 1))],
    })
    assert res["status"] == "success"
    assert res["failure_reports"] == []
    assert len(res["components"]) == 5  # succeeded on retry


# ---- 401 → immediate auth failure, no retry --------------------------------
def test_401_no_retry_auth_report():
    res = _run({
        "component_sets": [(200, _meta("component_sets", 1))],
        "components": [(401, "bad token")],
        "styles": [(200, _meta("styles", 1))],
    })
    assert res["status"] == "partial"
    fr = [f for f in res["failure_reports"] if f["endpoint"] == "getFileComponents"]
    assert len(fr) == 1
    assert fr[0]["error_class"] == "auth"
    assert fr[0]["http_status"] == 401
    assert fr[0]["retry_count"] == 0  # no retry on auth


# ---- 5xx → single retry then failure ---------------------------------------
def test_5xx_single_retry_then_server_error():
    res = _run({
        "component_sets": [(200, _meta("component_sets", 1))],
        "components": [(500, "boom"), (500, "boom")],  # 500 twice: 1 retry then give up
        "styles": [(200, _meta("styles", 1))],
    })
    assert res["status"] == "partial"
    fr = [f for f in res["failure_reports"] if f["endpoint"] == "getFileComponents"][0]
    assert fr["error_class"] == "server_error" and fr["http_status"] == 500
    assert fr["retry_count"] == 1  # exactly one retry per spec


def test_5xx_then_200_recovers():
    res = _run({
        "component_sets": [(200, _meta("component_sets", 1))],
        "components": [(503, "svc"), (200, _meta("components", 9))],
        "styles": [(200, _meta("styles", 1))],
    })
    assert res["status"] == "success"
    assert len(res["components"]) == 9


# ---- partial (spec §8 explicit case) ---------------------------------------
def test_partial_component_sets_ok_components_500():
    res = _run({
        "component_sets": [(200, _meta("component_sets", 27, described=8))],
        "components": [(500, "err"), (500, "err")],
        "styles": [(200, _meta("styles", 35))],
    })
    assert res["status"] == "partial"
    assert len(res["component_sets"]) == 27  # present
    assert len(res["styles"]) == 35          # present
    assert res["components"] == []           # empty on failure
    assert len(res["failure_reports"]) == 1
    assert res["failure_reports"][0]["endpoint"] == "getFileComponents"


# ---- empty / malformed / timeout / all-fail --------------------------------
def test_empty_body_no_retry():
    res = _run({
        "component_sets": [(200, "")],  # empty body
        "components": [(200, _meta("components", 1))],
        "styles": [(200, _meta("styles", 1))],
    })
    fr = [f for f in res["failure_reports"] if f["endpoint"] == "getFileComponentSets"][0]
    assert fr["error_class"] == "empty" and fr["retry_count"] == 0


def test_malformed_json_no_retry():
    res = _run({
        "component_sets": [(200, "{not json")],
        "components": [(200, _meta("components", 1))],
        "styles": [(200, _meta("styles", 1))],
    })
    fr = [f for f in res["failure_reports"] if f["endpoint"] == "getFileComponentSets"][0]
    assert fr["error_class"] == "malformed" and fr["retry_count"] == 0


def test_timeout_one_retry():
    res = _run({
        "component_sets": [("timeout", None), ("timeout", None)],  # times out twice
        "components": [(200, _meta("components", 1))],
        "styles": [(200, _meta("styles", 1))],
    })
    fr = [f for f in res["failure_reports"] if f["endpoint"] == "getFileComponentSets"][0]
    assert fr["error_class"] == "timeout" and fr["retry_count"] == 1


def test_all_fail_status_failure():
    res = _run({
        "component_sets": [(401, "x")],
        "components": [(401, "x")],
        "styles": [(401, "x")],
    })
    assert res["status"] == "failure"
    assert len(res["failure_reports"]) == 3
    assert res["component_sets"] == [] and res["components"] == [] and res["styles"] == []


# ---- live smoke (real REST; needs FIGMA_PAT) -------------------------------
_HAS_PAT = bool(os.environ.get("FIGMA_PAT") or os.environ.get("FIGMA_API_KEY"))


@pytest.mark.skipif(not _HAS_PAT, reason="FIGMA_PAT not in env — live smoke skipped")
def test_live_smoke_helix_core_library():
    res = asyncio.run(sl.run_semantic_layer(FILE_KEY))
    assert res["status"] == "success", f"failure_reports={res.get('failure_reports')}"
    # Shape/invariant, NOT moment-in-time counts: HELIX_Core's content drifts as the design system
    # grows (was hardcoded 27 sets / 418 components / 35 styles). Assert presence + schema + sane
    # relationships, which catch real regressions without false-red on drift.
    cs, comps, styles = res["component_sets"], res["components"], res["styles"]
    assert 0 < len(cs) <= 500                     # roster present + sanity ceiling
    assert len(comps) >= len(cs)                  # each component set has at least one variant
    assert len(styles) > 0
    for c in cs:
        assert c.get("name") and c.get("key") and c.get("node_id")
    assert 0 <= res["coverage_report"]["component_sets_with_descriptions"] <= len(cs)
    assert res["provenance"]["lane"] == "lane-2-semantic-layer"
    assert res["failure_reports"] == []
