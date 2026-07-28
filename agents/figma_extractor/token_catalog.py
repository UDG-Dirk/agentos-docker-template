"""Lane 7 — Token Catalog (Tokens Studio) extraction (spec:lane-7-token-catalog-v0-1-draft v0.1.2).

Deterministic, zero-LLM. Reads the authoritative design-token catalog from the Figma file's
``document.sharedPluginData.tokens`` (a Tokens Studio export, DTCG-claimed / legacy-key actual),
LZString-UTF16-decompresses it in pure Python (no deps, no subprocess), parses the token sets,
resolves aliases (emitting BOTH raw + resolved), normalises modes (set-per-breakpoint,
dark-mode-ready), compares freshness, and signals catalog-vs-Variables divergence.

Additive to the deterministic extractor (Phase A per §9): emits ``token_catalog`` +
``reconciliation_contract`` alongside existing output; no downstream consumer yet.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime

import httpx

from agents.figma_extractor.http_errors import FileExportDisabledError, raise_for_figma_status

# --------------------------------------------------------------------------- constants
LANE = "lane-7-token-catalog"
LANE_VERSION = "lane-7-token-catalog-v0-1-2"
_TESTED_TS_VERSION = "2.11.5"  # Tokens Studio version the parser was verified against (Phase 1)
_FIGMA_API_BASE = "https://api.figma.com/v1"
_TIMEOUT_S = 30.0
_DEFAULT_FRESHNESS_DAYS = 7  # spec §4.6 Dirk ratification
_DIVERGENCE_DELTA_PCT = 20.0  # spec §4.9 — >20% more referenced than catalog → suspect
_MAX_ALIAS_HOPS = 10  # spec §10.8 bounded multi-hop + cycle detection
_LZ_MAX_ITERS = 1_000_000  # spec §4.3 pathological-hang protection

# recognised-field allow-lists (spec §4.4 v0.1.2) — anything else surfaces as unrecognized.
# NOTE(v0.1.2 deviation, flagged for v0.1.3): the spec's illustrative allow-list omitted several
# fields the REAL Tokens Studio 2.11.5 blob actually carries (tokenFormat, usedTokenSet, themes_meta,
# selectedExportThemes, fileKey, values_meta, collapsedTokenSets). Enumerated from the live blob
# (Phase 1) so clean input yields [] rather than a wall of false-positive warnings (§ escalation trigger).
_TOP_LEVEL_FIELDS = {"version", "tokenFormat", "values", "usedTokenSet", "updatedAt", "themes_meta",
                     "isCompressed", "activeTheme", "selectedExportThemes", "fileKey", "themes",
                     "values_meta", "collapsedTokenSets", "checkForChanges"}
_TOKEN_FIELDS = {"name", "value", "type", "$extensions", "description", "$description"}
_EXT_NAMESPACES = {"com.figma.hiddenFromPublishing", "com.figma.scopes"}

RECONCILIATION_CONTRACT = {
    "policy_name": "authoritative_catalog_with_usage_evidence_fallback",
    "consumer_rules": [
        "RULE 1: When a token appears in Lane 7 catalog, PREFER Lane 7 values (name, category, resolved value)",
        "RULE 2: When a token is referenced in components (Lane 1) but NOT in Lane 7 catalog, treat as an "
        "unnamed usage-derived token from Lane 1; log as `catalog_gap` for observability",
        "RULE 3: When Lane 1 value differs from Lane 7 value_resolved for the same token, PREFER Lane 7 "
        "authoritative; log divergence as `value_mismatch` for observability",
        "RULE 4: Downstream stations MUST NOT silently pick one source without emitting an audit record",
    ],
    "rationale": "Lane 7 is authoritative (designer-authored named tokens); Lane 1 is usage-evidence "
                 "(Framelink globalVars keys, no clean join key). Extractor does not reconcile; downstream "
                 "stations must, per this contract, to prevent divergent cross-station semantics.",
}


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _figma_pat() -> str:
    return os.environ.get("FIGMA_PAT") or os.environ.get("FIGMA_API_KEY") or ""


def _fail(error_class: str, message: str, http_status=None) -> dict:
    return {"endpoint": "sharedPluginData.tokens", "http_status": http_status,
            "error_class": error_class, "message": message[:400],
            "attempted_at": _now_iso(), "retry_count": 0}


# --------------------------------------------------------------------------- LZString-UTF16 (pure Python)
def _lz_decompress(length, reset_value, get_next):
    """Reference LZString `_decompress`, pure-Python (byte-identical to lz-string npm — Phase 1.5).
    Bounded to _LZ_MAX_ITERS to defuse pathological input (spec §4.3)."""
    dic = [0, 1, 2]
    enlarge = 4
    dsize = 4
    numbits = 3
    result = []
    val = get_next(0)
    pos = reset_value
    idx = 1

    def readbits(maxpow):
        nonlocal val, pos, idx
        bits = 0
        power = 1
        while power != maxpow:
            resb = val & pos
            pos >>= 1
            if pos == 0:
                pos = reset_value
                val = get_next(idx)
                idx += 1
            bits |= (1 if resb > 0 else 0) * power
            power <<= 1
        return bits

    c = readbits(4)
    if c == 0:
        c = chr(readbits(256))
    elif c == 1:
        c = chr(readbits(65536))
    elif c == 2:
        return ""
    dic.append(c)
    w = c
    result.append(c)
    iters = 0
    while True:
        iters += 1
        if iters > _LZ_MAX_ITERS:
            raise _LZIterationBound
        if idx > length:
            return ""
        c = readbits(2 ** numbits)
        if c == 0:
            dic.append(chr(readbits(256)))
            c = dsize
            dsize += 1
            enlarge -= 1
        elif c == 1:
            dic.append(chr(readbits(65536)))
            c = dsize
            dsize += 1
            enlarge -= 1
        elif c == 2:
            return "".join(result)
        if enlarge == 0:
            enlarge = 2 ** numbits
            numbits += 1
        if c < len(dic):
            entry = dic[c]
        elif c == dsize:
            entry = w + w[0]
        else:
            raise _LZCorrupt
        result.append(entry)
        dic.append(w + entry[0])
        dsize += 1
        enlarge -= 1
        w = entry
        if enlarge == 0:
            enlarge = 2 ** numbits
            numbits += 1


class _LZIterationBound(Exception):
    pass


class _LZCorrupt(Exception):
    pass


def decompress_from_utf16(compressed: str) -> str | None:
    """LZString.decompressFromUTF16, pure-Python. Returns decompressed str, or None on invalid input."""
    if compressed is None:
        return None
    if compressed == "":
        return ""
    return _lz_decompress(len(compressed), 16384, lambda i: ord(compressed[i]) - 32)


# --------------------------------------------------------------------------- version check (§4.2)
def _semver(v: str):
    parts = (v or "").split(".")
    out = []
    for p in parts[:3]:
        try:
            out.append(int(p))
        except ValueError:
            out.append(0)
    while len(out) < 3:
        out.append(0)
    return tuple(out)


def _version_check(actual: str) -> tuple[str, str | None]:
    """Return (check_label, error_class|None). Major bump escalates to failure."""
    a = _semver(actual)
    t = _semver(_TESTED_TS_VERSION)
    if a == t:
        return "exact_match", None
    if a[0] != t[0]:
        return "major_bump_escalated", "tokens_studio_major_version_incompatible"
    if a[1] != t[1]:
        return "minor_bump", None  # partial + informational, still parse
    return "patch_bump", None


# --------------------------------------------------------------------------- modes (§4.7)
def _split_set(name: str) -> tuple[str, str | None]:
    if "/" in name:
        cat, variant = name.rsplit("/", 1)
        return cat, variant
    return name, None


def _mode_scoped_categories(set_names) -> set[str]:
    """A category is mode-scoped when >=2 sets share it (dark-mode-ready: no hard-coded list)."""
    counts: dict[str, int] = {}
    for n in set_names:
        cat, variant = _split_set(n)
        if variant is not None:
            counts[cat] = counts.get(cat, 0) + 1
    return {cat for cat, c in counts.items() if c >= 2}


# --------------------------------------------------------------------------- DTCG parse (§4.4) + aliases (§4.5)
def _is_alias(value) -> bool:
    return isinstance(value, str) and value.strip().startswith("{") and value.strip().endswith("}")


def _alias_target(value: str) -> str:
    return value.strip()[1:-1]


def _resolution_order(set_names):
    """Deterministic set iteration for alias lookup: primitive/Core first, then the rest alphabetical."""
    rest = sorted(n for n in set_names if n != "primitive/Core")
    return (["primitive/Core"] if "primitive/Core" in set_names else []) + rest


def _parse_and_collect(sets_obj: dict):
    """Parse the decompressed DTCG object. Returns (tokens, token_sets, unrecognized, name_index).
    tokens: list of dicts (pre-resolution). name_index: {name: raw_value} for alias resolution
    (deterministic set order — first occurrence wins)."""
    tokens = []
    token_sets = []
    unrecognized = []
    name_index: dict[str, object] = {}
    set_names = list(sets_obj.keys())
    mode_cats = _mode_scoped_categories(set_names)

    for setname in _resolution_order(set_names):
        arr = sets_obj.get(setname)
        if not isinstance(arr, list):
            unrecognized.append({"location": f"set.{setname}", "field_name": "<non-list set>",
                                 "detail": type(arr).__name__})
            token_sets.append({"name": setname, "token_count": 0})
            continue
        cat, variant = _split_set(setname)
        mode = variant if cat in mode_cats else None
        token_sets.append({"name": setname, "token_count": len(arr)})
        for t in arr:
            if not isinstance(t, dict):
                unrecognized.append({"location": f"set.{setname}", "field_name": "<non-dict token>"})
                continue
            name = t.get("name")
            value = t.get("value", t.get("$value"))
            ttype = t.get("type", t.get("$type"))
            # unrecognized per-token fields
            for fk in t:
                if fk not in _TOKEN_FIELDS:
                    unrecognized.append({"location": f"token.{name}", "field_name": fk})
            exts_raw = t.get("$extensions") or {}
            for ns in exts_raw:
                if ns not in _EXT_NAMESPACES:
                    unrecognized.append({"location": f"token.{name}.$extensions", "field_name": ns,
                                         "error_class": "unrecognized_extension_namespace"})
            extensions = {
                "hiddenFromPublishing": exts_raw.get("com.figma.hiddenFromPublishing"),
                "scopes": exts_raw.get("com.figma.scopes"),
            }
            tok = {
                "name": name, "set": setname, "type": ttype,
                "value_raw": value, "value_resolved": None,
                "is_alias": _is_alias(value),
                "alias_target": _alias_target(value) if _is_alias(value) else None,
                "description": t.get("description", t.get("$description")),
                "mode": mode,
                "extensions": extensions,
            }
            tokens.append(tok)
            # name index: first occurrence in resolution order wins (primitive precedence)
            if name is not None and name not in name_index:
                name_index[name] = value
    return tokens, token_sets, unrecognized, name_index


def _resolve(name_index: dict, value):
    """Resolve an alias chain to a terminal value. Returns (resolved, error|None). Bounded + cycle-safe."""
    seen = set()
    cur = value
    hops = 0
    while _is_alias(cur):
        if hops >= _MAX_ALIAS_HOPS:
            return None, "alias_hop_limit_exceeded"
        target = _alias_target(cur)
        if target in seen:
            return None, "alias_cycle_detected"
        seen.add(target)
        if target not in name_index:
            return None, "alias_unresolvable"
        cur = name_index[target]
        hops += 1
    return cur, None


# --------------------------------------------------------------------------- freshness (§4.6)
def _parse_dt(s):
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _freshness(blob_updated_at, file_last_modified, threshold_days):
    """Return (status, delta_days|None). status in current|stale|unknown|check_disabled."""
    if threshold_days is not None and threshold_days <= 0:
        return "check_disabled", None
    bu = _parse_dt(blob_updated_at)
    fm = _parse_dt(file_last_modified)
    if bu is None or fm is None:
        return "unknown", None
    delta = (fm - bu).total_seconds() / 86400.0
    thr = _DEFAULT_FRESHNESS_DAYS if threshold_days is None else threshold_days
    return ("stale" if delta > thr else "current"), round(delta, 1)


# --------------------------------------------------------------------------- default fetch (§4.1)
async def _default_fetch_shared(file_key: str, client: httpx.AsyncClient | None = None) -> dict:
    own = client is None
    if own:
        client = httpx.AsyncClient(headers={"X-Figma-Token": _figma_pat()}, timeout=_TIMEOUT_S)
    try:
        resp = await client.get(f"{_FIGMA_API_BASE}/files/{file_key}",
                                params={"depth": 1, "plugin_data": "shared"})
        raise_for_figma_status(resp)  # export-lock 403 -> FileExportDisabledError (distinct)
        return resp.json()
    finally:
        if own:
            await client.aclose()


# --------------------------------------------------------------------------- orchestrator (Steps 7.1–7.9)
async def run_token_catalog(file_key: str, *, session=None, client: httpx.AsyncClient | None = None,
                            fetch_shared=None, file_last_modified: str | None = None,
                            lane3_variableid_count: int | None = None,
                            freshness_threshold_days: int | None = None,
                            shared_doc: dict | None = None) -> dict:
    """Run Lane 7. Returns {token_catalog, reconciliation_contract}. Never raises on data issues —
    surfaces everything as failure_reports/gaps inside token_catalog (spec §3 fail-loud).

    Injection points for tests: ``shared_doc`` (skip fetch), ``fetch_shared`` (custom fetch),
    ``lane3_variableid_count`` (Step 7.9), ``file_last_modified`` (Lane 5 timestamp)."""
    failure_reports: list[dict] = []
    gaps: list[str] = []
    status = "success"

    def cat(**extra) -> dict:
        base = {
            "source": "tokens-studio-shared-plugin-data",
            "provenance": {"source": "tokens-studio-shared-plugin-data",
                           "decompression": "pure-python-lzstring-utf16",
                           "orchestrator": "workflow-layer-deterministic",
                           "lane_version": LANE_VERSION, "llm_involvement": "none"},
            "extracted_at": _now_iso(),
            "status": status,
            "failure_reports": failure_reports,
            "gaps": gaps,
        }
        base.update(extra)
        return {"token_catalog": base, "reconciliation_contract": RECONCILIATION_CONTRACT}

    # Step 7.1 — fetch
    try:
        if shared_doc is None:
            fetcher = fetch_shared or _default_fetch_shared
            shared_doc = await fetcher(file_key, client) if fetch_shared is None else await fetcher(file_key)
    except FileExportDisabledError as e:  # content-protection lock — distinct, actionable
        failure_reports.append(_fail("file_export_disabled", f"{file_key}: {e.figma_message}"))
        status = "failure"
        return cat()
    except Exception as e:  # noqa: BLE001 — fetch failure is a lane failure, surfaced not raised
        failure_reports.append(_fail("shared_fetch_error", f"{file_key}: {e!r}"))
        status = "failure"
        return cat()

    # Step 7.2 — locate blob + version-check
    doc = (shared_doc or {}).get("document") or {}
    blob = ((doc.get("sharedPluginData") or {}).get("tokens")) or {}
    if not blob or "values" not in blob:
        failure_reports.append(_fail("missing_shared_plugin_data",
                                     "document.sharedPluginData.tokens absent or has no 'values'"))
        status = "failure"
        return cat(token_count_total=0, tokens=[], token_sets=[])
    ts_version = str(blob.get("version", ""))
    vcheck, verr = _version_check(ts_version)
    if verr:
        failure_reports.append(_fail(verr, f"Tokens Studio {ts_version} vs tested {_TESTED_TS_VERSION}"))
        status = "failure"
        return cat(tokens_studio_version=ts_version, tokens_studio_version_check=vcheck,
                   token_count_total=0, tokens=[], token_sets=[])
    if vcheck == "minor_bump":
        status = "partial"
        gaps.append(f"tokens_studio minor version bump {ts_version} (tested {_TESTED_TS_VERSION}) — "
                    "parser not re-verified")

    # unrecognized top-level fields
    unrecognized = [{"location": "blob.root", "field_name": k} for k in blob if k not in _TOP_LEVEL_FIELDS]

    # Step 7.3 — decompress
    raw = blob.get("values", "")
    if raw == "":
        failure_reports.append(_fail("lzstring_empty_input", "empty compressed 'values'"))
        status = "failure"
        return cat(tokens_studio_version=ts_version, tokens_studio_version_check=vcheck,
                   token_count_total=0, tokens=[], token_sets=[])
    try:
        if not all(ord(ch) >= 32 for ch in raw[:64]):  # cheap non-UTF16-safe screen
            failure_reports.append(_fail("lzstring_invalid_encoding", "non-UTF16-safe chars in blob"))
            status = "failure"
            return cat(tokens_studio_version=ts_version, token_count_total=0, tokens=[], token_sets=[])
        decompressed = decompress_from_utf16(raw)
    except _LZIterationBound:
        failure_reports.append(_fail("lzstring_iteration_bound_exceeded", "decompression exceeded iter cap"))
        status = "failure"
        return cat(tokens_studio_version=ts_version, token_count_total=0, tokens=[], token_sets=[])
    except Exception as e:  # noqa: BLE001
        failure_reports.append(_fail("lzstring_truncated_input", f"decompress failed: {e!r}"))
        status = "failure"
        return cat(tokens_studio_version=ts_version, token_count_total=0, tokens=[], token_sets=[])
    if not decompressed:
        failure_reports.append(_fail("lzstring_truncated_input", "decompression returned empty"))
        status = "failure"
        return cat(tokens_studio_version=ts_version, token_count_total=0, tokens=[], token_sets=[])

    # Step 7.4 — parse DTCG
    import json  # local import: only needed on the success path
    try:
        sets_obj = json.loads(decompressed)
    except Exception as e:  # noqa: BLE001
        failure_reports.append(_fail("lzstring_corrupt_data", f"decompressed non-JSON: {e!r}"))
        status = "failure"
        return cat(tokens_studio_version=ts_version, token_count_total=0, tokens=[], token_sets=[])
    if not isinstance(sets_obj, dict):
        failure_reports.append(_fail("dtcg_parse_error", f"top-level not an object ({type(sets_obj).__name__})"))
        status = "failure"
        return cat(tokens_studio_version=ts_version, token_count_total=0, tokens=[], token_sets=[])

    tokens, token_sets, parse_unrecognized, name_index = _parse_and_collect(sets_obj)
    unrecognized += parse_unrecognized
    if unrecognized:
        status = "partial" if status != "failure" else status
        failure_reports.append(_fail("unrecognized_schema_field",
                                     f"{len(unrecognized)} unrecognized field(s); see unrecognized_schema_fields"))

    # Step 7.5 — alias resolution
    alias_errors = 0
    for tok in tokens:
        if tok["is_alias"]:
            resolved, err = _resolve(name_index, tok["value_raw"])
            tok["value_resolved"] = resolved
            if err:
                tok["resolution_error"] = err
                alias_errors += 1
        else:
            tok["value_resolved"] = tok["value_raw"]
    if alias_errors:
        status = "partial" if status != "failure" else status
        gaps.append(f"{alias_errors} alias(es) unresolved")

    # Step 7.6 — freshness
    threshold = freshness_threshold_days
    if threshold is None:
        env = os.environ.get("HELIX_LANE7_FRESHNESS_THRESHOLD_DAYS")
        if env not in (None, ""):
            try:
                threshold = int(env)
            except ValueError:
                threshold = None
    fresh_status, delta_days = _freshness(blob.get("updatedAt"), file_last_modified, threshold)
    if fresh_status == "stale":
        status = "partial" if status != "failure" else status
        gaps.append(f"freshness_suspect: blob updatedAt older than file lastModified by {delta_days}d")
        failure_reports.append(_fail("freshness_suspect", f"blob-vs-file delta {delta_days}d > threshold"))

    # Step 7.7 — modes
    modes = sorted({tok["mode"] for tok in tokens if tok["mode"]})

    # Step 7.9 — divergence (signal-only)
    catalog_count = len(tokens)
    if lane3_variableid_count is None:
        divergence_status = "unable_to_check"
        divergence_delta = None
    else:
        divergence_delta = (round((lane3_variableid_count - catalog_count) / catalog_count * 100, 1)
                            if catalog_count else None)
        if divergence_delta is not None and divergence_delta > _DIVERGENCE_DELTA_PCT:
            divergence_status = "catalog_divergence_suspect"
            status = "partial" if status != "failure" else status
            gaps.append(f"catalog_divergence_suspect: {lane3_variableid_count} referenced vs "
                        f"{catalog_count} in catalog (+{divergence_delta}%)")
            failure_reports.append(_fail("catalog_divergence_suspect",
                                         f"referenced={lane3_variableid_count} catalog={catalog_count} "
                                         f"delta={divergence_delta}%"))
        else:
            divergence_status = "no_signal"

    # Step 7.8 — emit
    return cat(
        tokens_studio_version=ts_version,
        tokens_studio_version_check=vcheck,
        token_format_claimed=str(blob.get("tokenFormat", "dtcg")),
        actual_key_convention="legacy",
        blob_updated_at=blob.get("updatedAt"),
        file_last_modified=file_last_modified,
        blob_inner_file_key=blob.get("fileKey"),
        freshness_status=fresh_status,
        freshness_threshold_days_applied=(_DEFAULT_FRESHNESS_DAYS if threshold is None else threshold),
        freshness_delta_days=delta_days,
        divergence_status=divergence_status,
        divergence_delta_pct=divergence_delta,
        unique_variableids_referenced_count=lane3_variableid_count,
        token_count_total=catalog_count,
        token_sets=token_sets,
        modes_detected=modes,
        themes=blob.get("themes", []) if isinstance(blob.get("themes"), list) else [],
        tokens=tokens,
        unrecognized_schema_fields=unrecognized,
    )
