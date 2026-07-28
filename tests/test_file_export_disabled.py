"""Tests for the file_export_disabled 403 classifier + per-lane wiring (task: file-export-disabled-
detection). Deterministic + offline (MockTransport / injected fetchers). Covers the "File not
exportable" content-protection lock B-S surfaced 2026-07-28."""
from __future__ import annotations

import asyncio

import httpx
import pytest

import agents.figma_extractor.binding_topology as bt
import agents.figma_extractor.cache_versioning as cv
import agents.figma_extractor.http_errors as he
import agents.figma_extractor.pathway_b_traversal as pb
import agents.figma_extractor.token_catalog as tc

EXPORT_LOCK_BODY = '{"status":403,"err":"File not exportable"}'
DGX = "wjSOPgJLnuDztSDx4OIXSM"


def _mock_client(status, body):
    def _h(req):
        return httpx.Response(status, text=body)
    return httpx.AsyncClient(transport=httpx.MockTransport(_h))


# ---- pure classifier ------------------------------------------------------
def test_classify_forbidden_categories():
    assert he.classify_forbidden("File not exportable") == "file_export_disabled"
    assert he.classify_forbidden('{"err":"File not exportable"}') == "file_export_disabled"
    assert he.classify_forbidden("NOT EXPORTABLE") == "file_export_disabled"  # case-insensitive
    assert he.classify_forbidden("Invalid scope") == "enterprise_scope"
    assert he.classify_forbidden("Forbidden") == "forbidden"
    assert he.classify_forbidden("") == "forbidden"
    assert he.classify_forbidden(None) == "forbidden"


def test_classify_http_error_status_map():
    assert he.classify_http_error(403, "File not exportable") == "file_export_disabled"
    assert he.classify_http_error(403, "Invalid scope") == "enterprise_scope"
    assert he.classify_http_error(403, "nope") == "forbidden"
    assert he.classify_http_error(401, "") == "auth_failure"
    assert he.classify_http_error(429, "") == "rate_limit"
    assert he.classify_http_error(404, "") == "not_found"
    assert he.classify_http_error(500, "") == "server_error"
    assert he.classify_http_error(418, "") == "client_error"


def test_file_export_disabled_report_shape():
    r = he.file_export_disabled_report(affected_lane="lane-5-cache-versioning")
    assert r["error_class"] == "file_export_disabled" and r["http_status"] == 403
    assert r["affected_lane"] == "lane-5-cache-versioning"
    assert "disable" in r["action_required"].lower() and r["provenance"]["llm_involvement"] == "none"
    assert "Lane 2" in r["accessible_lanes"]


def test_extract_figma_err():
    assert he._extract_figma_err('{"err":"File not exportable"}') == "File not exportable"
    assert he._extract_figma_err("not json") is None
    assert he._extract_figma_err('{"nope":1}') is None


# ---- raise_for_figma_status -----------------------------------------------
def test_raise_for_figma_status_export_lock():
    resp = httpx.Response(403, text=EXPORT_LOCK_BODY, request=httpx.Request("GET", "http://x"))
    with pytest.raises(he.FileExportDisabledError) as ei:
        he.raise_for_figma_status(resp)
    assert ei.value.figma_message == "File not exportable"


def test_raise_for_figma_status_other_403_and_5xx_still_raise_httpstatus():
    for body, code in [("Invalid scope", 403), ("boom", 500)]:
        resp = httpx.Response(code, text=body, request=httpx.Request("GET", "http://x"))
        with pytest.raises(httpx.HTTPStatusError):
            he.raise_for_figma_status(resp)


def test_raise_for_figma_status_200_no_raise():
    resp = httpx.Response(200, text="{}", request=httpx.Request("GET", "http://x"))
    he.raise_for_figma_status(resp)  # no exception


# ---- Lane 5 (cache_versioning) wiring -------------------------------------
def test_cache_versioning_export_lock_classified():
    async def go(body, status=403):
        client = _mock_client(status, body)
        try:
            return await cv.run_file_meta(DGX, client=client)
        finally:
            await client.aclose()
    r = asyncio.run(go(EXPORT_LOCK_BODY))
    fr = r.get("failure_report") or {}
    assert fr.get("error_class") == "file_export_disabled"
    # a generic 403 stays 'forbidden' (not misclassified), 401 stays 'auth'
    assert (asyncio.run(go('{"err":"nope"}')).get("failure_report") or {}).get("error_class") == "forbidden"
    assert (asyncio.run(go("bad token", 401)).get("failure_report") or {}).get("error_class") == "auth"


# ---- Lane 3 (binding_topology) wiring -------------------------------------
def test_binding_topology_export_lock_classified():
    async def go(body):
        client = _mock_client(403, body)
        try:
            return await bt.run_binding_topology(DGX, ["1:2"], client=client)
        finally:
            await client.aclose()
    r = asyncio.run(go(EXPORT_LOCK_BODY))
    # the export-lock class surfaces in the envelope's failure_report, wherever it is carried
    assert "file_export_disabled" in str(r)


# ---- Pathway B (Lane 1 composition path) wiring ---------------------------
def test_pathway_b_export_lock_on_page_fetch():
    async def fp(fk):
        raise he.FileExportDisabledError("File not exportable")
    r = asyncio.run(pb.run_pathway_b(DGX, fetch_pages=fp, fetch_node=lambda *a: None))
    assert r["pathway_b_status"] == "failure"
    assert any(f["error_class"] == "file_export_disabled" for f in r["failure_reports"])


def test_pathway_b_export_lock_mid_traversal():
    async def fp(fk):
        return [{"id": "p1", "name": "P"}]
    async def fn(fk, nid, depth):
        raise he.FileExportDisabledError("File not exportable")
    r = asyncio.run(pb.run_pathway_b(DGX, fetch_pages=fp, fetch_node=fn))
    assert r["pathway_b_status"] == "failure"
    assert any(f["error_class"] == "file_export_disabled" for f in r["failure_reports"])


# ---- Lane 7 (token_catalog) wiring ----------------------------------------
def test_token_catalog_export_lock_classified():
    async def fetch_shared(fk):
        raise he.FileExportDisabledError("File not exportable")
    r = asyncio.run(tc.run_token_catalog(DGX, fetch_shared=fetch_shared))
    cat = r["token_catalog"]
    assert cat["status"] == "failure"
    assert any(f["error_class"] == "file_export_disabled" for f in cat["failure_reports"])


# ---- determinism ----------------------------------------------------------
def test_classification_deterministic():
    assert he.classify_http_error(403, EXPORT_LOCK_BODY) == he.classify_http_error(403, EXPORT_LOCK_BODY)
