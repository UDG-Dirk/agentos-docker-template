"""Tests for Lane 3 — get_figma_binding_topology (spec lane-3-binding-topology-v0-1-draft §8).

Deterministic failure-injection + binding-parse via httpx.MockTransport (backoff stubbed),
plus the Lane-3-specific partial-response + empty-bindings coverage tests, plus a live smoke
(skipif no FIGMA_PAT) against the Button component_set (57:766, verified 460+ bindings).
"""
from __future__ import annotations

import asyncio
import os

import httpx
import pytest

import agents.figma_extractor.binding_topology as bt

FILE_KEY = "8qPSyetzviLR6eF6bkpL44"
BTN = "57:766"


def _seg(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1]


def _mock_client(program: dict) -> httpx.AsyncClient:
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
    monkeypatch.setattr(bt, "_sleep", _noop)


def _run(program, node_ids=(BTN,)):
    async def go():
        async with _mock_client(program) as c:
            return await bt.run_binding_topology(FILE_KEY, list(node_ids), client=c)
    return asyncio.run(go())


# A Button subtree with node-level + component-property boundVariables (scalar/list/nested shapes).
_BTN_DOC = {
    "id": BTN, "name": "Button", "type": "COMPONENT_SET",
    "children": [
        {
            "id": "57:760", "name": "SolidButton", "type": "COMPONENT",
            "boundVariables": {
                "itemSpacing": {"type": "VARIABLE_ALIAS", "id": "VariableID:182:5761"},
                "paddingLeft": {"type": "VARIABLE_ALIAS", "id": "VariableID:182:5600"},
                "fills": [{"type": "VARIABLE_ALIAS", "id": "VariableID:769:865"}],
            },
            "children": [
                {
                    "id": "103:1814", "name": "arrow", "type": "INSTANCE",
                    "boundVariables": {"size": {"x": {"type": "VARIABLE_ALIAS", "id": "VariableID:19:20"}}},
                    "componentProperties": {
                        "Icon": {"type": "INSTANCE_SWAP", "value": "x",
                                 "boundVariables": {"value": {"type": "VARIABLE_ALIAS", "id": "VariableID:99:1"}}},
                    },
                },
            ],
        },
    ],
}
_BTN_OK = {"nodes": {BTN: {"document": _BTN_DOC}}}


def test_smoke_binding_parse_and_summary():
    res = _run({"nodes": [(200, _BTN_OK)]})
    assert res["status"] == "success" and res["failure_report"] is None
    b = res["bindings"]
    assert BTN in b  # requested node present (empty entry — coverage signal)
    assert b["57:760"]["property_bindings"]["itemSpacing"]["variable_id"] == "VariableID:182:5761"
    assert b["57:760"]["property_bindings"]["fills[0]"]["variable_id"] == "VariableID:769:865"
    assert b["103:1814"]["property_bindings"]["size.x"]["variable_id"] == "VariableID:19:20"
    assert b["103:1814"]["component_property_bindings"]["Icon"]["variable_id"] == "VariableID:99:1"
    s = res["binding_summary"]
    assert s["total_nodes_queried"] == 1 and s["nodes_returned"] == 1
    assert s["nodes_with_bindings"] == 2  # 57:760 + 103:1814 (57:766 empty)
    assert s["total_property_bindings"] == 4  # itemSpacing, paddingLeft, fills[0], size.x
    assert s["total_component_property_bindings"] == 1
    assert s["unique_variable_ids_referenced"] == 5
    assert res["provenance"]["lane"] == "lane-3-binding-topology"
    assert res["provenance"]["query_scope_node_ids"] == [BTN]


# ---- Lane-3-specific: partial-response = success with coverage gap ----------
def test_partial_response_is_success_with_gap():
    res = _run({"nodes": [(200, _BTN_OK)]}, node_ids=(BTN, "99:999"))  # 99:999 not in response
    assert res["status"] == "success"
    assert res["binding_summary"]["total_nodes_queried"] == 2
    assert res["binding_summary"]["nodes_returned"] == 1
    assert "99:999" not in res["bindings"]  # missing id absent, not a failure


def test_empty_bindings_is_success_coverage_signal():
    doc = {"id": "360:38", "name": "Colors", "type": "CANVAS", "children": [
        {"id": "360:52", "name": "Swatch", "type": "COMPONENT"}]}  # no boundVariables anywhere
    res = _run({"nodes": [(200, {"nodes": {"360:38": {"document": doc}}})]}, node_ids=("360:38",))
    assert res["status"] == "success"
    assert res["binding_summary"]["nodes_with_bindings"] == 0
    assert res["binding_summary"]["total_property_bindings"] == 0
    assert "360:38" in res["bindings"]  # queried node present with empty bindings


# ---- failure-injection (spec §6 v0.2 enum) ---------------------------------
def test_429_then_200_retries_to_success():
    res = _run({"nodes": [(429, "rate limited"), (200, _BTN_OK)]})
    assert res["status"] == "success" and res["failure_report"] is None


def test_401_auth_no_retry():
    res = _run({"nodes": [(401, "bad token")]})
    fr = res["failure_report"]
    assert res["status"] == "failure" and fr["error_class"] == "auth" and fr["retry_count"] == 0
    assert res["bindings"] == {}


def test_404_not_found_no_retry():
    fr = _run({"nodes": [(404, "not found")]})["failure_report"]
    assert fr["error_class"] == "not_found" and fr["http_status"] == 404 and fr["retry_count"] == 0


def test_400_client_error_no_retry():
    fr = _run({"nodes": [(400, "bad ids")]})["failure_report"]
    assert fr["error_class"] == "client_error" and fr["http_status"] == 400 and fr["retry_count"] == 0


def test_5xx_single_retry_then_server_error():
    fr = _run({"nodes": [(500, "boom"), (500, "boom")]})["failure_report"]
    assert fr["error_class"] == "server_error" and fr["retry_count"] == 1


def test_5xx_then_200_recovers():
    res = _run({"nodes": [(503, "svc"), (200, _BTN_OK)]})
    assert res["status"] == "success"


def test_timeout_one_retry():
    fr = _run({"nodes": [("timeout", None), ("timeout", None)]})["failure_report"]
    assert fr["error_class"] == "timeout" and fr["retry_count"] == 1


def test_empty_body_no_retry():
    fr = _run({"nodes": [(200, "")]})["failure_report"]
    assert fr["error_class"] == "empty" and fr["retry_count"] == 0


def test_malformed_json_no_retry():
    fr = _run({"nodes": [(200, "{not json")]})["failure_report"]
    assert fr["error_class"] == "malformed" and fr["retry_count"] == 0


def test_empty_node_ids_client_error_no_http():
    res = asyncio.run(bt.run_binding_topology(FILE_KEY, [], client=_mock_client({"nodes": [(200, _BTN_OK)]})))
    assert res["status"] == "failure"
    assert res["failure_report"]["error_class"] == "client_error"
    assert res["failure_report"]["http_status"] is None


# ---- live smoke (real REST; needs FIGMA_PAT) -------------------------------
_HAS_PAT = bool(os.environ.get("FIGMA_PAT") or os.environ.get("FIGMA_API_KEY"))


@pytest.mark.skipif(not _HAS_PAT, reason="FIGMA_PAT not in env — live smoke skipped")
def test_live_smoke_button_subtree():
    res = asyncio.run(bt.run_binding_topology(FILE_KEY, [BTN]))
    assert res["status"] == "success", f"failure_report={res.get('failure_report')}"
    assert res["bindings"], "expected non-empty bindings for the Button subtree"
    s = res["binding_summary"]
    assert s["total_property_bindings"] + s["total_component_property_bindings"] >= 100, \
        f"expected >=100 bindings floor, got {s}"
    assert res["provenance"]["query_scope_node_ids"] == [BTN]
    assert res["failure_report"] is None
