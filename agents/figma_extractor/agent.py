"""
Figma Extractor Agent — AgentOS-registered (HELIX UC2 pipeline Step 1)
=====================================================================

Adapted for AgentOS from the standalone HELIX CLI script
(``~/opencode/workbench/helix-poc-agno/agents/figma_extractor/agent.py``):

  * Exposes a module-level ``figma_extractor_agent`` instance — the same
    declarative pattern as ``agents/web_search.py`` (``web_tools`` built at
    import, AgentOS handles MCP connect/close as part of its lifespan).
  * The CLI / argparse / ``asyncio.run`` / enrichment-from-spike-dirs harness is
    removed. This is extraction-only (no enrichment) for the prod spike.
  * Model is ``OpenAIChat`` (NOT ``OpenAIResponses``). Tool + ``output_schema``
    agents break on the OpenAIResponses -> Anthropic-via-LiteLLM route
    ("sequence item 0: expected str instance, NoneType found"). Same lesson as
    ``agents/reasoning_agent.py`` (RULE 7 / shared-results 4c-reasoning).

Secret handling (RULE 6): the Figma PAT is read from the environment
(``FIGMA_PAT`` or ``FIGMA_API_KEY``, injected by Coolify) and passed to the MCP
child process via ``env=`` ONLY — never in argv, logs, session_state, or output.
"""

from __future__ import annotations

import os
from pathlib import Path

from agno.agent import Agent
from agno.models.openai import OpenAIChat
from agno.tools.mcp import MCPTools
from mcp import StdioServerParameters

from agents.figma_extractor.models import FigmaExtractionResult
from app.settings import default_chat_model
from db import get_postgres_db

AGENT_DIR = Path(__file__).resolve().parent

# Coolify-injected. Empty at build time — the MCP child only needs it at connect
# time (AgentOS lifespan), by which point the Coolify env var is present.
FIGMA_PAT = os.environ.get("FIGMA_PAT") or os.environ.get("FIGMA_API_KEY", "")

# Parser model for structured-output assembly (Option B — fixes RULE 1 premature
# finalize). With ``parser_model`` set, the tool-calling base model runs FREE-FORM
# (its tools are not marked strict — see agno parse_tools) and this separate,
# tool-less model converts the base model's final text into ``FigmaExtractionResult``.
# ``output_schema`` stays set: it is the PARSE TARGET, not base-model pressure.
# Separate instance from the base model — never reuse (avoids tool-loop cross-talk).
# Env-overridable so an A/B against gpt-5.4 is a one-env change, no code edit.
FIGMA_PARSER_MODEL_ID = os.environ.get("FIGMA_PARSER_MODEL_ID", "anthropic/claude-sonnet-4-6")


# === SYSTEM PROMPT (verbatim from agents:figma-extractor:step2-instructions,
#     except the file-key line, which now reads the key from the run input
#     instead of a {figma_file_key} session-state placeholder) ===
INSTRUCTIONS = """You are the Figma Extractor agent in the HELIX pipeline. Your job is to extract design tokens, component structures, and variant matrices from a client's Figma design file.

You have access to the Framelink Figma MCP which provides two tools: get_figma_data (extracts design context for a file or node) and download_figma_images (downloads image assets).

The Figma file key to extract is provided in the run input (the user's message). Use exactly that key — do not guess or substitute another.

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

## Enrichment:
No enrichment data is provided in this deployment. Run extraction-only: set enrichment_coverage=0.0 and note the absence in gaps_detected.

## On error:
- MCP connection fails: retry up to 3 times with backoff; if still failing, stop and report.
- A page errors/empty: log it, add to gaps_detected, continue.
- Enrichment missing: extraction-only, enrichment_coverage=0.0, flag gap.
- Context getting large: prioritize foundation pages, then Button/Input/Toggle, then the rest; report truncation in gaps_detected.
"""


def _mcp_server_params() -> StdioServerParameters:
    """Stdio params for the Framelink figma-developer-mcp server.

    ``figma-developer-mcp`` is pre-installed globally in the image (see the Node
    layer in the Dockerfile), so ``npx -y`` resolves without a network pull.

    The PAT is passed via ``env`` only (RULE 6) — never in argv (argv is visible
    via /proc). ``cwd`` roots the server at this agent's directory so
    ``download_figma_images`` (localPath="runs/assets") writes into
    ``/app/agents/figma_extractor/runs/assets/`` (owned by app:app, writable).
    """
    return StdioServerParameters(
        command="npx",
        args=["-y", "figma-developer-mcp", "--stdio"],
        env={**os.environ, "FIGMA_API_KEY": FIGMA_PAT},
        cwd=str(AGENT_DIR),
    )


# Built at import; AgentOS connects/closes it as part of its lifespan (same as
# the WebSearch agent's MCP fallback in tools/parallel_search.py).
figma_mcp_tools = MCPTools(server_params=_mcp_server_params(), timeout_seconds=180)


figma_extractor_agent = Agent(
    id="figma-extractor",
    name="Figma Extractor",
    model=default_chat_model(),  # OpenAIChat (not OpenAIResponses) via app.settings — see module docstring
    db=get_postgres_db(),
    tools=[figma_mcp_tools],
    # Option B: keep output_schema as the parse TARGET, but hand structured-output
    # assembly to a separate tool-less parser_model. This removes the documented
    # RULE 1 anti-pattern (output_schema pressure making the tool-calling model
    # finalize an empty object after ~one call). parser_model MUST be a distinct
    # OpenAIChat instance (RULE 7: not OpenAIResponses) — never the base model.
    output_schema=FigmaExtractionResult,
    parser_model=OpenAIChat(id=FIGMA_PARSER_MODEL_ID),
    instructions=INSTRUCTIONS,
    markdown=False,
)
