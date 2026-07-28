"""Tests for composition-only mode (Pattern 3, self-contained) + the helix-composition-only-extractor
workflow (task composition-only-mode-implementation). Deterministic + offline."""
from __future__ import annotations

import asyncio

from agno.workflow.types import StepInput

import agents.figma_extractor.composition_mode as cm
import app.workflows.helix_composition_only_extractor as w

DGX = "wjSOPgJLnuDztSDx4OIXSM"


def _pb_result(remote=2, status="success"):
    async def f(fk):
        return {"pathway_b_status": status,
                "composition_tree": [{"page_id": "p1", "page_name": "Components", "frames": [
                    {"id": "1", "name": "Btn", "type": "COMPONENT", "depth": 1}]}],
                "pages": [{"page_id": "p1", "page_name": "Components", "frame_count": 1,
                           "remote_ref_count": remote, "local_component_count": 5, "depth_bounded": False}],
                "page_count": 1, "frame_count_total": 1, "remote_reference_count": remote,
                "remote_references": [{"key": f"r{i}", "ref_type": "component"} for i in range(remote)],
                "failure_reports": [], "gaps": []}
    return f


# ---- composition-only mode (registered_libraries=[] -> Lane 6 skipped) ------
def test_composition_only_skips_lane6():
    r = asyncio.run(cm.run_composition_extraction(DGX, registered_libraries=[],
                                                  pathway_b=_pb_result(remote=3), now=lambda: "T"))
    assert r["extraction_mode"] == "composition-only"
    assert r["resolution_events"] == [] and r["resolution_summary"] is None  # Lane 6 SKIPPED
    assert r["provenance"]["resolution"] == "skipped-self-contained"
    assert r["remote_reference_count"] == 3  # remote refs still REPORTED, just not resolved
    assert "lane6_note" in r and r["provenance"]["llm_involvement"] == "none"
    assert r["status"] == "success"  # pathway_b success, no unresolved-driven partial


def test_composition_only_partial_on_pathway_b_partial():
    r = asyncio.run(cm.run_composition_extraction(DGX, registered_libraries=[],
                                                  pathway_b=_pb_result(status="partial"), now=lambda: "T"))
    assert r["extraction_mode"] == "composition-only" and r["status"] == "partial"


def test_backward_compat_libraries_provided_runs_lane6():
    async def fake_resolve(fk, refs, libs):
        ev = [{"event_type": "resolution_started"},
              {"event_type": "resolution_complete", "resolution_summary": {"references_total": 2,
               "references_resolved": 2, "references_unresolved": 0}}]
        return {"events": ev, "resolution_complete": ev[-1]}
    r = asyncio.run(cm.run_composition_extraction(
        DGX, registered_libraries=[{"file_key": "core", "role": "core_foundation", "priority_order": 0}],
        pathway_b=_pb_result(remote=2), resolve=fake_resolve, now=lambda: "T"))
    assert r["extraction_mode"] == "composition"  # NOT composition-only when a library is registered
    assert r["resolution_summary"]["references_resolved"] == 2  # Lane 6 ran
    assert "lane6_note" not in r
    assert r["warnings"] == []  # normal composition mode -> no FM-2 warning


# ---- FM-2 mitigation: composition-only + 0 remote refs -> explicit output warning ----
def test_composition_only_zero_refs_emits_warning():
    r = asyncio.run(cm.run_composition_extraction(DGX, registered_libraries=[],
                                                  pathway_b=_pb_result(remote=0), now=lambda: "T"))
    assert r["extraction_mode"] == "composition-only" and r["remote_reference_count"] == 0
    assert len(r["warnings"]) == 1
    warn = r["warnings"][0]
    assert warn["event_type"] == "composition_only_mode_no_remote_refs"
    assert warn["severity"] == "warning"
    assert warn["extraction_still_succeeded"] is True
    assert warn["lane6_status"] == "skipped_self_contained"
    assert isinstance(warn["reference_files_to_check"], list) and warn["reference_files_to_check"]
    assert r["status"] == "success"  # warning is informational, does NOT change status


def test_composition_only_with_refs_no_warning():
    r = asyncio.run(cm.run_composition_extraction(DGX, registered_libraries=[],
                                                  pathway_b=_pb_result(remote=3), now=lambda: "T"))
    assert r["extraction_mode"] == "composition-only" and r["remote_reference_count"] == 3
    assert r["warnings"] == []  # composition-only WITH remote refs -> no FM-2 warning


def test_composition_only_zero_refs_warning_deterministic():
    def run():
        return asyncio.run(cm.run_composition_extraction(DGX, registered_libraries=[],
                                                         pathway_b=_pb_result(remote=0), now=lambda: "T"))
    assert run()["warnings"] == run()["warnings"]  # byte-identical for identical 0-ref input


# ---- workflow: parsing + executor + registration --------------------------
def test_parse_one_key():
    assert w._parse_one_key(f"https://www.figma.com/design/{DGX}/B-S") == DGX
    assert w._parse_one_key(f"just {DGX} here") == DGX
    assert w._parse_one_key("nope") is None


def test_executor_composition_only(monkeypatch):
    captured = {}

    async def fake_run(fk, *, registered_libraries, file_role):
        captured.update(fk=fk, libs=registered_libraries, role=file_role)
        return {"extraction_mode": "composition-only", "status": "success"}

    async def not_throttled():  # isolate the account-429 guard's probe -> offline, no real backoff
        import agents.figma_extractor.http_errors as he
        return he.ProbeResult(False)
    monkeypatch.setattr(w, "run_composition_extraction", fake_run)
    monkeypatch.setattr(w, "make_account_probe", lambda fk: not_throttled)
    out = asyncio.run(w.composition_only_executor(StepInput(input=f"design/{DGX}")))
    assert out.success is True and captured["fk"] == DGX and captured["libs"] == []
    assert captured["role"] == "self_contained"


def test_executor_missing_key_fails():
    out = asyncio.run(w.composition_only_executor(StepInput(input="no key")))
    assert out.success is False and out.content["error_class"] == "missing_file_key"


def test_workflow_shape():
    wf = w.helix_composition_only_extractor_workflow
    assert wf.id == "helix-composition-only-extractor"
    assert len(wf.steps) == 1 and wf.steps[0].name == "composition-only-extract"


def test_registered_in_agentos():
    try:
        import app.main as m
    except Exception as e:  # pragma: no cover - env-dependent
        import pytest
        pytest.skip(f"app.main import needs runtime env: {e!r}")
    ids = {getattr(x, "id", None) for x in m.agent_os.workflows}
    assert {"helix-figma-extractor", "helix-client-extractor",
            "helix-composition-only-extractor"} <= ids
