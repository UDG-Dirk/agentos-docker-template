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

import asyncio
import os
from datetime import UTC, datetime

import httpx

from agents.figma_extractor.http_errors import FileExportDisabledError, raise_for_figma_status

# (2,5,12,30)s × 4 retries — strengthened from (1,3,8)×3 after DGX (36 pages) 429'd 32/36.
# Only kicks in on 429/timeout/5xx, so Helix-scale (mostly first-try 200) latency is unchanged.
_BACKOFF = (2.0, 5.0, 12.0, 30.0)
# Adaptive inter-page pacing: spread Figma API load on LARGE files only. Files at/under the threshold
# (Helix_Modules = 17 pages) get NO pacing → baseline latency preserved (<20% constraint).
_PACING_PAGE_THRESHOLD = 20
_INTER_PAGE_PACE_S = 0.5


async def _sleep(seconds: float) -> None:  # indirection so tests can stub the backoff wait
    await asyncio.sleep(seconds)


async def _get_with_backoff(client: httpx.AsyncClient, url: str, params: dict) -> httpx.Response:
    """GET with (1,3,8)s backoff-retry on 429 / timeout / 5xx (composition-mode page fetches share the
    PAT with Core extraction, so the tail gets throttled). On exhaustion, raises — the caller records a
    fail-loud failure_report (P1a discipline preserved; retries reduce noise, never hide failures)."""
    for attempt in range(len(_BACKOFF) + 1):
        try:
            resp = await client.get(url, params=params)
        except httpx.TimeoutException:
            if attempt == len(_BACKOFF):
                raise
            await _sleep(_BACKOFF[attempt])
            continue
        if attempt < len(_BACKOFF) and (resp.status_code == 429 or 500 <= resp.status_code < 600):
            await _sleep(_BACKOFF[attempt])
            continue
        # export-lock 403 -> FileExportDisabledError (distinct); other errors surface as before.
        raise_for_figma_status(resp)  # final 429 / other client errors -> malformed_node_data
        return resp
    raise RuntimeError("unreachable")  # pragma: no cover


LANE = "pathway-b-traversal"
_FIGMA_API_BASE = "https://api.figma.com/v1"
_TIMEOUT_S = 30.0
_DEFAULT_MAX_DEPTH = 20
_PAGE_FETCH_DEPTH = 4  # how deep to pull each page's subtree in one REST call
_MAX_FRAMES_PER_PAGE = 500  # bound against pathological pages
_FRAME_TYPES = {"FRAME", "SECTION", "COMPONENT", "COMPONENT_SET", "INSTANCE", "GROUP"}
# Track D v0.2.5 — TEXT nodes are emitted too so their Rank-1 text_content surfaces (additive; no
# existing fixture contains TEXT, so frame_count assertions are unaffected — BC-A).
_EMIT_TYPES = _FRAME_TYPES | {"TEXT"}
_LAYOUT_MODES = {"HORIZONTAL", "VERTICAL", "GRID"}


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# --------------------------------------------------------------------------- #
# Track D v0.2.5 — Rank 1 (text) + Rank 2 (auto-layout) enrichment
#
# Additive optional fields (BC-A), nested per category (F2). Set ONLY when the node type + the
# Figma response actually carry them — never hallucinate defaults; absent means the key is omitted
# (consumers use `if node.get("text_content"):`). Totally defensive: on unexpected shapes they
# return None rather than raise (SP-6 fail-loud is enforced per-node by the caller, which flags any
# node that raised during enrichment as malformed_node_data).
# --------------------------------------------------------------------------- #


def _extract_text_content(node: dict) -> dict | None:
    """Rank 1 — text_content for TEXT nodes. None when not a TEXT node / no characters."""
    if node.get("type") != "TEXT":
        return None
    chars = node.get("characters")
    if not chars:
        return None
    style = node.get("style") if isinstance(node.get("style"), dict) else {}
    line_height = None
    if style.get("lineHeightPx") is not None or style.get("lineHeightPercent") is not None:
        line_height = {
            k: v
            for k, v in {
                "unit": style.get("lineHeightUnit"),
                "px": style.get("lineHeightPx"),
                "percent": style.get("lineHeightPercent"),
            }.items()
            if v is not None
        }
    tstyle = {
        k: v
        for k, v in {
            "font_family": style.get("fontFamily"),
            "font_size": style.get("fontSize"),
            "font_weight": style.get("fontWeight"),
            "line_height": line_height,
            "text_align": style.get("textAlignHorizontal"),
        }.items()
        if v is not None
    }
    out: dict = {"characters": chars}
    if tstyle:
        out["style"] = tstyle
    return out


def _extract_auto_layout(node: dict) -> dict | None:
    """Rank 2 — auto_layout for nodes with an auto-layout mode set. None when layoutMode is NONE/absent."""
    mode = node.get("layoutMode")
    if mode not in _LAYOUT_MODES:
        return None
    return {
        k: v
        for k, v in {
            "layout_mode": mode,
            "padding_top": node.get("paddingTop"),
            "padding_right": node.get("paddingRight"),
            "padding_bottom": node.get("paddingBottom"),
            "padding_left": node.get("paddingLeft"),
            "item_spacing": node.get("itemSpacing"),
            "primary_axis_sizing": node.get("primaryAxisSizingMode"),
            "counter_axis_sizing": node.get("counterAxisSizingMode"),
            "primary_axis_align": node.get("primaryAxisAlignItems"),
            "counter_axis_align": node.get("counterAxisAlignItems"),
        }.items()
        if v is not None
    }


def _extract_component_property_definitions(node: dict) -> dict | None:
    """Rank 3 — componentPropertyDefinitions (the variant/prop SCHEMA), on COMPONENT_SET / COMPONENT.

    TD-2: present on COMPONENT_SET nodes (1/file). Standalone COMPONENTs may also carry definitions
    (boolean/text props), so both types are accepted. None when the node type is irrelevant / empty.
    """
    if node.get("type") not in ("COMPONENT_SET", "COMPONENT"):
        return None
    defs = node.get("componentPropertyDefinitions")
    if not isinstance(defs, dict) or not defs:
        return None
    out: dict = {}
    for name, d in defs.items():
        if not isinstance(d, dict):
            continue
        entry = {k: v for k, v in {
            "type": d.get("type"),
            "default_value": d.get("defaultValue"),
            "variant_options": d.get("variantOptions") or None,  # VARIANT type only
        }.items() if v is not None}
        out[name] = entry
    return out or None


def _extract_component_properties(node: dict) -> dict | None:
    """Rank 3 — componentProperties (the prop VALUES applied on an INSTANCE). None when absent."""
    if node.get("type") != "INSTANCE":
        return None
    props = node.get("componentProperties")
    if not isinstance(props, dict) or not props:
        return None
    out: dict = {}
    for name, d in props.items():
        if isinstance(d, dict):
            out[name] = {k: v for k, v in {"type": d.get("type"), "value": d.get("value")}.items()
                         if v is not None}
    return out or None


def _extract_variant_properties(node: dict) -> dict | None:
    """Rank 3 — variantProperties (variant-axis → value on a VARIANT INSTANCE). [U] TD-2 samples had
    0; newer Figma folds variants into componentProperties. Retained when present. None otherwise."""
    if node.get("type") != "INSTANCE":
        return None
    vp = node.get("variantProperties")
    return dict(vp) if isinstance(vp, dict) and vp else None


def _enrich_frame(node: dict, frame: dict) -> None:
    """Attach Rank 1/2/3 optional fields to `frame` in place. On any parse error, flag the frame
    (``enrichment_error``) so the caller can fail loud per-node (SP-6) without crashing the walk."""
    try:
        tc = _extract_text_content(node)
        if tc:
            frame["text_content"] = tc
        al = _extract_auto_layout(node)
        if al:
            frame["auto_layout"] = al
        cpd = _extract_component_property_definitions(node)  # Rank 3 — schema (COMPONENT_SET/COMPONENT)
        if cpd:
            frame["component_property_definitions"] = cpd
        cp = _extract_component_properties(node)             # Rank 3 — values (INSTANCE)
        if cp:
            frame["component_properties"] = cp
        vp = _extract_variant_properties(node)               # Rank 3 — variant axes (INSTANCE)
        if vp:
            frame["variant_properties"] = vp
    except Exception as e:  # noqa: BLE001 — enrichment must never break traversal (SP-6 per-node)
        frame["enrichment_error"] = repr(e)[:200]


def _figma_pat() -> str:
    return os.environ.get("FIGMA_PAT") or os.environ.get("FIGMA_API_KEY") or ""


async def _default_fetch_pages(file_key: str, client: httpx.AsyncClient) -> list[dict]:
    resp = await _get_with_backoff(client, f"{_FIGMA_API_BASE}/files/{file_key}", {"depth": 1})
    doc = resp.json().get("document") or {}
    return [{"id": c.get("id"), "name": c.get("name")} for c in (doc.get("children") or [])]


async def _default_fetch_node(file_key: str, node_id: str, client: httpx.AsyncClient, depth: int) -> dict:
    resp = await _get_with_backoff(
        client, f"{_FIGMA_API_BASE}/files/{file_key}/nodes", {"ids": node_id, "depth": depth}
    )
    return (resp.json().get("nodes") or {}).get(node_id) or {}


def _walk_frames(node: dict, page_name: str, max_depth: int, depth: int = 0):
    """Depth-first, children in order. Yields (frame_dict, hit_depth_limit)."""
    if depth > max_depth:
        yield None, True
        return
    ntype = node.get("type")
    if ntype in _EMIT_TYPES:
        frame = {"id": node.get("id"), "name": node.get("name"), "type": ntype, "depth": depth}
        _enrich_frame(node, frame)  # Track D v0.2.5 — additive Rank 1/2 optional fields (BC-A)
        yield frame, False
    for child in node.get("children") or []:
        yield from _walk_frames(child, page_name, max_depth, depth + 1)


async def run_pathway_b(
    file_key: str,
    *,
    client: httpx.AsyncClient | None = None,
    fetch_pages=None,
    fetch_node=None,
    max_depth: int = _DEFAULT_MAX_DEPTH,
    page_fetch_depth: int = _PAGE_FETCH_DEPTH,
) -> dict:
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
        failure_reports.append(
            {"endpoint": "pathway-b", "error_class": error_class, "message": message[:300], "attempted_at": _now_iso()}
        )

    try:
        try:
            pages = await fp(file_key)
        except FileExportDisabledError as e:  # content-protection lock — distinct, actionable
            fail("file_export_disabled", f"{file_key}: {e.figma_message}")
            return _result(
                file_key, "failure", pages_summary, composition_tree, remote_references, failure_reports, gaps
            )
        except Exception as e:  # noqa: BLE001
            fail("file_inaccessible", f"{file_key}: {e!r}")
            return _result(
                file_key, "failure", pages_summary, composition_tree, remote_references, failure_reports, gaps
            )
        if not pages:
            fail("empty_composition_file", "no pages in document.children")
            return _result(
                file_key, "partial", pages_summary, composition_tree, remote_references, failure_reports, gaps
            )

        pace = len(pages) > _PACING_PAGE_THRESHOLD  # adaptive: only large files pace between fetches
        for _i, page in enumerate(pages):  # page order (deterministic)
            if _i and pace:
                await _sleep(_INTER_PAGE_PACE_S)  # deterministic spacing; spreads API load, defuses 429s
            pid, pname = page.get("id"), page.get("name")
            try:
                wrap = await fn(file_key, pid, page_fetch_depth)
            except FileExportDisabledError as e:  # export lock mid-traversal -> fail loud, distinct
                fail("file_export_disabled", f"page {pname} ({pid}): {e.figma_message}")
                status = "failure"
                break
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
                if "enrichment_error" in fr:  # SP-6 per-node fail-loud (Track D v0.2.5)
                    fail(
                        "malformed_node_data",
                        f"page {pname} node {fr.get('id')}: enrichment parse error: {fr['enrichment_error']}",
                    )
                    gaps.append(f"page {pname} node {fr.get('id')} enrichment unparsed")
                    status = "partial" if status != "failure" else status
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
                remote_references.append(
                    {"key": c.get("key"), "source_node_id": _cid, "source_page_name": pname, "name": c.get("name")}
                )
            pages_summary.append(
                {
                    "page_id": pid,
                    "page_name": pname,
                    "frame_count": len(frames),
                    "remote_ref_count": len(remote),
                    "local_component_count": len(local),
                    "depth_bounded": depth_hit,
                }
            )
            composition_tree.append({"page_id": pid, "page_name": pname, "frames": frames})
        return _result(file_key, status, pages_summary, composition_tree, remote_references, failure_reports, gaps)
    finally:
        if own and fetch_pages is None:
            await client.aclose()


def _result(file_key, status, pages_summary, composition_tree, remote_references, failure_reports, gaps):
    return {
        "lane": LANE,
        "file_key": file_key,
        "pathway_b_status": status,
        "pages": pages_summary,
        "page_count": len(pages_summary),
        "frame_count_total": sum(p["frame_count"] for p in pages_summary),
        "remote_references": remote_references,
        "remote_reference_count": len(remote_references),
        "composition_tree": composition_tree,
        "failure_reports": failure_reports,
        "gaps": gaps,
        "provenance": {"lane": LANE, "traversal_order": "page_then_depth_first", "llm_involvement": "none"},
        "extracted_at": _now_iso(),
    }
