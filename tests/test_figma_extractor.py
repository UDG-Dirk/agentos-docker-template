"""Figma Extractor test harness — smoke (ST) + contract (CT) + behavioral (BR).

Criteria: agents:figma-extractor:step2-test-criteria. Tests run against a real
run JSON ({result, _meta}); pass --output=<path>. Quality criteria (QC-*) and the
broadcast/sequential comparison (CMP-*) are human-reviewed in COMPARISON.md /
FIRST_RUN_REPORT.md, not asserted here.
"""
import json
import os
from pathlib import Path

import pytest

# ----------------------------------------------------------------------------- SMOKE
def test_ST1_ST2_agent_ran_and_mcp_connected(meta):
    """ST-1 agent started, ST-2 MCP connected — proven by >=1 captured MCP call."""
    assert meta.get("mcp_calls"), "no MCP calls captured — agent/MCP did not run"


def test_ST3_discovery_returns_pages(extraction_result):
    assert len(extraction_result.pages_discovered) >= 1


def test_ST4_at_least_one_node_extraction(meta):
    calls = meta.get("mcp_calls", [])
    figma_calls = [c for c in calls if "figma" in (c.get("tool") or "").lower()]
    if not figma_calls:
        pytest.skip("no figma tool calls captured in _meta (Team leader response hides "
                    "member calls) — verify extraction via the run .log")
    node_calls = [c for c in figma_calls if _has_node_id(c.get("args"))]
    assert node_calls, "no get_figma_data call with a node id (no actual extraction)"


def test_ST5_output_validates_against_schema(extraction_result):
    # fixture construction already validated via Pydantic; assert the type
    from models import FigmaExtractionResult  # type: ignore
    assert isinstance(extraction_result, FigmaExtractionResult)


def test_ST6_no_crash(meta):
    status = str(meta.get("run_status", "")).upper()
    assert "ERROR" not in status and "FAIL" not in status, f"run status indicates crash: {status}"


# --------------------------------------------------------------------------- CONTRACT
def test_CT1_pages_discovered_min(extraction_result):
    assert len(extraction_result.pages_discovered) >= 15, \
        f"only {len(extraction_result.pages_discovered)} pages (file has 17+)"


def test_CT2_pageinfo_fields_populated(extraction_result):
    for p in extraction_result.pages_discovered:
        assert p.name and p.node_id and p.page_type in ("foundation", "component", "other")


def test_CT3_tokens_nonempty(extraction_result):
    assert len(extraction_result.tokens) > 0


def test_CT4_tokenentry_fields_valid(extraction_result):
    valid_cat = {"color", "typography", "spacing", "sizing", "effect", "other"}
    for t in extraction_result.tokens:
        assert t.name and t.value and t.category in valid_cat


def test_CT5_components_min_six(extraction_result):
    assert len(extraction_result.components) >= 6, \
        f"only {len(extraction_result.components)} components (need 6 priority)"


def test_CT6_components_have_variants(extraction_result):
    for c in extraction_result.components:
        assert c.name and c.node_id and len(c.variants) >= 1, f"{c.name} has no variants"


def test_CT7_no_ephemeral_urls_in_asset_paths(extraction_result, asset_base):
    for a in extraction_result.assets:
        lp = a.local_path or ""
        assert not lp.startswith("http"), f"ephemeral URL leaked into local_path: {lp[:60]}"
        if lp and lp != "DOWNLOAD_FAILED":
            resolved = _resolve_asset(lp, asset_base)
            assert resolved.is_file(), (
                f"asset local_path set but file missing on disk: {lp!r} "
                f"(resolved to {resolved})"
            )


def test_CT8_extraction_runs_min(extraction_result):
    assert extraction_result.extraction_runs >= 1


def test_CT9_consensus_confidence_range(extraction_result):
    assert 0.0 <= extraction_result.consensus_confidence <= 1.0


def test_CT10_gaps_is_list(extraction_result):
    assert isinstance(extraction_result.gaps_detected, list)


def test_CT11_typos_is_list(extraction_result):
    assert isinstance(extraction_result.typos_detected, list)


# ------------------------------------------------------------------------- BEHAVIORAL
def test_BR1_discovery_first(meta):
    """First figma call must be file-level discovery: no node id, or the document
    root 0:0 (NOT a content node)."""
    calls = meta.get("mcp_calls", [])
    figma_calls = [c for c in calls if "figma" in (c.get("tool") or "").lower()]
    if not figma_calls:
        pytest.skip("no figma tool calls captured in _meta (Team leader response hides "
                    "member calls) — verify discovery-first via the run .log")
    first = figma_calls[0]
    nid = _node_id_value(first.get("args"))
    assert nid in (None, "", "0:0", "0-0"), \
        f"first figma call targeted node {nid!r} — discovery-first violated: {first.get('args')}"


def test_BR2_pat_not_leaked(run_payload):
    pat = os.environ.get("FIGMA_PAT") or os.environ.get("FIGMA_API_KEY")
    if not pat:
        pytest.skip("FIGMA_PAT not in env — cannot verify leak")
    blob = json.dumps(run_payload, default=str)
    assert pat not in blob, "FIGMA_PAT leaked into run output"


def test_BR3_assets_have_local_paths(extraction_result, asset_base):
    for a in extraction_result.assets:
        assert a.local_path, "asset missing local_path"
        if a.local_path != "DOWNLOAD_FAILED":
            resolved = _resolve_asset(a.local_path, asset_base)
            assert resolved.is_file(), (
                f"asset local_path set but file missing on disk: {a.local_path!r} "
                f"(resolved to {resolved})"
            )


def test_AS1_at_least_one_asset_file_on_disk(extraction_result, asset_base):
    candidates = [a for a in extraction_result.assets if a.local_path != "DOWNLOAD_FAILED"]
    if not candidates:
        pytest.skip("run produced no non-failed asset entries")
    resolved_paths = [_resolve_asset(a.local_path, asset_base) for a in candidates]
    assert any(p is not None and p.is_file() for p in resolved_paths), (
        "no asset file found on disk; attempted paths: "
        + ", ".join(str(p) for p in resolved_paths)
    )


def test_BR4_schema_compliance(result_dict):
    from models import FigmaExtractionResult  # type: ignore
    FigmaExtractionResult(**result_dict)  # raises on invalid


def test_BR5_token_usage_captured(meta):
    assert meta.get("metrics") is not None, "no token/usage metrics captured"


# ------------------------------------------------------------------------------ utils
def _resolve_asset(local_path, asset_base):
    """Resolve an AssetEntry.local_path to an absolute Path. Tries absolute,
    then relative to the agent dir, then relative to helix root."""
    if not local_path or local_path == "DOWNLOAD_FAILED":
        return None
    p = Path(local_path)
    if p.is_absolute():
        return p
    cands = [asset_base / local_path, asset_base.parents[1] / local_path]
    for c in cands:
        if c.exists():
            return c
    return cands[0]


def _node_id_value(args):
    """Return the nodeId targeted by a tool-call args payload, or None if file-level."""
    if not args:
        return None
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            return None
    if isinstance(args, dict):
        for k, v in args.items():
            kl = str(k).lower().replace("-", "").replace("_", "")
            if kl in ("nodeid", "nodeids", "ids", "id") and v:
                return str(v)
    return None


def _has_node_id(args) -> bool:
    """True if a tool-call args payload targets a specific node (nodeId/node-id/ids)."""
    if not args:
        return False
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            low = args.lower()
            return ("nodeid" in low) or ("node_id" in low) or ('"ids"' in low)
    if isinstance(args, dict):
        for k, v in args.items():
            kl = str(k).lower().replace("-", "").replace("_", "")
            if kl in ("nodeid", "nodeids", "ids", "id") and v:
                return True
    return False
