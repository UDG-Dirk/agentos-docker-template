"""
Lane 5 — REST Cache / Versioning primitives (HELIX Path C)
=========================================================

Two atomic tools on the Figma Extractor agent (spec
``helix-poc-agno:spec:lane-5-cache-versioning-v0-1-draft``):

  * ``get_figma_file_meta``     — cheap (~965 B) change-detection probe: ``version``
                                  + ``last_touched_at`` without pulling the ~700 KB tree.
  * ``get_figma_file_versions`` — historical version list (reproducibility / audit).

Primitives only — Lane 5 provides the data; the "did it change / what to pin" DECISION
lives in the workflow layer (spec §9 non-goal). PAT-only, server-side (env), same as
Lane 1/2. error_class enum + retry policy match Lane 2 v0.2 (adds ``not_found`` /
``client_error``). Each tool = one endpoint → a single ``failure_report`` object (or null),
NOT an array. Never raises to the agent.
"""
from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime

import httpx

from agents.figma_extractor.http_errors import classify_http_error

FIGMA_API_BASE = "https://api.figma.com/v1"
TIMEOUT_S = 10.0
_RATE_LIMIT_BACKOFF = (0.5, 2.0, 8.0)
_LANE = "lane-5-cache-versioning"

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
        "error_class": error_class,  # rate_limit|auth|not_found|client_error|server_error|timeout|empty|malformed
        "message": (message or "")[:500],
        "attempted_at": _now_iso(),
        "retry_count": retry_count,
    }


async def _fetch_one(client: httpx.AsyncClient, operation_id: str, path_seg: str, file_key: str,
                     params: dict | None = None) -> tuple[dict | None, dict | None]:
    """One endpoint with the Lane 2 v0.2 retry/classification policy.
    Returns (json_dict, None) on success or (None, failure_report) on failure. Never raises."""
    url = f"{FIGMA_API_BASE}/files/{file_key}/{path_seg}"
    retries = 0
    while True:
        try:
            resp = await client.get(url, params=params)
        except httpx.TimeoutException as e:
            if retries < 1:  # timeout: 1 retry
                retries += 1
                await _sleep(1.0)
                continue
            return None, _failure(operation_id, None, "timeout", f"request timed out: {e!r}", retries)
        except httpx.HTTPError as e:  # transport error — treat as timeout-class 1 retry
            if retries < 1:
                retries += 1
                await _sleep(1.0)
                continue
            return None, _failure(operation_id, None, "timeout", f"transport error: {e!r}", retries)

        status = resp.status_code

        if status == 200:
            if not resp.text.strip():  # empty body: no retry
                return None, _failure(operation_id, 200, "empty", "200 with empty response body", retries)
            try:
                return resp.json(), None
            except Exception as e:  # malformed JSON: no retry
                return None, _failure(operation_id, 200, "malformed", f"invalid JSON: {e!r}", retries)

        if status == 429:  # rate_limit: exp backoff, max 3 retries
            if retries < len(_RATE_LIMIT_BACKOFF):
                await _sleep(_RATE_LIMIT_BACKOFF[retries])
                retries += 1
                continue
            return None, _failure(operation_id, 429, "rate_limit", resp.text, retries)

        if status in (401, 403):  # auth / forbidden: no retry
            # 403 sub-classification: 'File not exportable' content-protection lock, Enterprise scope,
            # or generic forbidden. 401 stays 'auth'. Other 403s behave exactly as before.
            ec = classify_http_error(status, resp.text) if status == 403 else "auth"
            return None, _failure(operation_id, status, ec, resp.text, retries)

        if status == 404:  # not_found (v0.2): no retry — bad/deleted file_key
            return None, _failure(operation_id, 404, "not_found", resp.text, retries)

        if 400 <= status < 500:  # other 4xx → client_error (v0.2): no retry
            return None, _failure(operation_id, status, "client_error", resp.text, retries)

        if 500 <= status < 600:  # server_error: 1 retry after 1s
            if retries < 1:
                retries += 1
                await _sleep(1.0)
                continue
            return None, _failure(operation_id, status, "server_error", resp.text, retries)

        # Unexpected (e.g. 3xx) — no retry, classify client_error with true status.
        return None, _failure(operation_id, status, "client_error", f"unexpected status {status}: {resp.text}", retries)


async def _run_tool(operation_id: str, path_seg: str, source_endpoint: str, file_key: str,
                    client: httpx.AsyncClient | None, params: dict | None, unwrap) -> dict:
    """Fetch one endpoint and assemble the Lane 5 single-endpoint envelope."""
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(headers={"X-Figma-Token": _figma_pat()}, timeout=TIMEOUT_S)
    try:
        data, failure = await _fetch_one(client, operation_id, path_seg, file_key, params)
    finally:
        if owns_client:
            await client.aclose()

    extracted_at = _now_iso()
    payload = {} if failure is not None else (unwrap(data) if unwrap else data)
    return {
        "status": "failure" if failure is not None else "success",
        "file_key": file_key,
        "extracted_at": extracted_at,
        "payload": payload,
        "provenance": {
            "lane": _LANE,
            "source_endpoint": source_endpoint,
            "extracted_at": extracted_at,
            "file_key": file_key,
        },
        "failure_report": failure,  # single object or null (spec §4)
    }


# ---- testable cores + agent tools ------------------------------------------
async def run_file_meta(file_key: str, client: httpx.AsyncClient | None = None) -> dict:
    # /meta returns {"file": {...}}; unwrap so payload.version / .last_touched_at are top-level (verbatim file obj).
    return await _run_tool("getFileMeta", "meta", "/meta", file_key, client, None,
                           unwrap=lambda d: d.get("file", d) if isinstance(d, dict) else d)


async def run_file_versions(file_key: str, page_size: int = 30, client: httpx.AsyncClient | None = None) -> dict:
    # /versions returns {"versions": [...], "pagination": {...}} — captured verbatim.
    return await _run_tool("getFileVersions", "versions", "/versions", file_key, client,
                           {"page_size": page_size}, unwrap=None)


async def get_figma_file_meta(file_key: str) -> dict:
    """Cheap change-detection probe for a Figma file (Lane 5). One REST call to
    ``/meta`` (~965 B) returning ``version`` + ``last_touched_at`` + name/role/editor_type,
    WITHOUT pulling the full file tree — use it every run to decide whether to re-extract.
    Never raises: returns ``{status, payload, provenance, failure_report}``.

    Args:
        file_key: The Figma file key, e.g. "8qPSyetzviLR6eF6bkpL44".
    """
    return await run_file_meta(file_key)


async def get_figma_file_versions(file_key: str, page_size: int = 30) -> dict:
    """Historical version list for a Figma file (Lane 5) — reproducibility / audit. One
    REST call to ``/versions`` returning ``versions[]`` (id/created_at/label/description/user)
    + pagination, captured verbatim. Called on-demand, not every run. Never raises: returns
    ``{status, payload, provenance, failure_report}``.

    Args:
        file_key: The Figma file key, e.g. "8qPSyetzviLR6eF6bkpL44".
        page_size: Max versions to request (default 30, Figma max 50).
    """
    return await run_file_versions(file_key, page_size)
