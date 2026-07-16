"""HELIX 3b Semantic Matcher — shared typed contracts.

The LLM-escalation output schema (spec v3.1). This is the agent-facing contract:
the future 3b Agno Step matcher and the testbench LLM surfaces (llm_path.py) both
emit this shape, so abstention behaviour is defined in exactly one place.

Note (repo state, 2026-07-16): there is not yet a standalone `matcher.py` agent —
the full 3b Agno Step wrapper is still deferred ("later work" per the parent
testbench task). The only live LLM invocation surface is the testbench's
`llm_path.py` (MockLLM + the real OpenAIChat/LiteLLM RealLLM). This module holds
the schema both that surface and the eventual matcher will import.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

Outcome = Literal["map", "unmappable"]
Confidence = Literal["authoritative", "high", "medium", "unresolved"]


class LLMMatchResult(BaseModel):
    """Structured result of one LLM escalation.

    outcome="map"        -> baseline_var names the chosen baseline token.
    outcome="unmappable" -> the LLM abstained; baseline_var is None and the token
                            routes to the unmapped register with reason
                            'llm_abstained'. rationale must explain why.
    """

    outcome: Outcome
    baseline_var: Optional[str] = None  # None when outcome="unmappable"
    confidence: Confidence = "unresolved"
    rationale: str = ""

    def model_post_init(self, _ctx) -> None:  # defensive contract check
        if self.outcome == "map" and not self.baseline_var:
            raise ValueError("outcome='map' requires a non-null baseline_var")
        if self.outcome == "unmappable" and self.baseline_var is not None:
            raise ValueError("outcome='unmappable' requires baseline_var=None")
