"""LIVE-FIRE test for Option B (parser_model) — real Figma API + real model via LiteLLM.

Per Dirk's ratification: mocked tests did not catch the empty-extraction failure, so this
fires the REAL extractor against Helix_Core_Library (`8qPSyetzviLR6eF6bkpL44`) and asserts a
NON-EMPTY extraction. Requires FIGMA_PAT (loaded from the repo .env). Skips cleanly without it,
so a PAT-less CI run does not fail — the gate is that it PASSES where the PAT exists (Coolify /
a provisioned local .env).

Targets the extractor AGENT directly (connecting the Framelink MCP via ``async with``), NOT the
full workflow — the workflow's normalize step has requires_output_review=True and would pause for
HITL, which can't complete unattended. Option B is an agent-level fix, so the agent is the right
unit to live-fire.

Run the pytest assertion:   python -m pytest tests/live_fire/test_extractor_parser_model.py -q
Measure parse-success rate: python tests/live_fire/test_extractor_parser_model.py 5
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
# Put repo root on sys.path so `import agents...` works under direct script execution
# (the __main__ measurement harness), not only under pytest which adds rootdir itself.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Load the repo .env so FIGMA_PAT / OPENAI_* are present (mirrors `dotenv run`).
try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except Exception:  # dotenv optional; env may already be exported
    pass

import os  # noqa: E402  (after load_dotenv)

FILE_KEY = "8qPSyetzviLR6eF6bkpL44"  # Helix_Core_Library — the ONLY sanctioned live-fire file
_HAS_PAT = bool(os.environ.get("FIGMA_PAT") or os.environ.get("FIGMA_API_KEY"))

pytestmark = pytest.mark.skipif(not _HAS_PAT, reason="FIGMA_PAT not in env — live-fire skipped")


async def _run_once():
    """One real extraction via the agent. Returns (result, latency_sec)."""
    from agents.figma_extractor.agent import figma_extractor_agent, figma_mcp_tools
    from agents.figma_extractor.models import FigmaExtractionResult

    message = f"Run the Figma extractor. Extract design tokens and components from Figma file {FILE_KEY}"
    t0 = time.monotonic()
    async with figma_mcp_tools:  # connect Framelink MCP for the run (AgentOS does this in prod lifespan)
        resp = await figma_extractor_agent.arun(input=message)
    latency = time.monotonic() - t0
    content = getattr(resp, "content", resp)
    result = content if isinstance(content, FigmaExtractionResult) else None
    return result, latency


def test_parser_model_produces_non_empty_extraction():
    """Core Option B assertion: parser_model path yields a valid, NON-EMPTY FigmaExtractionResult."""
    result, latency = asyncio.run(_run_once())
    assert result is not None, "parser_model did not emit a valid FigmaExtractionResult"
    assert result.tokens or result.components, (
        f"extraction EMPTY (tokens={len(result.tokens)}, components={len(result.components)}) "
        f"— Option B did not resolve premature-finalize; latency={latency:.1f}s"
    )
    # Confabulation watch (log-pull found placeholder file keys under output_schema pressure):
    assert result.file_key == FILE_KEY, f"file_key confabulated: {result.file_key!r}"


def _measure(n: int) -> dict:
    """Fire n real extractions; report parse-valid rate, non-empty rate, latency, sample."""
    parse_valid = non_empty = 0
    latencies: list[float] = []
    sample = {}
    for i in range(n):
        result, latency = asyncio.run(_run_once())
        latencies.append(latency)
        if result is not None:
            parse_valid += 1
            if result.tokens or result.components:
                non_empty += 1
                if not sample:
                    cats = sorted({t.category for t in result.tokens})
                    sample = {
                        "token_count": len(result.tokens),
                        "category_count": len(cats),
                        "component_count": len(result.components),
                    }
        print(f"  run {i + 1}/{n}: parse_valid={result is not None} "
              f"non_empty={bool(result and (result.tokens or result.components))} "
              f"latency={latency:.1f}s")
    return {
        "n": n,
        "parse_success_rate": round(parse_valid / n, 3),
        "non_empty_rate": round(non_empty / n, 3),
        "avg_latency_sec": round(sum(latencies) / len(latencies), 1),
        "model": os.environ.get("FIGMA_PARSER_MODEL_ID", "anthropic/claude-sonnet-4-6"),
        "sample_extraction_result_summary": sample,
    }


if __name__ == "__main__":
    if not _HAS_PAT:
        print("FIGMA_PAT absent — cannot measure. Provision it in .env first.")
        sys.exit(2)
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    import json

    print(json.dumps(_measure(n), indent=2))
