#!/usr/bin/env python3
"""
Spike S1 Track B — headless Figma extraction inside an Agno agent via a PAT-based community MCP server.

Validates: can an Agno agent call a PAT-authenticated Figma MCP server over stdio,
with NO browser / NO OAuth / NO human in the loop?

Secret handling: FIGMA_PAT is loaded from helix-poc-agno/.env via python-dotenv and
passed to the MCP child process; it is NEVER printed. Model creds come from the
poc-agno-template/.env (LiteLLM proxy).

Run:
  cd /home/dirk/opencode/workbench/helix-poc-agno
  ../agno-setup/poc-agno-template/.venv/bin/python spike_s1_trackb_agno_probe.py
"""
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
TEMPLATE_ENV = HERE.parent / "agno-setup" / "poc-agno-template" / ".env"
LOCAL_ENV = HERE / ".env"
OUT = HERE / "spike_s1" / "trackb_agno_result.json"

# --- env: model creds from template, FIGMA_PAT from local. override=False so the
#     local FIGMA_PAT and template model creds coexist; neither clobbers the other. ---
load_dotenv(LOCAL_ENV, override=False)       # FIGMA_PAT (+ anything local)
load_dotenv(TEMPLATE_ENV, override=False)    # OPENAI_MODEL_ID / OPENAI_BASE_URL / OPENAI_API_KEY

FIGMA_PAT = os.environ.get("FIGMA_PAT") or os.environ.get("FIGMA_API_KEY")
MODEL_ID = os.environ.get("OPENAI_MODEL_ID", "gpt-5.4")
FILE_KEY = "8qPSyetzviLR6eF6bkpL44"
# AUTHORITATIVE node ids (from Track A MCP metadata). The task spec's "457-729=Button"
# repeats Dirk's off-by-one labels; 457:729 is actually Dropdown. Real Button = 57:645.
BUTTON_NODE = "57:645"

if not FIGMA_PAT:
    print(json.dumps({"status": "FAIL", "error": "FIGMA_PAT not found in env after dotenv load"}))
    sys.exit(2)


class TrackBResult(BaseModel):
    server_used: str = Field(description="which community MCP server was used")
    mcp_tools_seen: List[str] = Field(default_factory=list, description="tool names exposed by the MCP server")
    tokens_found: List[str] = Field(default_factory=list, description="design token / variable names or 'name: value' pairs extracted for the Button")
    component_variants_found: List[str] = Field(default_factory=list, description="variant/state combinations identified for the Button")
    code_connect_present: Optional[bool] = Field(default=None, description="did the server return Code Connect mapped component data?")
    notes: str = Field(default="", description="anything notable: format, gaps, errors")
    errors: List[str] = Field(default_factory=list)


async def run_with_framelink():
    from agno.agent import Agent
    from agno.models.openai import OpenAIChat
    from agno.tools.mcp import MCPTools
    from mcp import StdioServerParameters

    server_params = StdioServerParameters(
        command="npx",
        args=["-y", "figma-developer-mcp", f"--figma-api-key={FIGMA_PAT}", "--stdio"],
        env={**os.environ},
    )

    async with MCPTools(server_params=server_params, timeout_seconds=120) as mcp_tools:
        # discover exposed tools (Phase 0 documentation for Framelink, captured headlessly)
        tool_names = []
        try:
            fns = getattr(mcp_tools, "functions", None) or {}
            tool_names = sorted(fns.keys())
        except Exception as e:  # pragma: no cover
            tool_names = [f"<introspection failed: {e}>"]
        print("FRAMELINK MCP TOOLS:", tool_names, file=sys.stderr)

        agent = Agent(
            model=OpenAIChat(id=MODEL_ID),  # OpenAIChat (NOT OpenAIResponses) for tool round-trips via LiteLLM
            tools=[mcp_tools],
            output_schema=TrackBResult,
            instructions=(
                "You are a headless Figma extraction agent. Use the available Figma MCP tools "
                f"(PAT-authenticated, no browser) to extract from file {FILE_KEY}, node {BUTTON_NODE} "
                "(the Button component). Call the server's get-figma-data / get-node style tool. "
                "Report: the MCP tool names you saw, every design token / variable name (with values "
                "if present), every component variant/state combination, whether Code Connect data was "
                "returned, and notes on the output format. If a tool errors, record the exact error."
            ),
            markdown=False,
        )
        task = (
            f"Extract the Button component from Figma file {FILE_KEY}, node {BUTTON_NODE}. "
            "Return the complete token list and component variant structure."
        )
        resp = await agent.arun(task)
        content = resp.content
        if isinstance(content, TrackBResult):
            data = content.model_dump()
        elif isinstance(content, BaseModel):
            data = content.model_dump()
        else:
            data = {"raw": str(content)}
        data["_mcp_tools_introspected"] = tool_names
        data["_server"] = "figma-developer-mcp (Framelink)"
        data["_node"] = BUTTON_NODE
        data["_model_id"] = MODEL_ID
        return data


async def main():
    result = {"status": None}
    try:
        data = await run_with_framelink()
        result = {"status": "PASS", "server": "framelink", **data}
    except Exception as e:
        import traceback
        result = {
            "status": "FAIL",
            "server": "framelink",
            "error": repr(e),
            "traceback": traceback.format_exc(),
        }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2, default=str))
    # stderr summary only — never print the PAT
    print(f"STATUS={result.get('status')} server={result.get('server')} -> {OUT}", file=sys.stderr)
    if result.get("status") != "PASS":
        print("ERROR:", str(result.get("error"))[:400], file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
