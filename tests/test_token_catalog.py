"""Tests for Lane 7 — Token Catalog (spec:lane-7-token-catalog-v0-1-draft v0.1.2 §8).

Deterministic + offline: full-pipeline contract tests run against REAL compressed fixtures
(tests/fixtures/tokens_studio_*.{txt,json}) generated from the live 720-token blob — no network,
no PAT. A single live smoke (skipif no PAT) hits the real Figma file.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import pathlib

import pytest

import agents.figma_extractor.token_catalog as tc

FK = "8qPSyetzviLR6eF6bkpL44"
_FIX = pathlib.Path(__file__).parent / "fixtures"
_REAL_BLOB = (_FIX / "tokens_studio_values_blob.txt").read_text(encoding="utf-8")
_BLOBS = json.loads((_FIX / "tokens_studio_test_blobs.json").read_text())


def _doc(values, *, version="2.11.5", updated="2026-04-24T07:40:16.254Z", themes=None, extra=None):
    tokens = {"version": version, "values": values, "isCompressed": True, "updatedAt": updated,
              "checkForChanges": True, "themes": themes if themes is not None else [], "fileKey": "QqBw_inner"}
    if extra:
        tokens.update(extra)
    return {"document": {"sharedPluginData": {"tokens": tokens}}}


def _run(values, **kw):
    doc = kw.pop("doc", None) or _doc(values, **{k: kw.pop(k) for k in
                                                  ("version", "updated", "themes", "extra") if k in kw})
    return asyncio.run(tc.run_token_catalog(FK, shared_doc=doc, **kw))["token_catalog"]


# ---- LZString pure-Python decode (byte-identical to node reference) --------
def test_decompress_real_blob_matches_720():
    out = tc.decompress_from_utf16(_REAL_BLOB)
    data = json.loads(out)
    n = sum(len(v) for v in data.values() if isinstance(v, list))
    assert n == 720 and set(data) >= {"primitive/Core", "semantic-color/light-mode"}


def test_decompress_empty_returns_empty():
    assert tc.decompress_from_utf16("") == ""
    assert tc.decompress_from_utf16(None) is None


# ---- full pipeline on the REAL blob (offline, deterministic) ---------------
def test_full_pipeline_real_blob_720_tokens():
    r = _run(_REAL_BLOB, file_last_modified="2026-07-27T12:30:55Z", lane3_variableid_count=201)
    assert r["token_count_total"] == 720
    assert r["tokens_studio_version_check"] == "exact_match"
    assert r["provenance"]["llm_involvement"] == "none"
    assert {s["name"] for s in r["token_sets"]} >= {"primitive/Core", "semantic-color/light-mode",
                                                    "semantic-dimension/Desktop"}
    assert set(r["modes_detected"]) == {"Tablet", "Phone", "Desktop", "Wide"}
    assert r["freshness_status"] == "stale"  # 2026-04-24 blob vs 2026-07-27 file
    assert r["divergence_status"] == "no_signal"  # 201 referenced < 720 catalog
    assert r["unrecognized_schema_fields"] == []
    # every aliased token resolved
    aliased = [t for t in r["tokens"] if t["is_alias"]]
    assert aliased and all(t["value_resolved"] is not None for t in aliased if not t.get("resolution_error"))


def test_cross_run_consistency_modulo_timestamp():
    def scrub(x):
        y = copy.deepcopy(x)
        y["extracted_at"] = "T"
        for fr in y.get("failure_reports", []):  # attempted_at is a per-run timestamp
            fr["attempted_at"] = "T"
        return y
    assert scrub(_run(_REAL_BLOB, file_last_modified="2026-07-27T12:30:55Z", lane3_variableid_count=201)) \
        == scrub(_run(_REAL_BLOB, file_last_modified="2026-07-27T12:30:55Z", lane3_variableid_count=201))


def test_anti_fabrication_every_token_traces_to_source():
    r = _run(_REAL_BLOB, file_last_modified="2026-07-27T12:30:55Z")
    src = json.loads(tc.decompress_from_utf16(_REAL_BLOB))
    src_names = {t.get("name") for arr in src.values() if isinstance(arr, list) for t in arr}
    for t in r["tokens"]:
        assert t["name"] in src_names, f"fabricated token {t['name']}"


# ---- contract: parse + extensions + emit-both aliases ----------------------
def test_simple_dtcg_parse_and_alias_emit_both():
    r = _run(_BLOBS["simple_dtcg"])
    assert r["token_count_total"] == 3
    bg = next(t for t in r["tokens"] if t["name"] == "color.bg.default")
    assert bg["is_alias"] and bg["alias_target"] == "color.base.white"
    assert bg["value_raw"] == "{color.base.white}" and bg["value_resolved"] == "#ffffff"  # emit BOTH
    assert bg["extensions"]["scopes"] == ["FRAME_FILL"] and bg["extensions"]["hiddenFromPublishing"] is False
    assert r["status"] == "success" and r["unrecognized_schema_fields"] == []


def test_alias_cycle_detected():
    r = _run(_BLOBS["with_cycle"])
    a = next(t for t in r["tokens"] if t["name"] == "a")
    assert a["value_resolved"] is None and a.get("resolution_error") == "alias_cycle_detected"
    assert r["status"] == "partial"


def test_alias_unresolvable():
    r = _run(_BLOBS["unresolvable"])
    x = next(t for t in r["tokens"] if t["name"] == "x")
    assert x["value_resolved"] is None and x["resolution_error"] == "alias_unresolvable"
    assert r["status"] == "partial"


# ---- unrecognized-field discipline (§4.4) ----------------------------------
def test_unrecognized_token_field_and_set():
    r = _run(_BLOBS["unknown_fields"])
    fields = {u["field_name"] for u in r["unrecognized_schema_fields"]}
    assert "weirdField" in fields
    assert r["status"] == "partial"
    assert any(fr["error_class"] == "unrecognized_schema_field" for fr in r["failure_reports"])


def test_unrecognized_extension_namespace():
    r = _run(_BLOBS["unknown_ext"])
    assert any(u.get("error_class") == "unrecognized_extension_namespace"
               and u["field_name"] == "com.acme.custom" for u in r["unrecognized_schema_fields"])


# ---- modes (set-per-breakpoint) --------------------------------------------
def test_modes_detected_from_multi_variant_category():
    r = _run(_BLOBS["modes"])
    assert set(r["modes_detected"]) == {"Phone", "Desktop"}
    phone = next(t for t in r["tokens"] if t["set"] == "semantic-dimension/Phone")
    assert phone["mode"] == "Phone" and phone["value_resolved"] == "360"


# ---- version-check discipline (§4.2) ---------------------------------------
def test_version_check_labels():
    assert tc._version_check("2.11.5") == ("exact_match", None)
    assert tc._version_check("2.11.9")[0] == "patch_bump"
    assert tc._version_check("2.12.0")[0] == "minor_bump"
    assert tc._version_check("3.0.0") == ("major_bump_escalated", "tokens_studio_major_version_incompatible")


def test_major_version_bump_fails_before_parse():
    r = _run(_BLOBS["simple_dtcg"], version="3.0.0")
    assert r["status"] == "failure"
    assert any(fr["error_class"] == "tokens_studio_major_version_incompatible" for fr in r["failure_reports"])
    assert r["token_count_total"] == 0  # did NOT attempt parse


def test_minor_bump_is_partial_but_parses():
    r = _run(_BLOBS["simple_dtcg"], version="2.12.0")
    assert r["status"] == "partial" and r["token_count_total"] == 3


# ---- LZString / blob pathological input (§7) -------------------------------
def test_missing_shared_plugin_data():
    r = asyncio.run(tc.run_token_catalog(FK, shared_doc={"document": {"sharedPluginData": {}}}))["token_catalog"]
    assert r["status"] == "failure"
    assert any(fr["error_class"] == "missing_shared_plugin_data" for fr in r["failure_reports"])


def test_lzstring_empty_input():
    r = _run("")
    assert r["status"] == "failure" and any(f["error_class"] == "lzstring_empty_input" for f in r["failure_reports"])


def test_lzstring_invalid_encoding():
    r = _run("\x01badcontrolchars")
    assert any(f["error_class"] == "lzstring_invalid_encoding" for f in r["failure_reports"])


def test_lzstring_corrupt_data_non_json():
    r = _run(_BLOBS["not_json"])  # decompresses fine, yields 'this is not json at all'
    assert r["status"] == "failure"
    assert any(f["error_class"] == "lzstring_corrupt_data" for f in r["failure_reports"])


def test_lzstring_truncated_blob():
    r = _run(_REAL_BLOB[:40])  # truncated mid-stream
    assert r["status"] == "failure"
    assert any(f["error_class"] in ("lzstring_truncated_input", "lzstring_corrupt_data")
               for f in r["failure_reports"])


# ---- freshness (§4.6) ------------------------------------------------------
def test_freshness_stale_current_disabled():
    stale = _run(_BLOBS["simple_dtcg"], updated="2026-01-01T00:00:00Z", file_last_modified="2026-07-27T00:00:00Z")
    assert stale["freshness_status"] == "stale" and stale["status"] == "partial"
    fresh = _run(_BLOBS["simple_dtcg"], updated="2026-07-26T00:00:00Z", file_last_modified="2026-07-27T00:00:00Z")
    assert fresh["freshness_status"] == "current"
    disabled = _run(_BLOBS["simple_dtcg"], updated="2020-01-01T00:00:00Z",
                    file_last_modified="2026-07-27T00:00:00Z", freshness_threshold_days=0)
    assert disabled["freshness_status"] == "check_disabled"


# ---- divergence detection (Step 7.9) ---------------------------------------
def test_divergence_suspect_when_referenced_exceeds_catalog():
    r = _run(_BLOBS["simple_dtcg"], lane3_variableid_count=100)  # 100 refs vs 3 catalog -> +3233%
    assert r["divergence_status"] == "catalog_divergence_suspect" and r["status"] == "partial"


def test_divergence_no_signal_and_unable():
    ok = _run(_BLOBS["simple_dtcg"], lane3_variableid_count=2)  # 2 < 3 -> no signal
    assert ok["divergence_status"] == "no_signal"
    unk = _run(_BLOBS["simple_dtcg"])  # no lane3 count provided
    assert unk["divergence_status"] == "unable_to_check"


def test_reconciliation_contract_emitted():
    doc = _doc(_BLOBS["simple_dtcg"])
    full = asyncio.run(tc.run_token_catalog(FK, shared_doc=doc))
    assert full["reconciliation_contract"]["policy_name"] == "authoritative_catalog_with_usage_evidence_fallback"
    assert len(full["reconciliation_contract"]["consumer_rules"]) == 4


# ---- live smoke (real Figma; needs FIGMA_PAT) ------------------------------
_HAS_PAT = bool(os.environ.get("FIGMA_PAT") or os.environ.get("FIGMA_API_KEY"))


@pytest.mark.skipif(not _HAS_PAT, reason="FIGMA_PAT not in env — live smoke skipped")
def test_live_smoke_token_catalog():
    r = asyncio.run(tc.run_token_catalog(FK, file_last_modified="2026-07-27T12:30:55Z",
                                         lane3_variableid_count=201))["token_catalog"]
    assert r["token_count_total"] >= 700  # 720 unless the blob changed since Phase 1
    assert r["provenance"]["llm_involvement"] == "none"
    assert set(r["modes_detected"]) >= {"Tablet", "Phone", "Desktop", "Wide"}
    assert r["unrecognized_schema_fields"] == []
