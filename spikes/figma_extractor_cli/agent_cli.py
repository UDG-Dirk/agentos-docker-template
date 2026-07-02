#!/usr/bin/env python3
"""Figma Extractor agent — HELIX UC2 pipeline Step 1.

Two implementation patterns (compared in Track C):
  - sequential : one agent, sequential extraction (cheap, deterministic baseline)
  - broadcast  : Team(2 members, broadcast) + leader consensus

Both produce a FigmaExtractionResult (identical schema). The agent talks to the
Framelink community MCP (figma-developer-mcp) over stdio; the PAT is injected at
the MCP tool layer via StdioServerParameters(env=...) and is NEVER placed in
session_state, logs, or output (RULE 6).

Secrets: FIGMA_PAT from helix-poc-agno/.env; model creds from
poc-agno-template/.env. Dual-dotenv pattern proven in S1 Track B.

Run:
  cd ~/opencode/workbench/agno-setup/poc-agno-template
  .venv/bin/python ~/opencode/workbench/helix-poc-agno/agents/figma_extractor/agent.py \
      --pattern sequential --out <abs path>/run_sequential_001.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# package-local import works whether run as module or script
sys.path.insert(0, str(Path(__file__).resolve().parent))
from models import FigmaExtractionResult  # noqa: E402

HELIX_ROOT = Path(__file__).resolve().parents[2]          # helix-poc-agno/
TEMPLATE_ENV = HELIX_ROOT.parent / "agno-setup" / "poc-agno-template" / ".env"
LOCAL_ENV = HELIX_ROOT / ".env"
SPIKE_S1 = HELIX_ROOT / "spike_s1"
ASSET_DIR = Path(__file__).resolve().parent / "runs" / "assets"

# FIGMA_PAT local; model creds from template. override=False so neither clobbers the other.
load_dotenv(LOCAL_ENV, override=False)
load_dotenv(TEMPLATE_ENV, override=False)

FIGMA_PAT = os.environ.get("FIGMA_PAT") or os.environ.get("FIGMA_API_KEY")
MODEL_ID = os.environ.get("OPENAI_MODEL_ID", "gpt-5.4")
DEFAULT_FILE_KEY = "8qPSyetzviLR6eF6bkpL44"

# === SYSTEM PROMPT (verbatim from agents:figma-extractor:step2-instructions) ===
INSTRUCTIONS = """You are the Figma Extractor agent in the HELIX pipeline. Your job is to extract design tokens, component structures, and variant matrices from a client's Figma design file.

You have access to the Framelink Figma MCP which provides two tools: get_figma_data (extracts design context for a file or node) and download_figma_images (downloads image assets).

The Figma file key for this run is: {figma_file_key}

You MUST follow this exact sequence. Do not skip or reorder steps.

## Phase 1: Discovery
1. Call get_figma_data with ONLY the file key — OMIT the nodeId argument entirely (or use nodeId "0:0" with depth 1, the document root). This returns the page tree. This MUST be your first tool call. Do NOT pass nodeId "0:1": that is the Playground page, not the document root, and returns a single page.
2. Build a page index: every page with name, node id, and page_type classified as "foundation" (Typography, Colors, Layout, Icons, ImageRatios, Text), "component" (Button, Input, Toggle, Checkbox, Dropdown, FormField, etc.), or "other" (Cover, Governance Process, dividers).
3. If discovery returns zero pages, STOP and report an error.
Resolve page names to node IDs from THIS discovery response only. Never trust an externally supplied name->id mapping.

## Phase 2: Foundation Extraction
For each foundation page (Typography, Colors, Layout, Icons, ImageRatios, Text):
4. Call get_figma_data with that page's node id (resolve to a content frame within the page if the page node returns no tokens).
5. Extract token entries: colors, typography scales (family/size/weight/line-height), spacing, sizing, radii, effects.
6. For each token capture: reconstructed CSS-var name, resolved value, style id, and infer category from the name pattern.
7. If enrichment data is provided below, match each token to its Variable slash-path (enrichment_match) and $type (enrichment_type); null if no match.
8. Flag suspected typos in token names (e.g. disbled->disabled, borde-subtle->border-subtle, surfac-subtle->surface-subtle).

## Phase 3: Component Extraction
For each priority component (Button, Input, Toggle, Checkbox, Dropdown, FormField):
9. Call get_figma_data with that component page's node id.
10. Identify the COMPONENT_SET and enumerate variants. Parse axes: Variant (Solid/Outline/Ghost...), Size (sm/md/lg/xl), State (Default/Hover/Active/Focus/Disabled).
11. Build a ComponentEntry with the full variant matrix.
12. List tokens this component consumes (from globalVars/styles in the response).
13. If enrichment provided, attach code_connect_snippet, props, and designer_instructions; else leave null and note the gap.

## Phase 4: Asset Download
14. For image/icon nodes found during extraction (e.g. icons on the Icons page, illustrations), call download_figma_images. This tool WRITES THE FILES TO DISK ITSELF — you do not fetch URLs yourself. You MUST pass all of:
    - localPath: exactly "runs/assets" (relative to the server root; created if missing).
    - nodes: a list of objects each with BOTH nodeId AND fileName. fileName must end in .svg (vector icons) or .png — e.g. {"nodeId": "79:150", "fileName": "icon-arrow-up.svg"}. For a node with an image fill, also include its imageRef.
    Download in batches of <=10 nodes per call.
15. Read the tool response. For EACH file the tool confirms saved, add an AssetEntry with local_path EXACTLY "runs/assets/<fileName>". Set original_url only to a URL the tool actually returned; else leave it "".
16. CRITICAL — NEVER invent or guess a local_path. If download_figma_images errors, returns nothing, you skipped it, or you are not certain a file was written, set local_path="DOWNLOAD_FAILED" and add the node to gaps_detected. An asset entry whose file is not on disk is a FAILURE. Returning zero assets is correct if no image/icon nodes were found or downloads failed.

## Phase 5: Assembly
17. Assemble the complete FigmaExtractionResult (pages, tokens, components, assets, gaps, typos).
18. enrichment_coverage = (tokens with enrichment_match / total tokens), float 0.0-1.0; 0.0 if no enrichment.
19. Report gaps: empty pages, components missing variants, extraction failures, missing enrichment.

## Rules — NEVER violate:
- CONTEXT ECONOMY: after each get_figma_data call, immediately distil that response into the structured tokens/components/variants you need, then move on. Do NOT echo, quote, or restate the raw YAML/JSON tool output in your reasoning or messages — carry forward only the compact structured data. Raw tool payloads are large; repeating them wastes context.
- NEVER finalize after a single tool call. You MUST make at least 7 get_figma_data calls: one discovery call, then one per foundation page and one per priority component (Button, Input, Toggle, Checkbox, Dropdown, FormField). Keep calling tools until every priority component and its tokens are extracted. An output with empty tokens or empty components is a FAILURE — do not return it; keep extracting. If a node returns nothing useful, note it in gaps_detected and move to the next, but do not stop the overall extraction.
- NEVER extract from a node before completing Phase 1 (Discovery).
- NEVER persist ephemeral MCP asset URLs as stable references — download first.
- NEVER include the Figma PAT in output, logs, or content.
- NEVER token-extract a page-level node without resolving to its content frame; union per-frame results.
- ALWAYS keep BOTH naming worlds: CSS-var names (extraction) AND Variable slash-paths (enrichment).
- ALWAYS report gaps and typos. Silent failures are worse than flagged gaps.

## On error:
- MCP connection fails: retry up to 3 times with backoff; if still failing, stop and report.
- A page errors/empty: log it, add to gaps_detected, continue.
- Enrichment missing: extraction-only, enrichment_coverage=0.0, flag gap.
- Context getting large: prioritize foundation pages, then Button/Input/Toggle, then the rest; report truncation in gaps_detected.
"""


# ---------------------------------------------------------------------------
# Enrichment (A5): instruction-context injection from S1 Track A proxy files.
# Avoids the LiteLLM knowledge-search tool route (agno-dev gotcha #4). Format is
# format-agnostic per step1-addendum Q1 — this is the chosen PoC implementation.
# ---------------------------------------------------------------------------
def build_enrichment_context(src_dir: Path = SPIKE_S1) -> tuple[str, bool]:
    """Return (context_text, present). Reads *_variable_defs.json (flat {path:value})
    and *_design_context.json (Code Connect / React+Tailwind) from the S1 proxy dir."""
    if not src_dir.exists():
        return "", False
    var_paths: dict[str, str] = {}
    code_snippets: list[str] = []
    for f in sorted(src_dir.glob("*variable_defs*.json")):
        try:
            data = json.loads(f.read_text())
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, (str, int, float)):
                        var_paths[str(k)] = str(v)
        except Exception:
            continue
    for f in sorted(src_dir.glob("*design_context*.json")):
        try:
            txt = f.read_text()
            code_snippets.append(f"--- {f.stem} ---\n{txt[:1500]}")
        except Exception:
            continue
    if not var_paths and not code_snippets:
        return "", False
    lines = ["", "=== ENRICHMENT DATA (Variable taxonomy + Code Connect, from official MCP) ==="]
    if var_paths:
        lines.append(f"Variable slash-paths ({len(var_paths)} entries) — match tokens to these for enrichment_match:")
        for k, v in list(var_paths.items())[:120]:
            lines.append(f"  {k} = {v}")
    if code_snippets:
        lines.append("\nCode Connect / design-context excerpts (attach to matching ComponentEntry):")
        lines.extend(code_snippets[:4])
    return "\n".join(lines), True


# ---------------------------------------------------------------------------
# Result + metadata coercion
# ---------------------------------------------------------------------------
def _coerce_result(content, file_key: str) -> FigmaExtractionResult:
    from pydantic import BaseModel
    if isinstance(content, FigmaExtractionResult):
        return content
    if isinstance(content, BaseModel):
        return FigmaExtractionResult(**content.model_dump())
    if isinstance(content, dict):
        return FigmaExtractionResult(**content)
    if isinstance(content, str):
        try:
            return FigmaExtractionResult(**json.loads(content))
        except Exception:
            pass
    return FigmaExtractionResult(file_key=file_key, gaps_detected=[f"unparseable agent content: {str(content)[:200]}"])


def _serialize_metrics(resp) -> Optional[dict]:
    m = getattr(resp, "metrics", None)
    if m is None:
        return None
    for attr in ("model_dump", "to_dict", "dict"):
        fn = getattr(m, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
    if isinstance(m, dict):
        return m
    return {"repr": str(m)[:500]}


def _extract_mcp_calls(resp) -> list[dict]:
    """Pull the tool-call sequence from the RunResponse so the harness can verify
    discovery-first (BR-1) without parsing debug logs."""
    calls = []

    def _collect(resp_obj):
        tools = getattr(resp_obj, "tools", None) or []
        for t in tools:
            name = getattr(t, "tool_name", None) or (t.get("tool_name") if isinstance(t, dict) else None)
            args = getattr(t, "tool_args", None) or (t.get("tool_args") if isinstance(t, dict) else None)
            if name:
                calls.append({"tool": name, "args": args})

    _collect(resp)
    # Team: leader RunResponse.tools only shows the delegate call; member tool calls
    # live on member_responses. Walk them so broadcast runs capture real figma calls too.
    for mr in (getattr(resp, "member_responses", None) or []):
        _collect(mr)
    return calls


# ---------------------------------------------------------------------------
# Agent / Team builders
# ---------------------------------------------------------------------------
def _mcp_server_params():
    from mcp import StdioServerParameters
    # PAT injected here ONLY (RULE 6). env carries FIGMA_API_KEY for the child process.
    child_env = {**os.environ, "FIGMA_API_KEY": FIGMA_PAT}
    # cwd roots the Framelink server at the agent dir, so download_figma_images'
    # localPath="runs/assets" writes into agents/figma_extractor/runs/assets/ (HARDEN R2).
    return StdioServerParameters(
        command="npx",
        args=["-y", "figma-developer-mcp", f"--figma-api-key={FIGMA_PAT}", "--stdio"],
        env=child_env,
        cwd=str(Path(__file__).resolve().parent),
    )


def _make_agent(mcp_tools, enrichment_ctx: str, name: str = "figma-extractor"):
    from agno.agent import Agent
    from agno.models.openai import OpenAIChat
    instructions = INSTRUCTIONS + (("\n" + enrichment_ctx) if enrichment_ctx else
                                   "\n=== ENRICHMENT DATA === NONE PROVIDED. Run extraction-only; set enrichment_coverage=0.0 and flag in gaps_detected.")
    return Agent(
        name=name,
        model=OpenAIChat(id=MODEL_ID),  # OpenAIChat (NOT OpenAIResponses) — RULE 7
        tools=[mcp_tools],
        output_schema=FigmaExtractionResult,
        add_session_state_to_context=True,  # exposes {figma_file_key} etc.
        instructions=instructions,
        markdown=False,
    )


async def _run(pattern: str, file_key: str, session_state: dict, out_path: Path) -> dict:
    t0 = time.time()
    enrichment_ctx, enrichment_present = build_enrichment_context()
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    from agno.tools.mcp import MCPTools
    task = (
        f"Extract design tokens, foundation styles, and the six priority components "
        f"(Button, Input, Toggle, Checkbox, Dropdown, FormField) from Figma file {file_key}. "
        f"Follow your Phase 1->5 sequence exactly. Save downloaded assets under: {ASSET_DIR}. "
        f"Return a complete FigmaExtractionResult."
    )

    async with MCPTools(server_params=_mcp_server_params(), timeout_seconds=180) as mcp_tools:
        if pattern == "broadcast":
            from agno.team import Team
            from agno.models.openai import OpenAIChat
            m1 = _make_agent(mcp_tools, enrichment_ctx, name="figma-extractor-1")
            m2 = _make_agent(mcp_tools, enrichment_ctx, name="figma-extractor-2")
            team = Team(
                name="figma-extractor-team",
                model=OpenAIChat(id=MODEL_ID),
                members=[m1, m2],
                delegate_to_all_members=True,
                output_schema=FigmaExtractionResult,
                add_session_state_to_context=True,
                instructions=("Broadcast the extraction task to both members. Synthesize a single "
                              "consensus FigmaExtractionResult: union tokens/components/variants, keep the "
                              "more complete entry on conflict, set extraction_runs to the number of members, "
                              "and set consensus_confidence to the fraction of fields both members agreed on."),
            )
            resp = await team.arun(task, session_state=session_state)
            runs = 2
        else:  # sequential
            agent = _make_agent(mcp_tools, enrichment_ctx)
            resp = await agent.arun(task, session_state=session_state)
            runs = 1

    result = _coerce_result(getattr(resp, "content", resp), file_key)
    result.file_key = result.file_key or file_key
    if not result.extraction_runs or result.extraction_runs < runs:
        result.extraction_runs = runs
    if pattern == "sequential":
        result.consensus_confidence = 1.0
    if not enrichment_present and result.enrichment_coverage != 0.0:
        result.enrichment_coverage = 0.0
        result.gaps_detected.append("enrichment data absent — extraction-only mode")

    mcp_calls = _extract_mcp_calls(resp)
    metrics = _serialize_metrics(resp)
    # HARDEN R2: context-growth proxy. Per-call growth isn't exposed by Agno metrics
    # (totals only); input_tokens/call approximates how much accumulated YAML weighs.
    n_calls = max(1, len(mcp_calls))
    in_tok = (metrics or {}).get("input_tokens")
    context_growth = {
        "mcp_calls": len(mcp_calls),
        "input_tokens": in_tok,
        "output_tokens": (metrics or {}).get("output_tokens"),
        "total_tokens": (metrics or {}).get("total_tokens"),
        "approx_input_tokens_per_call": (round(in_tok / n_calls) if isinstance(in_tok, (int, float)) else None),
        "note": "input grows ~linearly as raw tool outputs accumulate in one context; primary FinOps target.",
    }
    meta = {
        "pattern": pattern,
        "model_id": MODEL_ID,
        "wall_clock_s": round(time.time() - t0, 2),
        "enrichment_present": enrichment_present,
        "mcp_calls": mcp_calls,
        "metrics": metrics,
        "context_growth": context_growth,
        "run_status": str(getattr(resp, "status", None)),
    }
    payload = {"result": result.model_dump(), "_meta": meta}
    out_path.write_text(json.dumps(payload, indent=2, default=str))
    # stderr summary only — never the PAT
    print(f"[{pattern}] status={meta['run_status']} tokens={len(result.tokens)} "
          f"components={len(result.components)} pages={len(result.pages_discovered)} "
          f"mcp_calls={len(meta['mcp_calls'])} wall={meta['wall_clock_s']}s -> {out_path}",
          file=sys.stderr)
    return payload


def _default_session_state(file_key: str) -> dict:
    return {
        "figma_file_key": file_key,
        "storybook_repo_url": "https://rmvc01.rm.udg.de/customer-udg-ai/projects/helix-poc-agno.git",
        "cms_repo_url": "https://rmvc01.rm.udg.de/customer-udg-ai/projects/helix-poc-agno.git",
        "cms_type": "storyblok",
        "framework": "vue",
    }


def main():
    ap = argparse.ArgumentParser(description="HELIX Figma Extractor (Step 1)")
    ap.add_argument("--pattern", choices=["sequential", "broadcast"], default="sequential")
    ap.add_argument("--file-key", default=DEFAULT_FILE_KEY)
    ap.add_argument("--out", required=True, help="absolute path for the run JSON")
    args = ap.parse_args()
    if not FIGMA_PAT:
        print(json.dumps({"status": "FAIL", "error": "FIGMA_PAT missing from env"}))
        sys.exit(2)
    session_state = _default_session_state(args.file_key)
    asyncio.run(_run(args.pattern, args.file_key, session_state, Path(args.out)))


if __name__ == "__main__":
    main()
