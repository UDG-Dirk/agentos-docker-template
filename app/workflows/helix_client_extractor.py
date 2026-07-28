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


async def client_extract_executor(step_input: StepInput, **kwargs) -> StepOutput:
    """Parse the two file keys from the run message and run the per-client multi-file extraction.
    Never raises past the step boundary."""
    message = step_input.input or step_input.previous_step_content or ""
    req = _parse_client_request(str(message))
    if not req:
        return StepOutput(
            content={"workflow": "extract_client_design_system", "error": True,
                     "error_class": "missing_file_keys",
                     "message": "need two Figma keys/URLs in the run message (core first, client second; "
                                "or 'core=<key> client=<key>')"},
            success=False)
    events: list = []
    result = await extract_client_design_system(
        req["core_file_key"], req["client_file_key"],
        additional_library_keys=req["additional_library_keys"],
        freshness_threshold_days=req["freshness_threshold_days"] or 7,
        emit=events.append,  # Lane 6 events forwarded (also present in client_extraction.resolution_events)
    )
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
