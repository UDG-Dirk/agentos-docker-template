"""
HELIX Step 1 — Deterministic Figma Extractor
============================================

Spec: `helix-poc-agno:spec:figma-extractor-deterministic-v0-1-draft` (RATIFIED).
Replaces the LLM-orchestrated figma-extractor agent with a deterministic Python step —
ZERO LLM inference in the extraction path (the LLM was the fabrication surface: it invented
component names not in the authored roster).

Orchestration (spec §4), fixed order:
  1.1 Lane 5  get_figma_file_meta        → version/last_touched anchor
  1.2 Lane 2  get_figma_semantic_layer   → authoritative roster (component_sets/components/styles)
  1.3 Framelink get_figma_data (ClientSession.call_tool, NO LLM) per component_set → variants + globalVars
  1.4 Lane 3  get_figma_binding_topology → node→VariableID bindings
  1.5 Framelink download_figma_images (ClientSession.call_tool) for icon-classified sets → assets on disk
  1.6 Deterministic typo detection (Levenshtein + dictionary; no LLM)
  1.7 Compose

Output (Dirk-ratified Option 1, reconciling spec §5 with the Token Normalizer contract):
the step emits a **FigmaExtractionResult-shaped dict** (so normalize's runtime coercion +
the 57 frozen-fixture Normalizer tests are untouched) with the full §5 rich envelope nested
under key ``deterministic_extraction`` (FER uses extra=ignore, so normalize drops it cleanly;
downstream §5 consumers read it). Tokens are distilled DETERMINISTICALLY from get_figma_data
globalVars (resolved values, opaque styleId names — NO invented semantic names) + Lane 2's
named styles. Provenance marks ``llm_involvement: "none"``.

All lane/Framelink callables are injectable → fully unit-testable with mocks; defaults wire
the real Lane funcs + the Framelink MCP session.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import yaml

from agents.figma_extractor.models import (
    AssetEntry,
    ComponentEntry,
    ComponentVariant,
    FigmaExtractionResult,
    PageInfo,
    TokenEntry,
)
from agents.figma_extractor.token_catalog import run_token_catalog

PIPELINE = "figma-extractor-deterministic-v0-1"
_LANE_VERSIONS = {"lane_2": "v0.2", "lane_3": "v0.1", "lane_5": "v0.1"}
_NODE_CONCURRENCY = 2  # spec §10.3 (lowered 3→2: empirical rate-limit relief for heavy subtrees;
#                        §10.3 permits adjustment on empirical need — see rate-limit-hardening task)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------------------- typo detection
def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


# Known-good design-system vocabulary (deterministic dictionary; extend as the DS grows).
_DICTIONARY = {
    "dropdown", "disabled", "default", "hover", "active", "focus", "toggle", "checkbox",
    "button", "input", "radio", "accordion", "tabs", "card", "chip", "divider", "link",
    "icon", "solid", "outline", "ghost", "primary", "secondary", "elements", "unsorted",
    "list", "overline", "label", "helper", "group", "typography", "surface", "border",
    "small", "medium", "large", "content", "block", "heading", "image", "video",
}


def _detect_typos(named_nodes: list[tuple[str, str]], threshold: int = 1) -> list[dict]:
    """Deterministic typo detection: for each token in each name, if it's within `threshold`
    Levenshtein of a dictionary word but not IN the dictionary, flag it. No LLM.
    `named_nodes` = list of (name, node_id). Returns sorted, stable list."""
    import re

    out: dict[tuple, dict] = {}
    for name, node_id in named_nodes:
        for raw_tok in re.split(r"[\s/._\-=,]+", name or ""):
            tok = raw_tok.strip().lower()
            if len(tok) < 4 or tok in _DICTIONARY or not tok.isalpha():
                continue
            best = None
            for word in _DICTIONARY:
                if abs(len(word) - len(tok)) > threshold:
                    continue
                d = _levenshtein(tok, word)
                if d <= threshold and (best is None or d < best[1]):
                    best = (word, d)
            if best:
                key = (raw_tok, node_id)
                out[key] = {
                    "original": raw_tok,
                    "suggestion": best[0],
                    "node_id": node_id,
                    "confidence": round(1.0 - best[1] / max(len(raw_tok), 1), 2),
                    "detection_method": "levenshtein+dictionary",
                }
    return sorted(out.values(), key=lambda t: (t["node_id"], t["original"]))


# --------------------------------------------------------------------------- token distillation
_COLOR_PREFIXES = ("fill_", "stroke_")
_STYLE_TYPE_CATEGORY = {"TEXT": "typography", "EFFECT": "effect", "GRID": "other", "FILL": "color"}


def _infer_style_category(val) -> str | None:
    """Infer a token category from a globalVars style VALUE shape, for styleIds whose name carries
    no known prefix (e.g. Framelink 0.13.x named styles like 'link/md/regular', 'FocusRing').
    Value-shape driven so it is robust to naming/version changes. Returns None if not distillable.
    Anti-fabrication: the caller emits the OPAQUE styleId as name + the resolved value — never invents."""
    if isinstance(val, list):
        if any(isinstance(v, str) and ("#" in v or v.startswith("rgb")) for v in val):
            return "color"
        return None
    if isinstance(val, dict):
        if "fontFamily" in val or "fontSize" in val:
            return "typography"
        if "boxShadow" in val or "effects" in val or ("color" in val and ("radius" in val or "offset" in val)):
            return "effect"
        if any(k in val for k in ("gap", "padding", "mode", "sizing", "dimensions")):
            return "spacing"
    return None


def _distill_tokens_from_globalvars(gfd_yaml: str) -> tuple[list[TokenEntry], list[str]]:
    """Deterministically distil TokenEntry list from a get_figma_data YAML payload.
    Colors from fill_/stroke_ styleIds (resolved values), effects from effect_, typography from
    text_/style_, dimensions from numeric layout gaps/radii. Names are the OPAQUE styleId — NO
    invented semantic names (that was the LLM fabrication surface). Returns (tokens, styleIds_seen)."""
    tokens: list[TokenEntry] = []
    seen_ids: list[str] = []
    try:
        data = yaml.safe_load(gfd_yaml) or {}
    except Exception:
        return tokens, seen_ids
    styles = (((data.get("globalVars") or {}).get("styles")) or {})
    if not isinstance(styles, dict):
        return tokens, seen_ids
    for sid, val in styles.items():
        seen_ids.append(sid)
        if any(sid.startswith(p) for p in _COLOR_PREFIXES):
            vals = val if isinstance(val, list) else [val]
            for v in vals:
                if isinstance(v, str):
                    tokens.append(TokenEntry(name=sid, value=v, style_id=sid, category="color"))
        elif sid.startswith(("text_", "style_")):
            tokens.append(TokenEntry(name=sid, value=str(val)[:200], style_id=sid, category="typography"))
        elif sid.startswith("effect_"):
            tokens.append(TokenEntry(name=sid, value=str(val)[:200], style_id=sid, category="effect"))
        elif sid.startswith("layout_") and isinstance(val, dict):
            for dim_key in ("gap", "padding"):
                if dim_key in val and val[dim_key] not in (None, "", {}):
                    tokens.append(TokenEntry(name=f"{sid}/{dim_key}", value=str(val[dim_key]),
                                             style_id=sid, category="spacing"))
        else:
            # broaden (Framelink 0.13.x): named styles carry no known prefix (link/*, FocusRing, …).
            # Infer category from the value shape so typography/effect/color tokens are not silently
            # dropped. OPAQUE styleId name + resolved value — no fabrication.
            cat = _infer_style_category(val)
            if cat:
                tokens.append(TokenEntry(name=sid, value=str(val)[:200], style_id=sid, category=cat))
    return tokens, seen_ids


def _variants_for_set(gfd_yaml: str, set_node_id: str) -> tuple[list[ComponentVariant], list[str]]:
    """Parse metadata.components for the given componentSetId → ComponentVariant list.
    Deterministic parse of the 'Variant=.., Size=.., State=..' naming. Returns (variants, styleIds)."""
    variants: list[ComponentVariant] = []
    try:
        data = yaml.safe_load(gfd_yaml) or {}
    except Exception:
        return variants, []
    comps = ((data.get("metadata") or {}).get("components")) or {}
    for cid, c in (comps.items() if isinstance(comps, dict) else []):
        if not isinstance(c, dict) or c.get("componentSetId") != set_node_id:
            continue
        props = {}
        for part in str(c.get("name", "")).split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                props[k.strip().lower()] = v.strip()
        variants.append(ComponentVariant(variant=props.get("variant", ""), size=props.get("size", ""),
                                          state=props.get("state", ""), node_id=str(c.get("id", cid))))
    _, style_ids = _distill_tokens_from_globalvars(gfd_yaml)
    return variants, style_ids


# --------------------------------------------------------------------------- fail-loud thin-detection
def _classify_gfd_response(gfd_yaml: str) -> tuple[str | None, int | None, str]:
    """Classify a get_figma_data payload for fail-loud thin-detection (spec §3 'fail loud, not silent').

    Returns (error_class, http_status, detail). error_class is None when the response carries real
    enrichment substance. A component_set is FAILED only when Framelink returned NO usable structure:
      - rate_limit_exhausted : 429 marker survived _default_get_figma_data's backoff
      - malformed            : YAML could not be parsed
      - empty_response       : non-dict payload, OR an envelope whose metadata.components AND
                               globalVars.styles are BOTH empty (the 'Framelink walked nothing' signature)
    A set with EITHER components OR styles is treated as valid — this deliberately avoids false-failing a
    genuinely sparse set (e.g. one component, no local styles), per the spec's thin-but-valid caveat.
    error_class names refine Lane 2/3/5's generic `empty`/`malformed` for diagnostic precision."""
    if _is_rate_limited(gfd_yaml):
        return "rate_limit_exhausted", 429, "429 persisted after get_figma_data backoff"
    try:
        data = yaml.safe_load(gfd_yaml)
    except Exception as e:  # noqa: BLE001 — any parse failure is a failed set
        return "malformed", None, f"yaml parse failed: {e!r}"[:200]
    if not isinstance(data, dict):
        return "empty_response", None, f"non-dict payload ({type(data).__name__})"
    if _is_empty_envelope(gfd_yaml):
        return "empty_response", None, "metadata.components and globalVars.styles both empty"
    return None, None, ""


# --------------------------------------------------------------------------- default real callables
_RATE_LIMIT_MARKERS = ("Too Many Requests", "status 429")
_GFD_BACKOFF = (1.0, 3.0, 8.0)  # spec §10.3 — backoff on 429


def _is_rate_limited(text: str) -> bool:
    """Text-marker rate-limit signal (Framelink 0.9.x surfaces a Figma 429 as error text)."""
    return bool(text) and any(m in text for m in _RATE_LIMIT_MARKERS)


def _is_empty_envelope(text: str) -> bool:
    """A payload that parsed to a dict but carries NO substance — metadata.components AND
    globalVars.styles both empty. Framelink 0.13.x returns exactly this shape on a Figma 429 (the
    rate limit is NOT surfaced as error text), so it must be treated as retryable. For a component_set
    — which by definition holds ≥1 variant — an empty envelope is never legitimate, so retrying is safe."""
    try:
        data = yaml.safe_load(text)
    except Exception:  # noqa: BLE001 — unparseable is handled elsewhere, not "empty"
        return False
    if not isinstance(data, dict):
        return False
    meta = data.get("metadata")
    gv = data.get("globalVars")
    mc = meta.get("components") if isinstance(meta, dict) else None
    gs = gv.get("styles") if isinstance(gv, dict) else None
    has_components = isinstance(mc, dict) and len(mc) > 0
    has_styles = isinstance(gs, dict) and len(gs) > 0
    return not has_components and not has_styles


async def _default_get_figma_data(session, file_key: str, node_id: str, depth: int = 4) -> str:
    """Framelink get_figma_data via ClientSession (no LLM), with backoff-retry on rate limits.
    A Figma 429 is surfaced as error TEXT by Framelink 0.9.x and as an EMPTY ENVELOPE by 0.13.x —
    retry on BOTH signals until real data or the backoff is exhausted (then return the last payload,
    which _classify_gfd_response records loudly as rate_limit_exhausted / empty_response)."""
    for attempt in range(len(_GFD_BACKOFF) + 1):
        res = await session.call_tool("get_figma_data", {"fileKey": file_key, "nodeId": node_id, "depth": depth})
        blocks = getattr(res, "content", None) or []
        text = "".join(getattr(b, "text", "") for b in blocks if getattr(b, "type", None) == "text")
        retryable = _is_rate_limited(text) or _is_empty_envelope(text)
        if not retryable or attempt == len(_GFD_BACKOFF):
            return text
        await asyncio.sleep(_GFD_BACKOFF[attempt])
    return text


async def _default_download(session, file_key: str, nodes: list[dict], local_path: str = "runs/assets") -> str:
    res = await session.call_tool("download_figma_images",
                                  {"fileKey": file_key, "nodes": nodes, "localPath": local_path})
    blocks = getattr(res, "content", None) or []
    return "".join(getattr(b, "text", "") for b in blocks if getattr(b, "type", None) == "text")


# --------------------------------------------------------------------------- orchestrator
async def run_deterministic_extraction(
    file_key: str,
    *,
    session=None,
    lane_meta=None,
    lane_semantic=None,
    lane_binding=None,
    get_figma_data=None,
    download_images=None,
    do_assets: bool = True,
    do_token_catalog: bool = True,
    token_catalog_shared_doc=None,
    freshness_threshold_days=None,
) -> dict:
    """Deterministic Step-1 extraction. All collaborators injectable for tests; defaults wire the
    real Lane funcs + Framelink MCP session. Returns a FigmaExtractionResult-shaped dict with the
    §5 envelope nested under 'deterministic_extraction'. Never raises past the step boundary."""
    # Lazy real defaults (avoid import cycles / connecting a session in unit tests).
    if lane_meta is None:
        from agents.figma_extractor.cache_versioning import run_file_meta as lane_meta  # type: ignore
    if lane_semantic is None:
        from agents.figma_extractor.semantic_layer import run_semantic_layer as lane_semantic  # type: ignore
    if lane_binding is None:
        from agents.figma_extractor.binding_topology import run_binding_topology as lane_binding  # type: ignore
    gfd = get_figma_data or (lambda fk, nid: _default_get_figma_data(session, fk, nid))
    dl = download_images or (lambda fk, nodes: _default_download(session, fk, nodes))

    extracted_at = _now_iso()
    failure_reports: list[dict] = []

    # 1.1 meta
    meta = await lane_meta(file_key)
    if meta.get("failure_report"):
        failure_reports.append(meta["failure_report"])
    mp = meta.get("payload") or {}
    version, last_touched = mp.get("version"), mp.get("last_touched_at")

    # 1.2 semantic layer (the roster — entry point)
    sem = await lane_semantic(file_key)
    if sem.get("failure_reports"):
        failure_reports.extend(sem["failure_reports"])
    component_sets_raw = sem.get("component_sets") or []
    individual_components = sem.get("components") or []
    styles_raw = sem.get("styles") or []
    roster_ok = sem.get("status") in ("success", "partial") and bool(component_sets_raw)

    # 1.3 per-set get_figma_data (bounded concurrency), deterministic distill
    tokens: list[TokenEntry] = []
    components: list[ComponentEntry] = []
    rich_component_sets: list[dict] = []
    sets_failed: list[str] = []
    sem_sid = asyncio.Semaphore(_NODE_CONCURRENCY)

    def _fail_set(name, nid, error_class, http_status, message, retry_count=0) -> None:
        failure_reports.append({"endpoint": "get_figma_data", "http_status": http_status,
                                "error_class": error_class, "message": message[:400],
                                "attempted_at": _now_iso(), "retry_count": retry_count,
                                "node_id": nid, "component_set": name})
        sets_failed.append(name)

    async def _process_set(cs: dict) -> None:
        nid = cs.get("node_id")
        name = cs.get("name") or nid or "?"
        if not nid:
            _fail_set(cs.get("name", "?"), None, "client_error", None, "component_set missing node_id")
            return
        try:
            async with sem_sid:
                yaml_text = await gfd(file_key, nid)
        except Exception as e:
            _fail_set(name, nid, "server_error", None, f"{nid}: {e!r}")
            return
        # fail loud (spec §3): thin / empty / rate-limited responses become failed sets, never silent
        # success. Anti-fabrication: a failed set emits NOTHING (no empty shell backfilled downstream).
        err_class, http_status, detail = _classify_gfd_response(yaml_text)
        if err_class is not None:
            _fail_set(name, nid, err_class, http_status, f"{name} ({nid}): {detail}")
            return
        set_tokens, _ = _distill_tokens_from_globalvars(yaml_text)
        variants, style_ids = _variants_for_set(yaml_text, nid)
        tokens.extend(set_tokens)
        components.append(ComponentEntry(
            name=cs.get("name", ""), node_id=nid, variants=variants,
            designer_instructions=(cs.get("description") or None),
            tokens_consumed=sorted({t.style_id for t in set_tokens if t.style_id}),
        ))
        rich_component_sets.append({
            "node_id": nid, "name": cs.get("name"), "key": cs.get("key"),
            "description": cs.get("description"), "description_rt": cs.get("description_rt"),
            "containing_frame": cs.get("containing_frame"),
            "variant_count": len(variants), "style_ids": style_ids,
        })

    if roster_ok:
        await asyncio.gather(*[_process_set(cs) for cs in component_sets_raw])

    # Lane 2 named styles → additional (named) tokens
    for st in styles_raw:
        cat = _STYLE_TYPE_CATEGORY.get(str(st.get("style_type", "")).upper(), "other")
        tokens.append(TokenEntry(name=st.get("name", ""), value=str(st.get("style_type", "")),
                                 style_id=st.get("key"), category=cat))

    # dedup tokens by (name, value)
    _seen = set()
    deduped: list[TokenEntry] = []
    for t in tokens:
        k = (t.name, t.value)
        if k not in _seen:
            _seen.add(k)
            deduped.append(t)
    tokens = deduped

    # 1.4 bindings
    set_node_ids = [cs["node_id"] for cs in component_sets_raw if cs.get("node_id")]
    bindings_payload = {}
    if set_node_ids:
        btop = await lane_binding(file_key, set_node_ids)
        if btop.get("failure_report"):
            failure_reports.append(btop["failure_report"])
        bindings_payload = btop.get("bindings") or {}

    # 1.5 asset download for icon-classified sets (deterministic name rule; no LLM)
    assets: list[AssetEntry] = []
    if do_assets and roster_ok:
        icon_nodes = [{"nodeId": cs["node_id"], "fileName": f"{cs.get('name','icon')}.svg"}
                      for cs in component_sets_raw
                      if cs.get("node_id") and "icon" in str(cs.get("name", "")).lower()]
        if icon_nodes:
            try:
                await dl(file_key, icon_nodes[:10])
                for n in icon_nodes[:10]:
                    assets.append(AssetEntry(local_path=f"runs/assets/{n['fileName']}",
                                             node_id=n["nodeId"], format="svg"))
            except Exception as e:
                failure_reports.append({"endpoint": "download_figma_images", "http_status": None,
                                        "error_class": "server_error", "message": repr(e)[:300],
                                        "attempted_at": _now_iso(), "retry_count": 0})

    # 1.6 typo detection (component_set names + variant names + style names)
    named = [(cs.get("name", ""), cs.get("node_id", "")) for cs in component_sets_raw]
    named += [(c.name + " " + v.variant + " " + v.size + " " + v.state, v.node_id)
              for c in components for v in c.variants]
    named += [(st.get("name", ""), st.get("node_id", "")) for st in styles_raw]
    typos_rich = _detect_typos(named)
    typos_flat = [f"{t['original']} -> {t['suggestion']} (node {t['node_id']})" for t in typos_rich]

    # status (spec §3 fail-loud): ALL per-set enrichment failed → failure; ANY failed → partial
    if not roster_ok:
        status = "failure"
    elif component_sets_raw and len(sets_failed) >= len(component_sets_raw):
        status = "failure"  # every component_set's get_figma_data enrichment failed (Lane-2 skeleton only)
    elif failure_reports or sets_failed:
        status = "partial"
    else:
        status = "success"

    # attach bindings onto rich component_sets
    for rcs in rich_component_sets:
        rcs["bindings"] = bindings_payload.get(rcs["node_id"], {})

    # FER (contract) — pages_discovered synthesised from component_sets as a coverage anchor
    fer = FigmaExtractionResult(
        file_key=file_key,
        pages_discovered=[PageInfo(name=cs.get("name", "?"), node_id=cs.get("node_id", ""),
                                   page_type="component") for cs in component_sets_raw],
        tokens=tokens,
        components=components,
        assets=assets,
        extraction_runs=1,
        consensus_confidence=1.0,
        gaps_detected=[
            f"{fr.get('endpoint')} {fr.get('error_class')}"
            + (f" [{fr.get('component_set')}/{fr.get('node_id')}]" if fr.get("node_id") else "")
            + (f": {fr.get('message')}" if fr.get("message") else "")
            for fr in failure_reports
        ],
        typos_detected=typos_flat,
        enrichment_coverage=0.0,
    )

    section5 = {
        "status": status,
        "file_key": file_key,
        "extracted_at": extracted_at,
        "version": version,
        "last_touched_at": last_touched,
        "component_sets": rich_component_sets,
        "individual_components": individual_components,
        "styles": styles_raw,
        "assets": [a.model_dump() for a in assets],
        "typos_detected": typos_rich,
        "coverage_report": {
            "component_sets_expected": len(component_sets_raw),
            "component_sets_extracted": len(components),
            "component_sets_failed": sets_failed,
            "individual_components_total": len(individual_components),
            "styles_total": len(styles_raw),
            "assets_downloaded": len(assets),
            "typos_found": len(typos_rich),
        },
        "provenance": {
            "extraction_pipeline": PIPELINE,
            "lane_versions": _LANE_VERSIONS,
            "orchestrator": "workflow-layer-deterministic",
            "llm_involvement": "none",
        },
        "failure_reports": failure_reports,
    }

    content = fer.model_dump()
    content["deterministic_extraction"] = section5  # §5 envelope nested (FER coercion drops it)

    # 1.7 Lane 7 — Token Catalog (ADDITIVE, Phase A; spec:lane-7-token-catalog v0.1.2). Self-contained:
    # its own status/failure_reports live under content["token_catalog"]; does NOT flip the main status
    # (no downstream consumer yet). Divergence (Step 7.9) reads Lane-3's unique VariableID count.
    if do_token_catalog:
        lane3_vids = {b.get("variable_id") for node in bindings_payload.values()
                      for grp in ("property_bindings", "component_property_bindings")
                      for b in (node.get(grp) or {}).values() if b.get("variable_id")}
        try:
            tc = await run_token_catalog(
                file_key, session=session, file_last_modified=last_touched,
                lane3_variableid_count=(len(lane3_vids) if lane3_vids else None),
                freshness_threshold_days=freshness_threshold_days,
                shared_doc=token_catalog_shared_doc,
            )
            content["token_catalog"] = tc["token_catalog"]
            content["reconciliation_contract"] = tc["reconciliation_contract"]
        except Exception as e:  # noqa: BLE001 — Lane 7 is additive; never break the extraction
            content["token_catalog"] = {"status": "failure", "source": "tokens-studio-shared-plugin-data",
                                        "failure_reports": [{"error_class": "lane7_unexpected_error",
                                                             "message": repr(e)[:300]}]}
    return content
