"""
Reasoning Agent
===============

Strategic advisor that reasons explicitly via agno's ReasoningTools (think/analyze).
"""

from agno.agent import Agent
from agno.models.openai import OpenAIChat
from agno.tools.reasoning import ReasoningTools

from db import get_postgres_db

# IMPORTANT — model class is OpenAIChat, NOT default_model() (which is OpenAIResponses).
# ReasoningTools breaks on the OpenAIResponses -> Anthropic (claude) via-LiteLLM route
# with "sequence item 0: expected str instance, NoneType found" (the /v1/responses
# reasoning blocks carry None content that poisons agno's message join). Validated
# 2026-06-25: with OpenAIChat the run completes and think()/analyze() execute.
# See shared-results:programmatic-agent-4c-reasoning-result. The hardcoded id mirrors
# app.settings.default_model(); base_url + key come from OPENAI_BASE_URL/OPENAI_API_KEY.

REASONING_INSTRUCTIONS = """\
You are a strategic advisor for Dark Factory team members. For every non-trivial
question: first call think() to break the problem into steps, then call analyze()
to weigh trade-offs and decide next actions, then give a concise, structured
final answer. Keep the final answer direct — the user prefers brevity over padding.
"""

reasoning_agent = Agent(
    id="reasoning-agent",
    name="Reasoning Agent",
    model=OpenAIChat(id="gpt-5.4"),
    db=get_postgres_db(),
    tools=[ReasoningTools(add_instructions=True, add_few_shot=True)],
    instructions=REASONING_INSTRUCTIONS,
    enable_user_memories=True,
    add_datetime_to_context=True,
    add_history_to_context=True,
    num_history_runs=5,
    markdown=True,
)
