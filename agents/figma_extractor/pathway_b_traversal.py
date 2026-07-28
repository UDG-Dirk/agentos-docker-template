"""Pathway B — composition-file page-frame traversal (Path C rev.3.1 Mode B).

Deterministic, zero-LLM. Walks a composition file's pages (page order, then depth-first — matches
Lane 6 §3 traversal order for cross-run consistency), emitting a bounded `composition_tree` +
the `remote:true` component references Lane 6 consumes. The current published-library extractor
returns nothing on composition files (Modules = 0/0/0 published); organisms live as page frames,
reachable only here.

Anti-fabrication: every emitted frame/reference carries its source node_id + page. Fail loud:
file_inaccessible, empty_composition_file, traversal_depth_exceeded, malformed_node_data.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime

import httpx

LANE = "pathway-b-traversal"
_FIGMA_API_BASE = "https://api.figma.com/v1"
_TIMEOUT_S = 30.0
_DEFAULT_MAX_DEPTH = 20
_PAGE_FETCH_DEPTH = 4          # how deep to pull each page's subtree in one REST call
_MAX_FRAMES_PER_PAGE = 500     # bound against pathological pages
_FRAME_TYPES = {"FRAME", "SECTION", "COMPONENT", "COMPONENT_SET", "INSTANCE", "GROUP"}


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _figma_pat() -> str:
    return os.environ.get("FIGMA_PAT") or os.environ.get("FIGMA_API_KEY") or ""


async def _default_fetch_pages(file_key: str, client: httpx.AsyncClient) -> list[dict]:
    resp = await client.get(f"{_FIGMA_API_BASE}/files/{file_key}", params={"depth": 1})
    resp.raise_for_status()
    doc = (resp.json().get("document") or {})
    return [{"id": c.get("id"), "name": c.get("name")} for c in (doc.get("children") or [])]


async def _default_fetch_node(file_key: str, node_id: str, client: httpx.AsyncClient, depth: int) -> dict:
    resp = await client.get(f"{_FIGMA_API_BASE}/files/{file_key}/nodes",
                            params={"ids": node_id, "depth": depth})
    resp.raise_for_status()
    return (resp.json().get("nodes") or {}).get(node_id) or {}


def _walk_frames(node: dict, page_name: str, max_depth: int, depth: int = 0):
    """Depth-first, children in order. Yields (frame_dict, hit_depth_limit)."""
    if depth > max_depth:
        yield None, True
        return
    ntype = node.get("type")
    if ntype in _FRAME_TYPES:
        yield {"id": node.get("id"), "name": node.get("name"), "type": ntype, "depth": depth}, False
    for child in (node.get("children") or []):
        yield from _walk_frames(child, page_name, max_depth, depth + 1)


async def run_pathway_b(file_key: str, *, client: httpx.AsyncClient | None = None,
                        fetch_pages=None, fetch_node=None, max_depth: int = _DEFAULT_MAX_DEPTH,
                        page_fetch_depth: int = _PAGE_FETCH_DEPTH) -> dict:
    """Traverse a composition file. Returns composition_tree + remote_references + status. Never raises
    past the boundary — surfaces failures as failure_reports (spec fail-loud). Injectable for tests."""
    own = client is None
    if own and fetch_pages is None:
        client = httpx.AsyncClient(headers={"X-Figma-Token": _figma_pat()}, timeout=_TIMEOUT_S)
    fp = fetch_pages or (lambda fk: _default_fetch_pages(fk, client))
    fn = fetch_node or (lambda fk, nid, d: _default_fetch_node(fk, nid, client, d))
    failure_reports: list[dict] = []
    gaps: list[str] = []
    status = "success"
    pages_summary: list[dict] = []
    composition_tree: list[dict] = []
    remote_references: list[dict] = []

    def fail(error_class, message):
        failure_reports.append({"endpoint": "pathway-b", "error_class": error_class,
                                "message": message[:300], "attempted_at": _now_iso()})

    try:
        try:
            pages = await fp(file_key)
        except Exception as e:  # noqa: BLE001
            fail("file_inaccessible", f"{file_key}: {e!r}")
            return _result(file_key, "failure", pages_summary, composition_tree, remote_references,
                           failure_reports, gaps)
        if not pages:
            fail("empty_composition_file", "no pages in document.children")
            return _result(file_key, "partial", pages_summary, composition_tree, remote_references,
                           failure_reports, gaps)

        for page in pages:  # page order (deterministic)
            pid, pname = page.get("id"), page.get("name")
            try:
                wrap = await fn(file_key, pid, page_fetch_depth)
            except Exception as e:  # noqa: BLE001
                fail("malformed_node_data", f"page {pname} ({pid}): {e!r}")
                gaps.append(f"page {pname} unreadable")
                status = "partial" if status != "failure" else status
                continue
            doc = wrap.get("document") or {}
            frames = []
            depth_hit = False
            for fr, hit in _walk_frames(doc, pname, max_depth):
                if hit:
                    depth_hit = True
                    continue
                frames.append(fr)
                if len(frames) >= _MAX_FRAMES_PER_PAGE:
                    depth_hit = True
                    break
            if depth_hit:
                status = "partial" if status != "failure" else status
                fail("traversal_depth_exceeded", f"page {pname}: depth>{max_depth} or >{_MAX_FRAMES_PER_PAGE} frames")
                gaps.append(f"page {pname} traversal bounded (depth/frame cap)")
            comps = wrap.get("components") or {}
            remote = [(cid, c) for cid, c in comps.items() if c.get("remote")]
            local = [(cid, c) for cid, c in comps.items() if not c.get("remote")]
            for _cid, c in remote:
                remote_references.append({"key": c.get("key"), "source_node_id": _cid,
                                          "source_page_name": pname, "name": c.get("name")})
            pages_summary.append({"page_id": pid, "page_name": pname, "frame_count": len(frames),
                                  "remote_ref_count": len(remote), "local_component_count": len(local),
                                  "depth_bounded": depth_hit})
            composition_tree.append({"page_id": pid, "page_name": pname, "frames": frames})
        return _result(file_key, status, pages_summary, composition_tree, remote_references,
                       failure_reports, gaps)
    finally:
        if own and fetch_pages is None:
            await client.aclose()


def _result(file_key, status, pages_summary, composition_tree, remote_references, failure_reports, gaps):
    return {
        "lane": LANE, "file_key": file_key, "pathway_b_status": status,
        "pages": pages_summary,
        "page_count": len(pages_summary),
        "frame_count_total": sum(p["frame_count"] for p in pages_summary),
        "remote_references": remote_references,
        "remote_reference_count": len(remote_references),
        "composition_tree": composition_tree,
        "failure_reports": failure_reports, "gaps": gaps,
        "provenance": {"lane": LANE, "traversal_order": "page_then_depth_first", "llm_involvement": "none"},
        "extracted_at": _now_iso(),
    }
