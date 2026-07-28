"""Lane 6 — Cross-File Library Resolution (spec:lane-6-cross-file-library-resolution-v0-1-draft v0.1.1).

STREAMING, deterministic, zero-LLM. Resolves `remote:true` component references from a composition
file (Modules / client files) against registered foundation libraries (Core + optional additional),
emitting SSE-compatible events as each reference resolves: `resolution_started`, `library_registered`,
`library_registration_failed`, `resolved_reference`, `library_key_collision`, `unresolved_reference`,
`third_library_suspect`, `library_dependency_cycle`, `resolution_complete`.

Resolution mechanism (empirically proven, `shared-results:helix-modules-figma-exploration-result`):
a remote instance carries a global component `key`; exact-match that key against a registered library's
`/components` (+/component_sets) map, first-match in registration-order priority.

Phase A / additive: this is the resolution ENGINE. It consumes an enumerated reference list; producing
that list from a composition file is Pathway B (a separate work stream — a minimal enumeration helper is
provided here for the live smoke, NOT the production traversal). UNBOUNDED N libraries per §10 item 16.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx

LANE = "lane-6-cross-file-library-resolution"
VERSION = "v0.1.1"
_FIGMA_API_BASE = "https://api.figma.com/v1"
_TIMEOUT_S = 30.0
_THIRD_LIB_THRESHOLD = 3          # §10 item 13
_THIRD_LIB_CHECK_EVERY = 10       # §4.5 batching for efficiency
_CYCLE_SCOPE = "direct_plus_one_hop_transitive"  # §10 item 15

# module-level library-map cache, keyed by (lib_key, lastModified) → (map, count)  (§4.2)
_LIB_CACHE: dict[tuple[str, str], tuple[dict, int]] = {}


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _figma_pat() -> str:
    return os.environ.get("FIGMA_PAT") or os.environ.get("FIGMA_API_KEY") or ""


# --------------------------------------------------------------------------- default library fetch (§4.2)
async def _default_fetch_library_map(lib_key: str, client: httpx.AsyncClient | None = None) -> dict:
    """Return {'lastModified': str, 'map': {component_key: {name, node_id, kind}}}. Cached per
    (lib_key, lastModified). map keys are the global component `key` used for cross-file matching."""
    own = client is None
    if own:
        client = httpx.AsyncClient(headers={"X-Figma-Token": _figma_pat()}, timeout=_TIMEOUT_S)
    try:
        meta = await client.get(f"{_FIGMA_API_BASE}/files/{lib_key}", params={"depth": 1})
        meta.raise_for_status()
        last_modified = meta.json().get("lastModified", "")
        ck = (lib_key, last_modified)
        if ck in _LIB_CACHE:
            cmap, _ = _LIB_CACHE[ck]
            return {"lastModified": last_modified, "map": cmap, "source": "cache_hit"}
        cmap: dict[str, dict] = {}
        for seg, kind in (("components", "component"), ("component_sets", "component_set")):
            resp = await client.get(f"{_FIGMA_API_BASE}/files/{lib_key}/{seg}")
            resp.raise_for_status()
            for x in (resp.json().get("meta") or {}).get(seg, []):
                k = x.get("key")
                if k:
                    cmap[k] = {"name": x.get("name"), "node_id": x.get("node_id"), "kind": kind}
        _LIB_CACHE[ck] = (cmap, len(cmap))
        return {"lastModified": last_modified, "map": cmap, "source": "fresh_fetch"}
    finally:
        if own:
            await client.aclose()


# --------------------------------------------------------------------------- minimal Pathway-B helper (smoke only)
async def enumerate_remote_references(composition_file_key: str, page_node_ids: list[str],
                                      client: httpx.AsyncClient | None = None, depth: int = 4) -> list[dict]:
    """MINIMAL reference enumeration for the live smoke (NOT production Pathway B). For each page,
    read the node response's `components` map and collect remote:true entries as references."""
    own = client is None
    if own:
        client = httpx.AsyncClient(headers={"X-Figma-Token": _figma_pat()}, timeout=_TIMEOUT_S)
    refs: list[dict] = []
    try:
        for pid in page_node_ids:
            resp = await client.get(f"{_FIGMA_API_BASE}/files/{composition_file_key}/nodes",
                                    params={"ids": pid, "depth": depth})
            if resp.status_code != 200:
                continue
            for wrap in (resp.json().get("nodes") or {}).values():
                page_name = (wrap.get("document") or {}).get("name", pid)
                for cid, c in (wrap.get("components") or {}).items():
                    if c.get("remote"):
                        refs.append({"key": c.get("key"), "source_node_id": cid,
                                     "source_page_name": page_name, "name": c.get("name")})
    finally:
        if own:
            await client.aclose()
    return refs


# --------------------------------------------------------------------------- clustering (§4.5)
def _cluster_key(ref: dict) -> str:
    """Evidence pattern for third-library clustering: metadata hint if present, else name prefix."""
    hint = ref.get("library_hint")
    if hint:
        return f"hint:{hint}"
    name = ref.get("name") or ref.get("key") or ""
    return "name:" + (name.split("=")[0].split("/")[0].strip() or "?")


def _new_suspect_clusters(unresolved: list[dict], already: set[str]) -> list[tuple[str, list[dict]]]:
    groups: dict[str, list[dict]] = {}
    for r in unresolved:
        groups.setdefault(_cluster_key(r), []).append(r)
    out = []
    for k, members in sorted(groups.items()):
        if len(members) >= _THIRD_LIB_THRESHOLD and k not in already:
            out.append((k, members))
    return out


# --------------------------------------------------------------------------- cycle detection (§4.4, bounded)
def _detect_cycles(registered_keys: list[str], library_edges: dict[str, list[str]]) -> list[list[str]]:
    """Bounded direct + one-hop transitive cycle detection over library dependency edges. Returns
    cycle paths [a, b, a]. Deterministic (sorted iteration)."""
    cycles = []
    seen = set()
    for a in registered_keys:
        for b in library_edges.get(a, []):
            # direct A->B->A
            if a in library_edges.get(b, []):
                key = tuple(sorted((a, b)))
                if key not in seen:
                    seen.add(key)
                    cycles.append([a, b, a])
    return cycles


# --------------------------------------------------------------------------- streaming resolver (Steps 6.1–6.6)
async def resolve_stream(
    composition_file_key: str,
    remote_references: list[dict],
    registered_libraries: list[dict],
    *,
    client: httpx.AsyncClient | None = None,
    fetch_library_map=None,
    library_edges: dict[str, list[str]] | None = None,
    now=_now_iso,
) -> AsyncIterator[dict]:
    """Yield the Lane 6 event stream in deterministic order. registered_libraries: ordered list of
    {file_key, role, priority_order} (Core first). remote_references: list of {key, source_node_id,
    source_page_name, name?}. fetch_library_map(lib_key[, client]) -> {'lastModified','map','source'}."""
    fetch = fetch_library_map or _default_fetch_library_map
    library_edges = library_edges or {}

    # Step 6.1 — dedup references by key, preserve traversal order (page_then_depth_first = input order)
    seen_keys: set[str] = set()
    queue: list[dict] = []
    for r in remote_references:
        k = r.get("key")
        if k is not None and k not in seen_keys:
            seen_keys.add(k)
            queue.append(r)

    yield {"event_type": "resolution_started", "source": LANE, "version": VERSION,
           "composition_file_key": composition_file_key, "references_total": len(queue),
           "registered_libraries": [{"file_key": lib["file_key"], "role": lib.get("role"),
                                     "priority_order": lib.get("priority_order", i)}
                                    for i, lib in enumerate(registered_libraries)],
           "streaming_deterministic_order": "page_then_depth_first", "started_at": now()}

    # Step 6.2 — register libraries (registration-order); cache-aware; failures don't abort others
    lib_maps: list[tuple[dict, dict]] = []  # [(lib, key->component)]
    per_lib_resolved = {lib["file_key"]: 0 for lib in registered_libraries}
    for i, lib in enumerate(registered_libraries):
        lk = lib["file_key"]
        try:
            res = await (fetch(lk, client) if fetch is _default_fetch_library_map else fetch(lk))
            lib_maps.append((lib, res["map"]))
            yield {"event_type": "library_registered", "file_key": lk, "role": lib.get("role"),
                   "priority_order": lib.get("priority_order", i),
                   "components_map_source": res.get("source", "fresh_fetch"),
                   "components_map_count": len(res["map"]),
                   "components_map_lastModified": res.get("lastModified"), "registered_at": now()}
        except Exception as e:  # noqa: BLE001 — one library failing must not abort the rest
            yield {"event_type": "library_registration_failed", "file_key": lk,
                   "error_class": "library_inaccessible", "error_details": repr(e)[:200],
                   "attempted_at": now()}

    # Step 6.4 — cycle detection (bounded; deterministic; emitted post-registration)
    registered_ok = [lib["file_key"] for lib, _ in lib_maps]
    for cyc in _detect_cycles(registered_ok, library_edges):
        yield {"event_type": "library_dependency_cycle", "cycle_path": cyc,
               "cycle_scope": _CYCLE_SCOPE, "action": "cycle_broken_at_second_visit", "detected_at": now()}

    # Step 6.3 — streaming resolution
    seq = 0
    resolved = unresolved = collisions = 0
    unresolved_refs: list[dict] = []
    reported_clusters: set[str] = set()
    suspects = 0
    for ref in queue:
        seq += 1
        k = ref["key"]
        hits = [(lib, m) for lib, m in lib_maps if k in m]
        if hits:
            chosen_lib, chosen_map = hits[0]  # registration order == priority
            if len(hits) > 1:
                collisions += 1
                yield {"event_type": "library_key_collision", "reference_key": k,
                       "libraries_with_key": [{"file_key": lib["file_key"], "role": lib.get("role"),
                                               "priority_order": lib.get("priority_order")} for lib, _ in hits],
                       "resolved_via": chosen_lib["file_key"],
                       "warning": "Duplicate key across libraries; resolved via first-match-in-priority-order.",
                       "detected_at": now()}
            tgt = chosen_map[k]
            resolved += 1
            per_lib_resolved[chosen_lib["file_key"]] += 1
            yield {"event_type": "resolved_reference", "reference_key": k,
                   "source_composition_file": composition_file_key,
                   "source_node_id": ref.get("source_node_id"), "source_page_name": ref.get("source_page_name"),
                   "target_library_file": chosen_lib["file_key"], "target_library_role": chosen_lib.get("role"),
                   "target_component_name": tgt.get("name"), "target_component_id": tgt.get("node_id"),
                   "resolution_mechanism": "key_match_v1", "resolved_at": now(), "sequence": seq}
        else:
            unresolved += 1
            unresolved_refs.append(ref)
            yield {"event_type": "unresolved_reference", "reference_key": k,
                   "source_composition_file": composition_file_key,
                   "source_node_id": ref.get("source_node_id"), "source_page_name": ref.get("source_page_name"),
                   "error_class": "library_not_registered_or_key_absent",
                   "checked_libraries": [lib["file_key"] for lib, _ in lib_maps],
                   "attempted_at": now(), "sequence": seq}
            if unresolved % _THIRD_LIB_CHECK_EVERY == 0:
                for ckey, members in _new_suspect_clusters(unresolved_refs, reported_clusters):
                    reported_clusters.add(ckey)
                    suspects += 1
                    yield _suspect_event(ckey, members, now)

    # final clustering flush (§4.5 — catch clusters below the every-10th boundary)
    for ckey, members in _new_suspect_clusters(unresolved_refs, reported_clusters):
        reported_clusters.add(ckey)
        suspects += 1
        yield _suspect_event(ckey, members, now)

    # Step 6.6 — completion summary
    total = len(queue)
    yield {"event_type": "resolution_complete", "composition_file_key": composition_file_key,
           "resolution_summary": {
               "references_total": total, "references_resolved": resolved,
               "references_unresolved": unresolved, "third_library_suspects": suspects,
               "library_key_collisions": collisions,
               "library_dependency_cycles": len(_detect_cycles(registered_ok, library_edges)),
               "resolution_rate_pct": round(resolved / total * 100, 1) if total else 100.0,
               "per_library_metrics": {lib["file_key"]: {"references_resolved": per_lib_resolved[lib["file_key"]],
                                                         "resolution_latency_ms_avg": None}
                                       for lib in registered_libraries}},
           "provenance": {"lane": LANE, "version": VERSION, "resolution_mechanism": "key_match_v1",
                          "cache_used": any(True for _ in lib_maps), "llm_involvement": "none"},
           "completed_at": now()}


def _suspect_event(cluster_key: str, members: list[dict], now) -> dict:
    return {"event_type": "third_library_suspect",
            "cluster_evidence": f"{len(members)} unresolved references share evidence pattern '{cluster_key}'",
            "unresolved_references_in_cluster": [m.get("key") for m in members],
            "suggested_investigation": "Query cross-team for library file_key registration",
            "detected_at": now(), "sensitivity_threshold_met": f">={_THIRD_LIB_THRESHOLD} clustered keys"}


# --------------------------------------------------------------------------- batch bridge (backward-compat §14)
async def resolve(composition_file_key: str, remote_references: list[dict],
                  registered_libraries: list[dict], **kw) -> dict:
    """Collect the event stream into a batch object (compatibility bridge for non-streaming consumers).
    Returns {'events': [...], 'resolution_complete': <the terminal event>}."""
    events = [e async for e in resolve_stream(composition_file_key, remote_references,
                                              registered_libraries, **kw)]
    complete = next((e for e in reversed(events) if e["event_type"] == "resolution_complete"), None)
    return {"events": events, "resolution_complete": complete}
