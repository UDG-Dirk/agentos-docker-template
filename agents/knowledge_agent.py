"""
Knowledge Agent
===============

Answers questions grounded in the Dark Factory knowledge base.

Retrieval is TOOL-FREE: ``add_knowledge_to_context=True`` injects the most
relevant KB documents into the prompt during message building, and
``search_knowledge=False`` disables the ``search_knowledge_base`` tool. This
deliberately avoids the Anthropic-via-LiteLLM tool route (validated 2026-06-25,
see shared-results:programmatic-agent-4c-reasoning-result). Because there are no
tools, ``default_model()`` (OpenAIResponses) is fine here.

NOTE: retrieval embeds the query via the KB's embedder — see the embedder caveat
in knowledge/dark_factory_kb.py (db/session.py now defaults to the LiteLLM-prefixed
id). The KB must be ingested first; app/main.py calls ingest() on startup.
"""

from agno.agent import Agent

from app.settings import default_model
from db import get_postgres_db
from knowledge.dark_factory_kb import dark_factory_knowledge

KNOWLEDGE_INSTRUCTIONS = """\
You answer questions about the Dark Factory using the reference documents
provided in your context. Ground every answer in those references — quote or
paraphrase them, and do not invent facts beyond them. If the references do not
cover the question, say so plainly. Be concise.
"""

knowledge_agent = Agent(
    id="knowledge-agent",
    name="Knowledge Agent",
    model=default_model(),
    db=get_postgres_db(),
    knowledge=dark_factory_knowledge,
    add_knowledge_to_context=True,
    search_knowledge=False,
    enable_user_memories=True,
    add_datetime_to_context=True,
    add_history_to_context=True,
    num_history_runs=5,
    markdown=True,
)
