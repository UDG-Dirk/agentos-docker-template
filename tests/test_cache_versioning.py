"""Tests for Lane 5 — cache/versioning tools (spec lane-5-cache-versioning-v0-1-draft §8).

Deterministic failure-injection via httpx.MockTransport (backoff stubbed) exercising the
real run_file_meta / run_file_versions control flow; plus a live smoke (skipif no FIGMA_PAT).
Mirrors the Lane 2 test pattern; adds the v0.2 not_found (404) + client_error (400) cases.
"""
from __future__ import annotations

import asyncio
import os

import httpx
import pytest

import agents.figma_extractor.cache_versioning as cv

FILE_KEY = "8qPSyetzviLR6eF6bkpL44"


def _seg(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1]


def _mock_client(program: dict) -> httpx.AsyncClient:
    """program: {segment: [(status, json_or_text), ...]} consumed in order (last repeats).
    Use ('timeout', None) to raise httpx.TimeoutException."""
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
    monkeypatch.setattr(cv, "_sleep", _noop)


def _meta(program):
    async def go():
        async with _mock_client(program) as c:
            return await cv.run_file_meta(FILE_KEY, client=c)
    return asyncio.run(go())


def _versions(program):
    async def go():
        async with _mock_client(program) as c:
            return await cv.run_file_versions(FILE_KEY, client=c)
    return asyncio.run(go())


_META_BODY = {"file": {"version": "1784894124", "last_touched_at": "2026-07-27T12:53:50Z",
                        "name": "Helix_Core-Library", "role": "owner", "editorType": "figma"}}
_VERS_BODY = {"versions": [{"id": f"v{i}", "created_at": "2026-07-01T00:00:00Z", "label": None,
                            "description": None, "user": {"id": "u", "handle": "h"}} for i in range(30)],
              "pagination": {"next_page": None}}


# ---- success / shape / provenance ------------------------------------------
def test_meta_success_shape_and_provenance():
    res = _meta({"meta": [(200, _META_BODY)]})
    assert res["status"] == "success"
    assert res["failure_report"] is None
    assert res["payload"]["version"] == "1784894124"
    assert res["payload"]["last_touched_at"].startswith("2026-07-27")
    assert res["provenance"]["lane"] == "lane-5-cache-versioning"
    assert res["provenance"]["source_endpoint"] == "/meta"
    assert res["file_key"] == FILE_KEY and res["extracted_at"]


def test_versions_success_shape_and_provenance():
    res = _versions({"versions": [(200, _VERS_BODY)]})
    assert res["status"] == "success"
    assert res["failure_report"] is None
    assert len(res["payload"]["versions"]) == 30
    assert res["payload"]["versions"][0]["id"] and res["payload"]["versions"][0]["created_at"]
    assert "pagination" in res["payload"]
    assert res["provenance"]["source_endpoint"] == "/versions"


# ---- retry classes (spec §6 v0.2) ------------------------------------------
def test_429_then_200_retries_to_success():
    res = _meta({"meta": [(429, "rate limited"), (200, _META_BODY)]})
    assert res["status"] == "success" and res["failure_report"] is None


def test_401_auth_no_retry():
    res = _meta({"meta": [(401, "bad token")]})
    assert res["status"] == "failure"
    fr = res["failure_report"]
    assert fr["error_class"] == "auth" and fr["http_status"] == 401 and fr["retry_count"] == 0
    assert res["payload"] == {}


def test_404_not_found_no_retry():
    res = _meta({"meta": [(404, "not found")]})
    fr = res["failure_report"]
    assert fr["error_class"] == "not_found" and fr["http_status"] == 404 and fr["retry_count"] == 0


def test_400_client_error_no_retry():
    res = _versions({"versions": [(400, "bad request")]})
    fr = res["failure_report"]
    assert fr["error_class"] == "client_error" and fr["http_status"] == 400 and fr["retry_count"] == 0


def test_5xx_single_retry_then_server_error():
    res = _meta({"meta": [(500, "boom"), (500, "boom")]})
    fr = res["failure_report"]
    assert fr["error_class"] == "server_error" and fr["http_status"] == 500 and fr["retry_count"] == 1


def test_5xx_then_200_recovers():
    res = _versions({"versions": [(503, "svc"), (200, _VERS_BODY)]})
    assert res["status"] == "success" and len(res["payload"]["versions"]) == 30


def test_timeout_one_retry():
    res = _meta({"meta": [("timeout", None), ("timeout", None)]})
    fr = res["failure_report"]
    assert fr["error_class"] == "timeout" and fr["retry_count"] == 1


def test_empty_body_no_retry():
    res = _meta({"meta": [(200, "")]})
    fr = res["failure_report"]
    assert fr["error_class"] == "empty" and fr["retry_count"] == 0


def test_malformed_json_no_retry():
    res = _versions({"versions": [(200, "{not json")]})
    fr = res["failure_report"]
    assert fr["error_class"] == "malformed" and fr["retry_count"] == 0


# ---- live smoke (real REST; needs FIGMA_PAT) -------------------------------
_HAS_PAT = bool(os.environ.get("FIGMA_PAT") or os.environ.get("FIGMA_API_KEY"))


@pytest.mark.skipif(not _HAS_PAT, reason="FIGMA_PAT not in env — live smoke skipped")
def test_live_smoke_meta_and_versions():
    meta = asyncio.run(cv.run_file_meta(FILE_KEY))
    assert meta["status"] == "success", f"failure_report={meta.get('failure_report')}"
    assert meta["payload"].get("version")
    assert meta["payload"].get("last_touched_at")
    assert meta["provenance"]["lane"] == "lane-5-cache-versioning"

    vers = asyncio.run(cv.run_file_versions(FILE_KEY))
    assert vers["status"] == "success", f"failure_report={vers.get('failure_report')}"
    assert len(vers["payload"]["versions"]) >= 1
    v0 = vers["payload"]["versions"][0]
    assert v0.get("id") and v0.get("created_at")
