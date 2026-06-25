"""
Context7 Tools
==============

Example wrapper around an external, authenticated MCP server — Context7's
hosted endpoint (``https://mcp.context7.com/mcp``). Use this as the template
for any third-party MCP that needs an API key.

Auth finding (agno 2.x, ``agno/tools/mcp/mcp.py``)
--------------------------------------------------
``MCPTools`` **does support client-side auth headers** over HTTP transports.
Two ways:

1. Static headers — pass ``server_params=StreamableHTTPClientParams(url=...,
   headers={...})``. The headers ride on every request to the MCP server. This
   is what we use below: simple, and the key is read once at construction.
2. Dynamic headers — pass ``header_provider=<callable returning a dict>``. agno
   opens a fresh session per agent run and merges the returned headers in.
   Useful for short-lived/rotating tokens. (Only valid on ``sse`` /
   ``streamable-http`` transports — agno raises otherwise.)

We therefore send the key client-side (no dependency on the MCP server reading
its own env). If Context7 expects the key in a differently-named header, adjust
``CONTEXT7_AUTH_HEADER`` below.
"""

from os import getenv

from agno.tools.mcp import MCPTools
from agno.tools.mcp.params import StreamableHTTPClientParams

CONTEXT7_MCP_URL = "https://mcp.context7.com/mcp"
# Context7 accepts the API key as a Bearer token. Override the header name here
# if the upstream service expects something else.
CONTEXT7_AUTH_HEADER = "Authorization"


def get_context7_tools() -> MCPTools:
    """Build the Context7 MCP toolkit.

    Reads ``CONTEXT7_API_KEY`` from the environment and passes it to the MCP
    server as a client-side ``Authorization: Bearer <key>`` header (agno
    supports this via ``StreamableHTTPClientParams(headers=...)``).

    If the key is unset, the connection is made without auth headers — the
    Context7 endpoint may then refuse or rate-limit requests; set the key in
    ``.env`` to authenticate.
    """
    api_key = getenv("CONTEXT7_API_KEY")
    headers = {CONTEXT7_AUTH_HEADER: f"Bearer {api_key}"} if api_key else None

    server_params = StreamableHTTPClientParams(url=CONTEXT7_MCP_URL, headers=headers)
    return MCPTools(server_params=server_params, transport="streamable-http")
