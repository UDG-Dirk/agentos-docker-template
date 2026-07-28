"""Composition-mode extraction + per-client orchestration (Path C rev.3.1 §5).

Two-mode architecture: Library mode (deterministic.py — published-library files like Core) and
Composition mode (this module — client/Modules files whose organisms live as page frames).

`run_composition_extraction`: Pathway B traversal + Lane 5 meta + Lane 6 streaming resolution
(NOT Lane 2 — returns 0/0/0 on composition files; Lane 7 optional — compositions usually don't
author tokens). Emits Lane 6 events through an optional `emit` callback (SSE forwarding).

`extract_client_design_system`: the per-client entry point — Library-mode Core (cached per
(core_key, lastModified)) + Composition-mode client + Lane 6 with registered libraries
[core] + additional_library_keys (UNBOUNDED N). Deterministic, zero-LLM.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime

import httpx

from agents.figma_extractor import cross_file_resolution as lane6
from agents.figma_extractor import pathway_b_traversal as pb
from agents.figma_extractor.token_catalog import RECONCILIATION_CONTRACT

_FIGMA_API_BASE = "https://api.figma.com/v1"
_TIMEOUT_S = 30.0

# Core-extraction cache, keyed by (core_file_key, core_lastModified) — MLOps/FinOps win (Path C §5)
_CORE_CACHE: dict[tuple[str, str], dict] = {}


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _figma_pat() -> str:
    return os.environ.get("FIGMA_PAT") or os.environ.get("FIGMA_API_KEY") or ""


async def _fetch_last_modified(file_key: str, client: httpx.AsyncClient) -> str:
    resp = await client.get(f"{_FIGMA_API_BASE}/files/{file_key}", params={"depth": 1})
    resp.raise_for_status()
    return resp.json().get("lastModified", "")


# --------------------------------------------------------------------------- composition-mode extractor
async def run_composition_extraction(
    file_key: str, *, registered_libraries: list[dict] | None = None,
    client: httpx.AsyncClient | None = None, file_role: str = "client",
    emit=None, now=_now_iso,
    pathway_b=None, resolve=None, library_edges: dict | None = None,
) -> dict:
    """Composition-mode extraction. Pathway B -> remote refs -> Lane 6 streaming resolution.
    `emit` (optional): called per Lane 6 event for SSE forwarding. Injectables (pathway_b, resolve)
    for tests. Returns a unified composition-mode output. Never raises past the boundary."""
    registered_libraries = registered_libraries or []
    pb_run = pathway_b or (lambda fk: pb.run_pathway_b(fk, client=client))

    # Step 1 — Pathway B traversal (composition tree + remote references)
    pb_result = await (pb_run(file_key) if pathway_b is None else pathway_b(file_key))

    # Step 2 — Lane 6 streaming resolution over the remote references
    remote_refs = pb_result.get("remote_references", [])
    events: list[dict] = []
    if resolve is not None:
        lane6_out = await resolve(file_key, remote_refs, registered_libraries)
        events = lane6_out.get("events", [])
    else:
        async for ev in lane6.resolve_stream(file_key, remote_refs, registered_libraries,
                                              client=client, library_edges=library_edges, now=now):
            events.append(ev)
            if emit is not None:
                emit(ev)  # SSE forwarding
    complete = next((e for e in reversed(events) if e.get("event_type") == "resolution_complete"), None)

    status = "success"
    if pb_result.get("pathway_b_status") != "success":
        status = "partial"
    if complete and complete["resolution_summary"]["references_unresolved"] > 0:
        status = "partial" if status != "failure" else status

    return {
        "extraction_mode": "composition",
        "file_key": file_key,
        "file_role": file_role,
        "status": status,
        "composition_tree": pb_result.get("composition_tree", []),
        "pages": pb_result.get("pages", []),
        "page_count": pb_result.get("page_count", 0),
        "frame_count_total": pb_result.get("frame_count_total", 0),
        "remote_reference_count": pb_result.get("remote_reference_count", 0),
        "pathway_b_status": pb_result.get("pathway_b_status"),
        "pathway_b_failures": pb_result.get("failure_reports", []),
        "resolution_events": events,
        "resolution_summary": (complete or {}).get("resolution_summary") if complete else None,
        "reconciliation_contract": RECONCILIATION_CONTRACT,
        "provenance": {"extraction_mode": "composition", "traversal": "pathway-b",
                       "resolution": "lane-6-streaming", "llm_involvement": "none"},
        "extracted_at": now(),
    }


# --------------------------------------------------------------------------- per-client orchestration
async def extract_client_design_system(
    core_file_key: str, client_file_key: str, *, additional_library_keys: list[str] | None = None,
    freshness_threshold_days: int = 7, client: httpx.AsyncClient | None = None,
    core_extractor=None, composition_extractor=None, core_last_modified=None,
    emit=None, now=_now_iso, use_core_cache: bool = True,
) -> dict:
    """Per-client entry point (Path C rev.3.1 §5). Library-mode Core (cached) + Composition-mode
    client + Lane 6 with registered libraries [core]+additional (UNBOUNDED N). Injectables for tests."""
    additional_library_keys = additional_library_keys or []
    own = client is None and (core_extractor is None or composition_extractor is None or core_last_modified is None)
    if own:
        client = httpx.AsyncClient(headers={"X-Figma-Token": _figma_pat()}, timeout=_TIMEOUT_S)
    try:
        # 1 — Library-mode Core extraction with caching keyed by (core_key, lastModified)
        if core_last_modified is None:
            core_last_modified = await _fetch_last_modified(core_file_key, client)
        ck = (core_file_key, core_last_modified)
        core_cache_hit = use_core_cache and ck in _CORE_CACHE
        if core_cache_hit:
            core_extraction = _CORE_CACHE[ck]
        else:
            if core_extractor is None:
                from agents.figma_extractor.agent import figma_mcp_tools
                from agents.figma_extractor.deterministic import run_deterministic_extraction

                async def _default_core(fk):
                    async with figma_mcp_tools:
                        return await run_deterministic_extraction(fk, session=figma_mcp_tools.session,
                                                                  do_assets=False)
                core_extraction = await _default_core(core_file_key)
            else:
                core_extraction = await core_extractor(core_file_key)
            if use_core_cache:
                _CORE_CACHE[ck] = core_extraction

        # 2 — registered libraries: Core (priority 0) + additional in caller order (UNBOUNDED N)
        registered = [{"file_key": core_file_key, "role": "core_foundation", "priority_order": 0}]
        for i, k in enumerate(additional_library_keys, start=1):
            registered.append({"file_key": k, "role": "additional_library", "priority_order": i})

        # 3 — Composition-mode client extraction (feeds remote refs into Lane 6)
        if composition_extractor is None:
            client_extraction = await run_composition_extraction(
                client_file_key, registered_libraries=registered, client=client, emit=emit, now=now)
        else:
            client_extraction = await composition_extractor(client_file_key, registered)

        return {
            "workflow": "extract_client_design_system",
            "core_file_key": core_file_key,
            "client_file_key": client_file_key,
            "additional_library_keys": additional_library_keys,
            "registered_libraries": registered,
            "core_extraction_cache": "hit" if core_cache_hit else "miss",
            "core_extraction": core_extraction,
            "client_extraction": client_extraction,
            "reconciliation_contract": RECONCILIATION_CONTRACT,
            "provenance": {"orchestrator": "extract_client_design_system", "llm_involvement": "none"},
            "extracted_at": now(),
        }
    finally:
        if own:
            await client.aclose()
