"""
WebSearch Agent
===============
"""

from agno.agent import Agent

from app.settings import default_model
from db import get_postgres_db
from tools.parallel_search import get_web_search_tools

# Parallel SDK (when PARALLEL_API_KEY is set) or keyless MCP fallback.
# See tools/parallel_search.py for the exact conditional. AgentOS handles
# MCP connect/close as part of its lifespan.
web_tools = get_web_search_tools()


WEB_SEARCH_INSTRUCTIONS = """\
Search the web for current information.

Workflow:
1. Use the search tool to find candidate sources for the question.
2. For recent-event, “latest,” or “recently” questions, answer only from search results you actually found in this run; do not infer newer publications, titles, or dates beyond what the results support.
3. When the user asks about specific pages, or when search snippets are too thin to safely summarize a recent claim, follow up with the extract / fetch tool to read the most relevant URLs before answering.
4. Cite the sources you used as plain URLs. Prefer recent, authoritative pages. If you cannot find a good answer, say so plainly.
"""


web_search = Agent(
    id="web-search",
    name="WebSearch",
    model=default_model(),
    db=get_postgres_db(),
    tools=[web_tools],
    instructions=WEB_SEARCH_INSTRUCTIONS,
    enable_agentic_memory=True,
    add_datetime_to_context=True,
    add_history_to_context=True,
    num_history_runs=5,
    markdown=True,
)
