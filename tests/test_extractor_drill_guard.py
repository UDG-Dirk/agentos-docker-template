"""Deterministic (synthetic) tests for the Option D drill guard in extract_with_retry.

These exercise the REAL ``extract_with_retry`` control flow — only the agent's
``arun`` is stubbed, so no Figma API / MCP / model call is made and no FIGMA_PAT is
needed. They cover: the node-level drill counter, the hard-abort on persistent
shallow finalize (negative path / HITL surface), a well-behaved extraction passing
the guard (positive path), and the escalating drill nudge on the final attempt.

The real-Figma live-fire (parser_model non-empty extraction) lives separately in
tests/live_fire/ and needs FIGMA_PAT.
"""
from __future__ import annotations

import asyncio

import pytest

import app.workflows.helix_figma_extractor as w
from agents.figma_extractor.models import ComponentEntry, FigmaExtractionResult, PageInfo, TokenEntry


# --- synthetic stand-ins for agno RunOutput / ToolExecution -------------------
class _FakeTool:
    def __init__(self, tool_name, tool_args):
        self.tool_name = tool_name
        self.tool_args = tool_args


class _FakeResp:
    def __init__(self, content, tools):
        self.content = content
        self.tools = tools


class _FakeStepInput:
    """Minimal StepInput stand-in: extract_with_retry only reads .input,
    .previous_step_content, and .workflow_session.session_id."""
    def __init__(self, message):
        self.input = message
        self.previous_step_content = None
        self.workflow_session = None


def _empty_result():
    # discovery happened (pages listed) but nothing extracted — the RULE 1 shape
    return FigmaExtractionResult(
        file_key="8qPSyetzviLR6eF6bkpL44",
        pages_discovered=[PageInfo(name="Colors", node_id="360:38", page_type="foundation")],
    )


def _rich_result():
    return FigmaExtractionResult(
        file_key="8qPSyetzviLR6eF6bkpL44",
        pages_discovered=[PageInfo(name="Colors", node_id="360:38", page_type="foundation")],
        tokens=[TokenEntry(name="--color-primary-500", value="#3388F0", category="color")],
        components=[ComponentEntry(name="Button", node_id="57:766")],
    )


def _root_only_tools():
    return [_FakeTool("get_figma_data", {"nodeId": "0:0"})]


def _drilled_tools(n=3):
    return [_FakeTool("get_figma_data", {"nodeId": "0:0"})] + [
        _FakeTool("get_figma_data", {"nodeId": f"57:{700 + i}"}) for i in range(n)
    ]


def _patch_arun(monkeypatch, responses):
    """Feed a fixed sequence of _FakeResp across attempts; record messages sent."""
    seq = iter(responses)
    sent = []

    async def fake_arun(**kwargs):
        sent.append(kwargs.get("input", ""))
        return next(seq)

    monkeypatch.setattr(w.figma_extractor_agent, "arun", fake_arun)
    return sent


# --- counter unit tests -------------------------------------------------------
def test_count_node_drill_calls_excludes_root_and_nonfigma():
    resp = _FakeResp(
        content=None,
        tools=[
            _FakeTool("get_figma_data", {"nodeId": "0:0"}),      # root — not counted
            _FakeTool("get_figma_data", {"nodeId": "0:1"}),      # playground — not counted
            _FakeTool("get_figma_data", {"nodeId": "360:38"}),   # drill — counted
            _FakeTool("get_figma_data", {"node_id": "57:766"}),  # snake_case key — counted
            _FakeTool("download_figma_images", {"nodes": []}),   # different tool — not counted
            _FakeTool("get_figma_data", {}),                     # no nodeId — not counted
        ],
    )
    assert w._count_node_drill_calls(resp) == 2


def test_count_node_drill_calls_handles_missing_tools():
    assert w._count_node_drill_calls(_FakeResp(content=None, tools=None)) == 0


# --- negative path: persistent shallow finalize -> hard-abort (HITL surface) ---
def test_drill_guard_hard_abort_on_persistent_shallow(monkeypatch):
    _patch_arun(
        monkeypatch,
        [_FakeResp(_empty_result(), _root_only_tools()) for _ in range(w.MAX_EXTRACTION_ATTEMPTS)],
    )
    step_input = _FakeStepInput("Extract Figma file 8qPSyetzviLR6eF6bkpL44")
    with pytest.raises(w.DrillGuardAbort) as ei:
        asyncio.run(w.extract_with_retry(step_input))
    diag = ei.value.diagnostic
    assert diag["error"] == "drill_guard_hard_abort"
    assert diag["node_level_drill_calls"] == 0          # only root calls were made
    assert diag["pages_discovered"] >= 1                # discovery succeeded
    assert diag["drill_threshold_N"] == w.DRILL_THRESHOLD_N
    assert "premature-finalize" in diag["suggestion"]


# --- positive path: a well-behaved extraction passes the guard ----------------
def test_drill_guard_passes_well_behaved_extraction(monkeypatch):
    _patch_arun(monkeypatch, [_FakeResp(_rich_result(), _drilled_tools(3))])
    step_input = _FakeStepInput("Extract Figma file 8qPSyetzviLR6eF6bkpL44")
    out = asyncio.run(w.extract_with_retry(step_input))
    assert out.content is not None
    assert out.content.tokens and out.content.components
    assert out.success is not False  # accepted


# --- recovery path: empty first, rich on retry -> accepted, no abort ----------
def test_drill_guard_recovers_on_retry(monkeypatch):
    _patch_arun(
        monkeypatch,
        [
            _FakeResp(_empty_result(), _root_only_tools()),   # attempt 1: shallow empty
            _FakeResp(_rich_result(), _drilled_tools(4)),     # attempt 2: recovers
        ],
    )
    step_input = _FakeStepInput("Extract Figma file 8qPSyetzviLR6eF6bkpL44")
    out = asyncio.run(w.extract_with_retry(step_input))
    assert out.content is not None and out.content.tokens


# --- escalation: final attempt appends the explicit drill nudge when shallow --
def test_escalating_drill_nudge_on_final_attempt(monkeypatch):
    sent = _patch_arun(
        monkeypatch,
        [_FakeResp(_empty_result(), _root_only_tools()) for _ in range(w.MAX_EXTRACTION_ATTEMPTS)],
    )
    step_input = _FakeStepInput("Extract Figma file 8qPSyetzviLR6eF6bkpL44")
    with pytest.raises(w.DrillGuardAbort):
        asyncio.run(w.extract_with_retry(step_input))
    assert len(sent) == w.MAX_EXTRACTION_ATTEMPTS
    assert w._CONTINUE_NUDGE.strip()[:20] not in sent[0]          # attempt 1 = clean
    assert w._CONTINUE_NUDGE.strip()[:20] in sent[1]              # attempt 2 = continue nudge
    assert "HARD REQUIREMENT" in sent[-1]                         # final = drill nudge escalated


def test_scrub_secrets_redacts_pat(monkeypatch):
    monkeypatch.setenv("FIGMA_PAT", "figd_SUPERSECRETVALUE123")
    scrubbed = w._scrub_secrets("token is figd_SUPERSECRETVALUE123 here")
    assert "SUPERSECRET" not in scrubbed
    assert "<redacted>" in scrubbed
