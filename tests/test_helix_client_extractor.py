"""Tests for the deployed HELIX Client Extractor workflow (task
extract-client-design-system-workflow-registration). Deterministic + offline (executor with a
monkeypatched extract_client_design_system). The direct-REST live smoke is the post-deploy 🟢
confirmation (POST /workflows/helix-client-extractor/runs).
"""
from __future__ import annotations

import asyncio

from agno.workflow.types import StepInput

import app.workflows.helix_client_extractor as w

CORE = "8qPSyetzviLR6eF6bkpL44"
CLIENT = "qMi5B9YeqAf9Ik1yN6erw4"
LIB3 = "QqBwuvd33y3MT4IXP6uGN"


# ---- message parsing -------------------------------------------------------
def test_parse_positional_two_urls():
    msg = f"Extract https://www.figma.com/design/{CORE}/Core and https://www.figma.com/design/{CLIENT}/Modules"
    r = w._parse_client_request(msg)
    assert r["core_file_key"] == CORE and r["client_file_key"] == CLIENT
    assert r["additional_library_keys"] == [] and r["freshness_threshold_days"] is None


def test_parse_labelled_with_additional_and_freshness():
    msg = f"core={CORE} client={CLIENT} additional={LIB3},anotherlibkey1234567 freshness=14"
    r = w._parse_client_request(msg)
    assert r["core_file_key"] == CORE and r["client_file_key"] == CLIENT
    assert r["additional_library_keys"] == [LIB3, "anotherlibkey1234567"]
    assert r["freshness_threshold_days"] == 14


def test_parse_missing_second_key_returns_empty():
    assert w._parse_client_request(f"just one key {CORE}") == {}
    assert w._parse_client_request("") == {}


# ---- executor orchestration (monkeypatched extract_client_design_system) ---
def test_executor_calls_extraction_with_parsed_params(monkeypatch):
    captured = {}

    async def fake_extract(core, client, *, additional_library_keys, freshness_threshold_days, emit):
        captured.update(core=core, client=client, add=additional_library_keys, fresh=freshness_threshold_days)
        for ev in ({"event_type": "resolution_started"}, {"event_type": "resolution_complete"}):
            emit(ev)
        return {"workflow": "extract_client_design_system", "client_extraction": {"status": "success"}}

    monkeypatch.setattr(w, "extract_client_design_system", fake_extract)
    si = StepInput(input=f"core={CORE} client={CLIENT} additional={LIB3} freshness=9")
    out = asyncio.run(w.client_extract_executor(si))
    assert out.success is True
    assert captured == {"core": CORE, "client": CLIENT, "add": [LIB3], "fresh": 9}
    assert out.content["workflow"] == "extract_client_design_system"


def test_executor_missing_keys_fails_cleanly(monkeypatch):
    called = {"n": 0}

    async def fake_extract(*a, **k):
        called["n"] += 1
        return {}
    monkeypatch.setattr(w, "extract_client_design_system", fake_extract)
    out = asyncio.run(w.client_extract_executor(StepInput(input="no keys here")))
    assert out.success is False and out.content["error_class"] == "missing_file_keys"
    assert called["n"] == 0  # never invoked extraction without two keys


def test_executor_marks_failure_on_failed_client_extraction(monkeypatch):
    async def fake_extract(core, client, *, additional_library_keys, freshness_threshold_days, emit):
        return {"client_extraction": {"status": "failure", "pathway_b_status": "failure"}}
    monkeypatch.setattr(w, "extract_client_design_system", fake_extract)
    out = asyncio.run(w.client_extract_executor(StepInput(input=f"{CORE} {CLIENT}")))
    assert out.success is False


# ---- workflow registration -------------------------------------------------
def test_workflow_object_shape():
    wf = w.helix_client_extractor_workflow
    assert wf.id == "helix-client-extractor"
    assert len(wf.steps) == 1 and wf.steps[0].name == "client-extract"


def test_registered_in_agentos():
    # importing app.main constructs AgentOS (needs DB env); guard so the test is environment-tolerant
    try:
        import app.main as m
    except Exception as e:  # pragma: no cover - env-dependent
        import pytest
        pytest.skip(f"app.main import needs runtime env: {e!r}")
    ids = {getattr(x, "id", None) for x in m.agent_os.workflows}
    assert "helix-client-extractor" in ids and "helix-figma-extractor" in ids
