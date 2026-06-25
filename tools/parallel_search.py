"""
Web Search Tools
================

Factory for the web-search toolkit used by the WebSearch Agent.

Extracted from ``agents/web_search.py`` so the agent file stays declarative.
The conditional below is the exact behaviour the agent used inline before the
refactor — keep them in sync.
"""

from os import getenv

from agno.tools.mcp import MCPTools
from agno.tools.parallel import ParallelTools


def get_web_search_tools() -> ParallelTools | MCPTools:
    """Build the web-search toolkit.

    When ``PARALLEL_API_KEY`` is set, use the official parallel-web SDK — the
    agent gets ``parallel_search`` and ``parallel_extract`` directly. Without a
    key, fall back to the keyless MCP endpoint and the agent gets ``web_search``
    and ``web_fetch`` instead. AgentOS handles MCP connect/close as part of its
    lifespan.
    """
    if getenv("PARALLEL_API_KEY"):
        return ParallelTools()
    return MCPTools(url="https://search.parallel.ai/mcp", transport="streamable-http")
