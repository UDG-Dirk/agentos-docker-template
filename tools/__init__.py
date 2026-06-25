"""
Tools Package
=============

Shared tool factories for agents. Each factory builds a configured agno
toolkit so agent files stay declarative — they import a factory and drop the
result into their ``tools=[...]`` list instead of wiring up MCP/SDK clients
inline.

Re-exports:
- ``get_web_search_tools`` — Parallel SDK (keyed) or keyless MCP fallback.
- ``get_context7_tools``   — Context7 external MCP wrapper.
"""

from tools.context7 import get_context7_tools
from tools.parallel_search import get_web_search_tools

__all__ = [
    "get_web_search_tools",
    "get_context7_tools",
]
