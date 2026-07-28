"""HELIX Client Extractor — deployed AgentOS workflow for multi-file (per-client) extraction.

Registers `composition_mode.extract_client_design_system` as a PROD workflow endpoint invocable via
the direct-REST pattern (`POST /workflows/helix-client-extractor/runs`, form-urlencoded `message=…`).

Multi-parameter shape is carried in the run `message` (Agno workflows take a single string input),
parsed deterministically: two Figma keys/URLs (first = Core / foundation library, second = client /
composition file), optional `additional=<k1,k2>` libraries and `freshness=<days>`.

Lane 6 streaming events ride, in deterministic order, inside the returned StepOutput content
(`client_extraction.resolution_events`) — the workflow run's SSE emits the Step events; per-event
interleaving of Lane 6 events onto the workflow SSE beyond that is bounded by Agno's step-event model
(events are all present + ordered in the result; see deliverable note).
"""
from __future__ import annotations

import re

from agno.workflow import Step, Workflow
from agno.workflow.types import StepInput, StepOutput

from agents.figma_extractor.composition_mode import extract_client_design_system
from agents.figma_extractor.http_errors import (
    make_account_probe,
    make_scope_classifier,
    with_account_level_retry,
)
from db import get_postgres_db

_KEY_RE = re.compile(r"/(?:file|design)/([A-Za-z0-9]{20,40})")
_BARE_RE = re.compile(r"\b([A-Za-z0-9]{20,40})\b")


def _parse_client_request(message: str) -> dict:
    """Parse (core_file_key, client_file_key, additional_library_keys, freshness_threshold_days) from
    the run message. Supports labelled (`core=… client=…`) and positional (first key = core, second =
    client) forms. Returns {} if fewer than two file keys are present."""
    msg = message or ""
    out: dict = {"additional_library_keys": [], "freshness_threshold_days": None}

    def _key(tok: str) -> str | None:
        m = _KEY_RE.search(tok) or _BARE_RE.search(tok)
        return m.group(1) if m else None

    # labelled form
    core_lbl = re.search(r"core\s*[=:]\s*(\S+)", msg, re.I)
    client_lbl = re.search(r"client\s*[=:]\s*(\S+)", msg, re.I)
    if core_lbl and client_lbl:
        out["core_file_key"] = _key(core_lbl.group(1))
        out["client_file_key"] = _key(client_lbl.group(1))
    else:
        # positional: first two file keys in the message, in order
        keys = _KEY_RE.findall(msg) or _BARE_RE.findall(msg)
        if len(keys) >= 2:
            out["core_file_key"], out["client_file_key"] = keys[0], keys[1]
    add = re.search(r"additional\s*[=:]\s*([A-Za-z0-9,]+)", msg, re.I)  # comma-separated, no spaces
    if add:
        out["additional_library_keys"] = [k.strip() for k in add.group(1).split(",") if k.strip()]
    fr = re.search(r"freshness\s*[=:]\s*(\d+)", msg, re.I)
    if fr:
        out["freshness_threshold_days"] = int(fr.group(1))
    if not out.get("core_file_key") or not out.get("client_file_key"):
        return {}
    return out


def _missing_params_prompt(message: str) -> dict:
    """Actionable prompt returned when required keys are absent — the 'give the user a chance to enter
    their libs' UX (Dirk 2026-07-28). MCP/REST callers render this structured guidance instead of a
    terse error, then re-invoke with the params. Deterministic, zero-LLM.

    NOTE (Phase A investigation): true interactive elicitation is NOT available on the current surface —
    the agno-prod MCP exposes a GENERIC `run_workflow(workflow_id, message)` (one string input, no
    per-workflow typed params, no MCP `elicitation/create`), and Agno HITL is output-review, not
    input-collection. So this structured prompt-on-missing is the achievable mechanism; first-class
    elicitation would need an Agno/MCP capability we don't have (escalated in the deliverable)."""
    found = _KEY_RE.findall(message or "") or _BARE_RE.findall(message or "")
    return {
        "workflow": "extract_client_design_system",
        "status": "needs_parameters",
        "needs_parameters": True,
        "error_class": "missing_file_keys",
        "message": "This workflow needs two Figma file keys. Re-invoke with them in the run message.",
        "required": {
            "core_file_key": "foundation/Core library file key (or a figma.com/design/<key>/… URL)",
            "client_file_key": "client/composition file key (or URL)",
        },
        "optional": {
            "additional_library_keys": "comma-separated extra library keys — 'additional=<k1>,<k2>'",
            "freshness_threshold_days": "integer, default 7 — 'freshness=14'",
        },
        "message_format": "core=<coreKey> client=<clientKey> [additional=<k1>,<k2>] [freshness=<days>]",
        "examples": [
            "core=8qPSyetzviLR6eF6bkpL44 client=qMi5B9YeqAf9Ik1yN6erw4",
            "https://www.figma.com/design/<coreKey>/Core https://www.figma.com/design/<clientKey>/Client",
        ],
        "detected_keys_in_message": found,
    }


async def client_extract_executor(step_input: StepInput, **kwargs) -> StepOutput:
    """Parse the two file keys from the run message and run the per-client multi-file extraction.
    Missing keys -> a structured, actionable parameters prompt (not a terse error). Never raises."""
    message = step_input.input or step_input.previous_step_content or ""
    req = _parse_client_request(str(message))
    if not req:
        return StepOutput(content=_missing_params_prompt(str(message)), success=False)
    events: list = []
    # Guarded against account-level 429 (bounded auto-retry + escalation) at the orchestration level;
    # per-page 429s still handled inside each lane. Probe the client (composition) file key.
    result = await with_account_level_retry(
        run_extraction=lambda: extract_client_design_system(
            req["core_file_key"], req["client_file_key"],
            additional_library_keys=req["additional_library_keys"],
            freshness_threshold_days=req["freshness_threshold_days"] or 7,
            emit=events.append,  # Lane 6 events forwarded (also in client_extraction.resolution_events)
        ),
        probe=make_account_probe(req["client_file_key"]),
        scope_classifier=make_scope_classifier(req["client_file_key"]),
        target_key=req["client_file_key"],
    )
    # Account-level escalation short-circuits before extraction -> flat throttled failure (no client_extraction).
    if result.get("error_class") == "account_level_rate_limit":
        return StepOutput(content=result, success=False)
    ce = result.get("client_extraction") or {}
    ok = ce.get("status") != "failure" and not ce.get("pathway_b_status") == "failure"
    return StepOutput(content=result, success=ok)


client_extract_step = Step(name="client-extract", executor=client_extract_executor)

helix_client_extractor_workflow = Workflow(
    id="helix-client-extractor",
    name="HELIX Client Extractor",
    description=(
        "Multi-file (per-client) extraction: Library-mode Core (cached) + Composition-mode client "
        "(Pathway B) + Lane 6 streaming cross-file resolution. Message carries two Figma keys/URLs "
        "(core first, client second) + optional additional=<k1,k2> and freshness=<days>."
    ),
    db=get_postgres_db(),
    steps=[client_extract_step],
)
