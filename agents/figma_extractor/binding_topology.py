"""
Lane 3 — REST Binding Topology (HELIX Path C)
=============================================

One node-scoped tool on the Figma Extractor agent (spec
``helix-poc-agno:spec:lane-3-binding-topology-v0-1-draft``):

  * ``get_figma_binding_topology(file_key, node_ids, depth?, geometry?)`` — single
    ``getFileNodes`` call; parses ``boundVariables`` at node level (fills/strokes/
    effects/layout/spacing) AND at componentProperty level; returns a compact
    ``{node_id → property → VariableID}`` map + a ``binding_summary``.

The map from node properties to Figma Variable IDs — the "intent" linking Lane 1's
resolved values to Lane 2's taxonomy. Variable IDs are surfaced OPAQUE (``VariableID:X:Y``);
resolution is downstream reconciliation, NOT Lane 3's job (spec §9). Node-scoped (caller
passes node_ids — usually from Lane 1's tree or Lane 2's component_sets/components).
PAT-only, server-side. error_class + retry mirror Lane 2 v0.2 / Lane 5 v0.1, with a 30s
timeout (payloads are larger). Single ``failure_report`` object (not array). Never raises.

Partial responses (some requested node_ids missing) are ``status: success`` with the gap
visible via ``binding_summary.total_nodes_queried > nodes_returned`` — NOT a failure (spec §6).
"""
from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime

import httpx

from agents.figma_extractor.http_errors import classify_http_error

FIGMA_API_BASE = "https://api.figma.com/v1"
TIMEOUT_S = 30.0  # spec §10.3 — larger than Lane 2/5 (binding-heavy queries return more)
_RATE_LIMIT_BACKOFF = (0.5, 2.0, 8.0)
_LANE = "lane-3-binding-topology"

_sleep = asyncio.sleep  # injectable for tests


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _figma_pat() -> str:
    return os.environ.get("FIGMA_PAT") or os.environ.get("FIGMA_API_KEY", "")


def _failure(http_status: int | None, error_class: str, message: str, retry_count: int) -> dict:
    return {
        "endpoint": "getFileNodes",
        "http_status": http_status,
        "error_class": error_class,  # rate_limit|auth|not_found|client_error|server_error|timeout|empty|malformed
        "message": (message or "")[:500],
        "attempted_at": _now_iso(),
        "retry_count": retry_count,
    }


async def _fetch_nodes(client: httpx.AsyncClient, file_key: str, params: dict) -> tuple[dict | None, dict | None]:
    """GET /files/{key}/nodes with the Lane 2 v0.2 retry/classification policy.
    Returns (json, None) or (None, failure_report). Never raises."""
    url = f"{FIGMA_API_BASE}/files/{file_key}/nodes"
    retries = 0
    while True:
        try:
            resp = await client.get(url, params=params)
        except httpx.TimeoutException as e:
            if retries < 1:
                retries += 1
                await _sleep(1.0)
                continue
            return None, _failure(None, "timeout", f"request timed out: {e!r}", retries)
        except httpx.HTTPError as e:
            if retries < 1:
                retries += 1
                await _sleep(1.0)
                continue
            return None, _failure(None, "timeout", f"transport error: {e!r}", retries)

        status = resp.status_code
        if status == 200:
            if not resp.text.strip():
                return None, _failure(200, "empty", "200 with empty response body", retries)
            try:
                return resp.json(), None
            except Exception as e:
                return None, _failure(200, "malformed", f"invalid JSON: {e!r}", retries)
        if status == 429:
            if retries < len(_RATE_LIMIT_BACKOFF):
                await _sleep(_RATE_LIMIT_BACKOFF[retries])
                retries += 1
                continue
            return None, _failure(429, "rate_limit", resp.text, retries)
        if status in (401, 403):
            # 403 sub-classification (file_export_disabled / enterprise_scope / forbidden); 401 -> auth.
            ec = classify_http_error(status, resp.text) if status == 403 else "auth"
            return None, _failure(status, ec, resp.text, retries)
        if status == 404:
            return None, _failure(404, "not_found", resp.text, retries)
        if 400 <= status < 500:
            return None, _failure(status, "client_error", resp.text, retries)
        if 500 <= status < 600:
            if retries < 1:
                retries += 1
                await _sleep(1.0)
                continue
            return None, _failure(status, "server_error", resp.text, retries)
        return None, _failure(status, "client_error", f"unexpected status {status}: {resp.text}", retries)


def _flatten_bound(bv, prefix: str = "") -> dict:
    """Flatten a Figma ``boundVariables`` object to {property_path: {type, variable_id}}.
    Handles scalar alias ({type,id}), list of aliases (fills/strokes → key[i]), and nested
    dicts (size → key.x/key.y). Verbatim capture — no canonicalization (spec §4)."""
    out: dict = {}
    if not isinstance(bv, dict):
        return out

    def add(path: str, val) -> None:
        if isinstance(val, dict) and val.get("type") == "VARIABLE_ALIAS" and "id" in val:
            out[path] = {"type": "VARIABLE_ALIAS", "variable_id": val["id"]}
        elif isinstance(val, list):
            for i, elem in enumerate(val):
                add(f"{path}[{i}]", elem)
        elif isinstance(val, dict):
            for k, v in val.items():
                add(f"{path}.{k}", v)

    for key, val in bv.items():
        add(f"{prefix}{key}" if prefix else key, val)
    return out


def _component_property_bindings(node: dict) -> dict:
    """Extract boundVariables carried on componentPropertyDefinitions / componentProperties."""
    out: dict = {}
    for src in ("componentPropertyDefinitions", "componentProperties"):
        props = node.get(src)
        if not isinstance(props, dict):
            continue
        for pname, pdef in props.items():
            if isinstance(pdef, dict) and pdef.get("boundVariables"):
                for subpath, alias in _flatten_bound(pdef["boundVariables"]).items():
                    # component-property boundVariables are typically {"value": alias}
                    key = pname if subpath == "value" else f"{pname}.{subpath}"
                    out[key] = alias
    return out


def _walk(node, bindings: dict) -> None:
    """Recursively collect node-level + component-property boundVariables across a subtree."""
    if not isinstance(node, dict):
        return
    nid = node.get("id")
    pb = _flatten_bound(node.get("boundVariables"))
    cpb = _component_property_bindings(node)
    if nid and (pb or cpb):
        entry = bindings.setdefault(nid, {
            "node_type": node.get("type"),
            "node_name": node.get("name"),
            "property_bindings": {},
            "component_property_bindings": {},
        })
        entry["property_bindings"].update(pb)
        entry["component_property_bindings"].update(cpb)
    for child in node.get("children", []) or []:
        _walk(child, bindings)


async def run_binding_topology(file_key: str, node_ids: list[str], depth: int | None = None,
                               geometry: str | None = None, client: httpx.AsyncClient | None = None) -> dict:
    """Core (testable). Pass ``client`` (httpx.AsyncClient w/ MockTransport) for tests."""
    extracted_at_start = _now_iso()
    requested = list(node_ids or [])

    def envelope(status, bindings, failure):
        pb_total = sum(len(b["property_bindings"]) for b in bindings.values())
        cpb_total = sum(len(b["component_property_bindings"]) for b in bindings.values())
        uniq = {a["variable_id"] for b in bindings.values()
                for a in list(b["property_bindings"].values()) + list(b["component_property_bindings"].values())}
        nodes_returned = 0 if failure else _nodes_returned_holder[0]
        return {
            "status": status,
            "file_key": file_key,
            "requested_node_ids": requested,
            "extracted_at": _now_iso(),
            "bindings": bindings,
            "binding_summary": {
                "total_nodes_queried": len(requested),
                "nodes_returned": nodes_returned,
                "nodes_with_bindings": sum(1 for b in bindings.values()
                                           if b["property_bindings"] or b["component_property_bindings"]),
                "total_property_bindings": pb_total,
                "total_component_property_bindings": cpb_total,
                "unique_variable_ids_referenced": len(uniq),
            },
            "provenance": {
                "lane": _LANE,
                "source_endpoint": "/files/{file_key}/nodes",
                "extracted_at": extracted_at_start,
                "file_key": file_key,
                "query_scope_node_ids": requested,
            },
            "failure_report": failure,
        }

    _nodes_returned_holder = [0]

    if not requested:  # client-side validation — node_ids required non-empty (spec §2)
        return envelope("failure", {}, _failure(None, "client_error", "node_ids is required and must be non-empty", 0))

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(headers={"X-Figma-Token": _figma_pat()}, timeout=TIMEOUT_S)
    params: dict = {"ids": ",".join(requested)}
    if depth is not None:
        params["depth"] = depth
    if geometry is not None:
        params["geometry"] = geometry
    try:
        data, failure = await _fetch_nodes(client, file_key, params)
    finally:
        if owns_client:
            await client.aclose()

    if failure is not None:
        return envelope("failure", {}, failure)

    resp_nodes = (data or {}).get("nodes") or {}
    bindings: dict = {}
    nodes_returned = 0
    for rid in requested:
        entry = resp_nodes.get(rid)
        doc = entry.get("document") if isinstance(entry, dict) else None
        if not doc:
            continue  # missing requested id → coverage gap (total_queried > returned), still success
        nodes_returned += 1
        _walk(doc, bindings)
        docid = doc.get("id", rid)
        # ensure the requested node appears even with no bindings (coverage signal, spec §4)
        bindings.setdefault(docid, {
            "node_type": doc.get("type"),
            "node_name": doc.get("name"),
            "property_bindings": {},
            "component_property_bindings": {},
        })
    _nodes_returned_holder[0] = nodes_returned
    return envelope("success", bindings, None)


async def get_figma_binding_topology(file_key: str, node_ids: list[str], depth: int | None = None,
                                     geometry: str | None = None) -> dict:
    """Extract the binding topology (node property → Figma Variable ID) for specific nodes (Lane 3).

    One REST call to getFileNodes for the given node_ids; parses boundVariables at node level
    (fills/strokes/effects/layout/spacing) and componentProperty level, returning a compact
    ``bindings`` map + ``binding_summary``. Variable IDs are surfaced OPAQUE (VariableID:X:Y) —
    resolution to names/values is downstream work. Pass node_ids discovered via Lane 1 (Framelink
    tree) or Lane 2 (component_sets/components). Never raises: returns ``{status, bindings,
    binding_summary, provenance, failure_report}``. Missing requested ids = success with a
    coverage gap (total_nodes_queried > nodes_returned), not a failure.

    Args:
        file_key: The Figma file key, e.g. "8qPSyetzviLR6eF6bkpL44".
        node_ids: Non-empty list of node ids to query, e.g. ["57:766"].
        depth: Optional subtree depth (default: full subtree).
        geometry: Optional "paths" to include vector geometry (default: omitted, smaller payload).
    """
    return await run_binding_topology(file_key, node_ids, depth, geometry)
