"""LLM escalation path for the testbench.

Two implementations behind one interface, both returning an ``LLMMatchResult``
(agents/semantic_matcher/models.py) so abstention is a first-class outcome:

  MockLLM   — default. Deterministic ORACLE stand-in for an IDEAL LLM. Picks the
              candidate a competent model should pick (value proximity as the
              disambiguator) AND abstains (outcome="unmappable") when the case is
              genuinely unmappable. Abstention is oracle-driven (uses the case's
              known expected outcome) so tests stay deterministic; real-LLM
              abstention rate is measured at HARDEN, not here.
  RealLLM   — gated behind HELIX_TESTBENCH_USE_REAL_LLM=1. Single-pass OpenAIChat
              via the project LiteLLM proxy. Its prompt now explicitly permits and
              rewards abstention. Costs are real.

MockLLM abstention policy (spec v3.1):
  When the case is truly unmappable (expected_baseline_var is None) AND the top-1
  candidate's aggregate score is below MOCKLLM_ABSTAIN_SCORE (0.5), the mock
  abstains. The MOCKLLM_ABSTENTION_RATE_OVERRIDE env var scales how often it takes
  that abstention when the condition holds:
     1.0 (default) = always abstain  (ideal LLM)
     0.0           = never abstain   (regress to v0.2 best-of-candidates behaviour)
     0<r<1         = abstain on a deterministic per-token fraction (reproducible)
"""
from __future__ import annotations

import os
import zlib
from typing import Optional

from agents.semantic_matcher.models import LLMMatchResult
from agents.semantic_matcher.scoring import name_similarity, score_candidates, value_distance

MOCKLLM_ABSTAIN_SCORE = 0.5  # top-1 aggregate below this + expected-unmapped => abstain candidate


def _abstention_rate() -> float:
    try:
        return max(0.0, min(1.0, float(os.getenv("MOCKLLM_ABSTENTION_RATE_OVERRIDE", "1.0"))))
    except ValueError:
        return 1.0


def _take_abstention(rate: float, client_token: dict) -> bool:
    """Deterministic gate for fractional abstention rates (reproducible per token)."""
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    key = (client_token.get("name") or "").encode("utf-8")
    frac = (zlib.crc32(key) % 1000) / 1000.0
    return frac < rate


class MockLLM:
    """Deterministic oracle stand-in for an ideal, abstention-capable LLM."""

    name = "mock"

    def match(self, client_token: dict, candidates: list[dict], *,
              expected_baseline_var: Optional[str] = None,
              scoring_result=None) -> LLMMatchResult:
        if not candidates:
            return LLMMatchResult(outcome="unmappable", baseline_var=None,
                                  confidence="unresolved",
                                  rationale="No candidates after pre-filter; nothing to map.")

        # top-1 aggregate (reuse harness scoring if provided, else compute)
        if scoring_result is None:
            scoring_result = score_candidates(client_token, candidates)
        top1_agg = (scoring_result.top_candidates[0].aggregate_score
                    if scoring_result.top_candidates else 0.0)

        # --- oracle abstention ---
        rate = _abstention_rate()
        if expected_baseline_var is None and top1_agg < MOCKLLM_ABSTAIN_SCORE \
                and _take_abstention(rate, client_token):
            return LLMMatchResult(
                outcome="unmappable", baseline_var=None, confidence="unresolved",
                rationale=(f"No plausible baseline match: best candidate aggregate "
                           f"{top1_agg:.2f} < {MOCKLLM_ABSTAIN_SCORE}. Abstaining."))

        # --- map: pick best by value proximity, then name similarity ---
        c_type = client_token.get("dtcg_type")
        c_val = client_token.get("value")
        c_name = client_token.get("name") or ""

        def key(cand: dict):
            vd = value_distance(c_type, c_val, cand.get("value"))
            vd_key = 1.0 if vd is None else vd
            ns = name_similarity(c_name, cand.get("name") or "")
            return (vd_key, -ns, cand.get("name") or "")

        best = min(candidates, key=key)
        vd = value_distance(c_type, c_val, best.get("value"))
        confidence = "high" if (vd is not None and vd <= 0.02) else "medium"
        return LLMMatchResult(
            outcome="map", baseline_var=best.get("name"), confidence=confidence,
            rationale=(f"Best of {len(candidates)} candidates by value proximity"
                       + (f" (value_distance={vd:.3f})" if vd is not None else " (name-only evidence)") + "."))


class RealLLM:
    """Gated single-pass OpenAIChat matcher (project LiteLLM convention)."""

    name = "real"

    def __init__(self) -> None:
        from os import getenv

        from agno.agent import Agent
        from agno.models.openai import OpenAIChat

        # OpenAIChat (NOT OpenAIResponses) per repo GOTCHA for tool/structured routes.
        self._agent = Agent(
            name="3b-testbench-matcher",
            model=OpenAIChat(id=getenv("OPENAI_MODEL_ID", "gpt-5.4"),
                             base_url=getenv("OPENAI_BASE_URL", None)),
            instructions=[
                "You map a client design token to the single best baseline token, or abstain.",
                "Use the token VALUE to disambiguate when names collide.",
                "If NO candidate is a plausible match for this client token, reply exactly "
                "'UNMAPPABLE' and then a one-line reason. Do NOT force a mapping.",
                "Otherwise reply with ONLY the baseline var name (e.g. --helix-color-brand-primary).",
            ],
        )

    def match(self, client_token: dict, candidates: list[dict], *,
              expected_baseline_var: Optional[str] = None,
              scoring_result=None) -> LLMMatchResult:
        if not candidates:
            return LLMMatchResult(outcome="unmappable", baseline_var=None,
                                  confidence="unresolved", rationale="No candidates.")
        import json

        names = [c.get("name") for c in candidates]
        prompt = (
            "CLIENT TOKEN:\n" + json.dumps(client_token, default=str)
            + "\n\nBASELINE CANDIDATES (name -> value):\n"
            + "\n".join(f"{c.get('name')} -> {json.dumps(c.get('value'), default=str)}"
                        for c in candidates[:40])
            + "\n\nReturn the single best baseline var name, or 'UNMAPPABLE' + reason."
        )
        out = self._agent.run(input=prompt)
        text = (getattr(out, "content", "") or "").strip()
        first = text.splitlines()[0].strip() if text else "UNMAPPABLE"
        if first.upper().startswith("UNMAPPABLE") or first.upper() == "NONE":
            reason = text[len(first):].strip() or "LLM judged no candidate a plausible match."
            return LLMMatchResult(outcome="unmappable", baseline_var=None,
                                  confidence="unresolved", rationale=reason)
        if first not in names:  # hallucinated name -> treat as abstention (safer than wrong map)
            return LLMMatchResult(outcome="unmappable", baseline_var=None, confidence="unresolved",
                                  rationale=f"LLM returned a non-candidate name ({first!r}); treated as abstention.")
        return LLMMatchResult(outcome="map", baseline_var=first, confidence="medium",
                              rationale="LLM selected from candidate list.")


def resolve_llm():
    if os.getenv("HELIX_TESTBENCH_USE_REAL_LLM") == "1":
        return RealLLM()
    return MockLLM()
