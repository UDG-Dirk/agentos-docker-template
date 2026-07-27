"""
Lane 2 — REST Semantic Layer (HELIX Path C)
==========================================

A composite tool for the Figma Extractor agent that pulls the *semantic* layer
Framelink (Lane 1) cannot see: the authored component-set taxonomy, the full
variant census, and the Text/Effect/Grid style axis — via three non-Enterprise
PAT-authenticated Figma REST endpoints, invoked in parallel.

Spec: ``helix-poc-agno:spec:lane-2-semantic-layer-v0-1-draft``.

Design (per spec):
  * β composite: ONE tool, three endpoints internally (asyncio.gather).
  * Naive description handling — ``description``/``description_rt`` captured
    verbatim; no structured parsing (deferred until Sascha's conventions known).
  * Never raises to the agent — always returns a structured dict with ``status``
    (success / partial / failure) and per-endpoint ``failure_reports``.
  * Minimum-viable provenance (lane marker + source_endpoints + extracted_at).

Auth: ``X-Figma-Token: $FIGMA_PAT`` (env; same source as the Framelink child).
This is PAT-only, server-side — no OAuth infrastructure (Path C Q2 dissolution).
"""
from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime

import httpx

FIGMA_API_BASE = "https://api.figma.com/v1"
TIMEOUT_S = 10.0  # spec §10.2 — arbitrary; Figma REST typically <2s
_RATE_LIMIT_BACKOFF = (0.5, 2.0, 8.0)  # spec §6 — 429 exponential, max 3 retries

# Endpoint registry: (failure-report operationId, URL path segment, response meta key).
_ENDPOINTS = (
    ("getFileComponentSets", "component_sets", "component_sets"),
    ("getFileComponents", "components", "components"),
    ("getFileStyles", "styles", "styles"),
)

# Injectable so failure-injection tests don't incur real backoff sleeps.
_sleep = asyncio.sleep


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _figma_pat() -> str:
    return os.environ.get("FIGMA_PAT") or os.environ.get("FIGMA_API_KEY", "")


def _failure(endpoint: str, http_status: int | None, error_class: str, message: str, retry_count: int) -> dict:
    return {
        "endpoint": endpoint,
        "http_status": http_status,
        "error_class": error_class,  # rate_limit|auth|server_error|timeout|empty|malformed
        "message": (message or "")[:500],  # bound; never echo secrets (none present in Figma errors)
        "attempted_at": _now_iso(),
        "retry_count": retry_count,
    }


async def _fetch_endpoint(client: httpx.AsyncClient, operation_id: str, path_seg: str, meta_key: str,
                          file_key: str) -> tuple[list | None, dict | None]:
    """Fetch one endpoint with the spec's per-error-class retry policy.

    Returns (items, None) on success or (None, failure_report) on failure. Never raises.
    """
    url = f"{FIGMA_API_BASE}/files/{file_key}/{path_seg}"
    retries = 0
    while True:
        try:
            resp = await client.get(url)
        except httpx.TimeoutException as e:
            if retries < 1:  # spec §6 — timeout: 1 retry
                retries += 1
                await _sleep(1.0)
                continue
            return None, _failure(operation_id, None, "timeout", f"request timed out: {e!r}", retries)
        except httpx.HTTPError as e:  # transport-level (connection etc.) — treat as timeout-class 1 retry
            if retries < 1:
                retries += 1
                await _sleep(1.0)
                continue
            return None, _failure(operation_id, None, "timeout", f"transport error: {e!r}", retries)

        status = resp.status_code

        if status == 200:
            body = resp.text
            if not body.strip():  # spec §6 — empty body: no retry, malformed/empty entry
                return None, _failure(operation_id, 200, "empty", "200 with empty response body", retries)
            try:
                data = resp.json()
            except Exception as e:  # spec §6 — malformed JSON: no retry
                return None, _failure(operation_id, 200, "malformed", f"invalid JSON: {e!r}", retries)
            items = (data.get("meta") or {}).get(meta_key)
            if items is None:
                return None, _failure(operation_id, 200, "malformed", f"missing meta.{meta_key} in response", retries)
            return list(items), None

        if status == 429:  # spec §6 — rate_limit: exp backoff, max 3 retries
            if retries < len(_RATE_LIMIT_BACKOFF):
                await _sleep(_RATE_LIMIT_BACKOFF[retries])
                retries += 1
                continue
            return None, _failure(operation_id, 429, "rate_limit", resp.text, retries)

        if status in (401, 403):  # spec §6 — auth: NO retry
            return None, _failure(operation_id, status, "auth", resp.text, retries)

        if 500 <= status < 600:  # spec §6 — server_error: 1 retry after 1s
            if retries < 1:
                retries += 1
                await _sleep(1.0)
                continue
            return None, _failure(operation_id, status, "server_error", resp.text, retries)

        # Any other unexpected status (e.g. 404 bad key, 400) — no retry. The spec's
        # error_class enum has no client-error bucket; classified server_error as the
        # catch-all with the true http_status recorded (flagged as spec feedback).
        return None, _failure(operation_id, status, "server_error", f"unexpected status {status}: {resp.text}", retries)


def _count_with_descriptions(items: list | None) -> int:
    if not items:
        return 0
    return sum(1 for it in items if isinstance(it, dict) and (it.get("description") or "").strip())


async def run_semantic_layer(file_key: str, client: httpx.AsyncClient | None = None) -> dict:
    """Core implementation (testable). Fetches the three endpoints in parallel and
    composes the payload. Pass ``client`` (e.g. an httpx.AsyncClient with a MockTransport)
    for failure-injection tests; otherwise a real PAT-authenticated client is created."""
    owns_client = client is None
    if owns_client:
        pat = _figma_pat()
        client = httpx.AsyncClient(
            headers={"X-Figma-Token": pat},
            timeout=TIMEOUT_S,
        )
    try:
        results = await asyncio.gather(
            *[_fetch_endpoint(client, op, path, meta, file_key) for op, path, meta in _ENDPOINTS]
        )
    finally:
        if owns_client:
            await client.aclose()

    (cs_items, cs_fail), (co_items, co_fail), (st_items, st_fail) = results
    failure_reports = [f for f in (cs_fail, co_fail, st_fail) if f is not None]
    successes = 3 - len(failure_reports)
    status = "success" if successes == 3 else ("partial" if successes >= 1 else "failure")
    extracted_at = _now_iso()

    return {
        "status": status,
        "file_key": file_key,
        "extracted_at": extracted_at,
        "component_sets": cs_items or [],
        "components": co_items or [],
        "styles": st_items or [],
        "coverage_report": {
            "component_sets_total": len(cs_items or []),
            "component_sets_with_descriptions": _count_with_descriptions(cs_items),
            "components_total": len(co_items or []),
            "components_with_descriptions": _count_with_descriptions(co_items),
            "styles_total": len(st_items or []),
            "styles_with_descriptions": _count_with_descriptions(st_items),
        },
        "provenance": {
            "lane": "lane-2-semantic-layer",
            "source_endpoints": ["/component_sets", "/components", "/styles"],
            "extracted_at": extracted_at,
            "file_key": file_key,
        },
        "failure_reports": failure_reports,
    }


async def get_figma_semantic_layer(file_key: str) -> dict:
    """Pull the Figma REST semantic layer for a design file (Lane 2 of the HELIX pipeline).

    Fetches, in parallel, the design system's authored taxonomy that Framelink cannot
    surface: published component SETS (with the designer's descriptions/agent-instructions),
    the full individual-component variant census, and Text/Effect/Grid STYLES. Descriptions
    are captured verbatim (no parsing). Never raises — returns a dict whose ``status`` is
    "success", "partial", or "failure" with per-endpoint ``failure_reports`` and a
    ``coverage_report``. Complements ``get_figma_data`` (Framelink values); REST is canonical
    for structural names, Framelink for resolved values.

    Args:
        file_key: The Figma file key, e.g. "8qPSyetzviLR6eF6bkpL44".
    """
    return await run_semantic_layer(file_key)
