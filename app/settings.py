"""
App Settings
============

Shared runtime objects for the platform.

Model id is centralized here via ``OPENAI_MODEL_ID`` (defaults to ``gpt-5.4``),
so switching the LiteLLM gateway alias or the model is a one-line / one-env
change instead of editing every agent.
"""

from os import getenv

from agno.models.openai import OpenAIChat, OpenAIResponses

# Single source of truth for the model id (LiteLLM gateway alias). Override in the
# environment (e.g. Coolify) to switch models for all agents on the next restart.
MODEL_ID = getenv("OPENAI_MODEL_ID", "gpt-5.4")


def default_model() -> OpenAIResponses:
    """Fresh model per agent (avoids shared-state footguns).

    Use for NON-tool agents. Targets the /v1/responses API — fine when no tools
    round-trip through LiteLLM -> Anthropic.
    """
    return OpenAIResponses(id=MODEL_ID)


def default_chat_model() -> OpenAIChat:
    """Fresh model per agent, for agents with ``tools=[...]`` (and/or ``output_schema``).

    Targets /v1/chat/completions. Tool + schema agents MUST use this over
    ``default_model()``: OpenAIResponses breaks tool round-trips via LiteLLM ->
    Anthropic ("sequence item 0: expected str instance, NoneType found"). RULE 7.
    """
    return OpenAIChat(id=MODEL_ID)
