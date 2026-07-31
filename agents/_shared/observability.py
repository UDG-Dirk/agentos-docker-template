"""Shared, deterministic observability helpers for HELIX pipeline stations (SP-22).

Extracted verbatim-in-behaviour from 3c's ``agents/theme_generator/reconciliation.py`` so 3c,
3d, and later stations share ONE contract. This is deterministic bookkeeping — cost/invocation
accounting + a threshold anomaly flag + a hard circuit breaker — NOT an LLM. Per the FinOps-honest
principle (Dark Factory KB / Finding 25) the aggregation/alert plane stays deterministic; an LLM
"watcher" is a separate, governance-gated capability (handoff:
shared-results:observability-harness-and-watcher-scoping-candidate-2026-07-31).

SP-9: env-driven; no hardcoded secrets. The circuit-breaker env-var NAMES are parameterised so each
station picks its own (3c: THEME_GEN_*; 3d: COMP_CODE_GEN_*).
"""
from __future__ import annotations

import os
import zlib
from typing import Optional

_DEFAULT_MAX_FINE_GRAINED = 200
_DEFAULT_MAX_COARSE_CHUNKS = 6
_ANOMALY_INVOCATION_MULTIPLIER = 2  # Probe 3: alert when actual > 2x expected-for-count


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


class CircuitBreaker:
    """Hard invocation cap (Probe 3). Trips fail-loud instead of spending silently.

    ``allow_fine_grained`` / ``allow_coarse`` record a slot and return False once the cap is hit;
    callers then flag remaining work for human review. Env-var names are parameterised so each
    station configures its own caps (defaults keep 3c's original THEME_GEN_* behaviour).
    """

    def __init__(self, max_fine_grained: int | None = None, max_coarse_chunks: int | None = None, *,
                 fine_env: str = "THEME_GEN_MAX_FINE_GRAINED",
                 coarse_env: str = "THEME_GEN_MAX_COARSE_CHUNKS") -> None:
        self.max_fine_grained = max_fine_grained if max_fine_grained is not None else _env_int(
            fine_env, _DEFAULT_MAX_FINE_GRAINED)
        self.max_coarse_chunks = max_coarse_chunks if max_coarse_chunks is not None else _env_int(
            coarse_env, _DEFAULT_MAX_COARSE_CHUNKS)
        self.fine_grained_used = 0
        self.coarse_used = 0
        self.tripped = False

    def allow_fine_grained(self) -> bool:
        if self.fine_grained_used >= self.max_fine_grained:
            self.tripped = True
            return False
        self.fine_grained_used += 1
        return True

    def allow_coarse(self) -> bool:
        if self.coarse_used >= self.max_coarse_chunks:
            self.tripped = True
            return False
        self.coarse_used += 1
        return True


def engagement_seed(customer_slug: str, timestamp: str) -> int:
    """Deterministic non-negative seed from (customer, engagement timestamp) — Decision #5.

    Same (customer, timestamp) → same seed → reproducible within an engagement. crc32 keeps it
    stable across processes/machines (no Python hash salt). NOTE: recorded for provenance but NOT
    sent to Anthropic-routed models (they reject `seed`; temperature=0 is the determinism lever —
    3c hardening finding 2026-07-31).
    """
    key = f"{customer_slug or ''}:{timestamp or ''}".encode("utf-8")
    return zlib.crc32(key) & 0x7FFFFFFF


def extract_usage(run_output) -> tuple[int, int]:
    """Best-effort (input_tokens, output_tokens) from an agno RunOutput. Defensive across agno
    versions — reads a ``metrics`` object/dict, summing list-valued per-message counts. Returns
    (0, 0) when metrics aren't exposed. (Verified populated on the 3c hardening live run.)
    """
    m = getattr(run_output, "metrics", None)
    if m is None:
        return 0, 0

    def _get(obj, key):
        val = obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)
        if isinstance(val, (list, tuple)):
            val = sum(x for x in val if isinstance(x, (int, float)))
        return int(val) if isinstance(val, (int, float)) else 0

    return _get(m, "input_tokens"), _get(m, "output_tokens")


def assess_anomaly(expected_fine_grained: int, actual_fine_grained: int,
                   breaker_tripped: bool = False) -> Optional[str]:
    """Return a human-readable anomaly string, or None. Probe 3's PRIMARY control: invocation-ratio,
    not an absolute $ cap. Fires when actual runs > 2x the expected count, or when the breaker tripped.
    """
    if breaker_tripped:
        return "circuit breaker tripped — fine-grained invocation cap hit"
    if expected_fine_grained > 0 and actual_fine_grained > _ANOMALY_INVOCATION_MULTIPLIER * expected_fine_grained:
        return (f"fine-grained invocations {actual_fine_grained} exceeded "
                f"{_ANOMALY_INVOCATION_MULTIPLIER}x expected ({expected_fine_grained})")
    return None
