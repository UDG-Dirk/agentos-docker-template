"""Shared HTTP error classification + account-level rate-limit guard (Figma extractor).

Deterministic, zero-LLM. Two concerns share this module:

  * ``with_account_level_retry`` — bounded auto-retry (30 → 60 → 120 s) on an ACCOUNT-LEVEL 429,
    detected by a bare ``GET /files/{key}?depth=1`` probe, escalating with a structured retry hint
    when the throttle outlasts the bounded window (task: account-level-429-bounded-retry-and-escalation).

  * ``classify_http_error`` — distinct error_class for 403 categories, incl. the "File not exportable"
    content-protection lock (task: file-export-disabled-detection). [added in its own MR]

Account-level 429 (the PAT throttled across ALL endpoints) is a DIFFERENT concern from per-page 429
(a single request throttled). Every lane already recovers per-page 429s with its own bounded backoff
(pathway_b MR !25 `(2,5,12,30)×4`; cache_versioning/semantic_layer/binding_topology `(0.5,2,8)×3`).
This guard sits ABOVE the lanes, at the workflow-orchestration level, so the ~3.5-min account-level
wait happens ONCE per run — not once per lane. Per-lane backoff is untouched.
"""
from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import httpx

_FIGMA_API_BASE = "https://api.figma.com/v1"
_PROBE_TIMEOUT_S = 20.0

# Deterministic, NO jitter (cross-run byte-identical). Total bounded wait = 210 s = 3.5 min, kept
# under a 4-min ceiling so the whole guard + a normal extraction stays inside typical container
# request budgets. See deliverable [U] note on exact Coolify/AGNO worker timeout.
_ACCOUNT_LEVEL_BACKOFF: tuple[float, ...] = (30.0, 60.0, 120.0)
# Figma sends NO Retry-After header on 429 (verified 2026-07-28: body {"status":429,"err":"Rate
# limit exceeded"}, no header). So the hint below is INFERRED, flagged retry_after_confidence.
_DEFAULT_RETRY_AFTER_S = 900  # 15 min — conservative account-level cooldown guess

# Scope cross-probe (HELIX cross-check 2026-07-28): a 429 on the target file can be per-FILE (sticky
# per-(PAT,file) penalty from hammering one heavy file — DGX) OR PAT-wide. On escalation we probe a
# KNOWN-GOOD reference file to tell them apart: reference 200 => per_file; reference 429 => pat_wide.
# The reference file key is DEPLOYMENT config (env FIGMA_RATE_LIMIT_REFERENCE_FILE) — no customer file
# id is hardcoded here. Unset => scope stays 'undetermined' (still fail-safe; the retry hint fires
# regardless). Any known-good, always-accessible file the PAT can read works (e.g. a Core library).
_SCOPE_REFERENCE_ENV = "FIGMA_RATE_LIMIT_REFERENCE_FILE"


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _figma_pat() -> str:
    return os.environ.get("FIGMA_PAT") or os.environ.get("FIGMA_API_KEY") or ""


async def _sleep(seconds: float) -> None:  # indirection so tests stub the (long) account-level wait
    await asyncio.sleep(seconds)


def _plus_seconds_iso(now_str: str, seconds: int) -> str | None:
    """`now_str` + `seconds`, ISO. None if `now_str` isn't a real timestamp (tests inject 'T')."""
    try:
        base = datetime.strptime(now_str, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None
    return (base + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class ProbeResult:
    """Outcome of a bare account-level probe: whether throttled + any Retry-After the server offered."""

    __slots__ = ("retry_after", "throttled")

    def __init__(self, throttled: bool, retry_after: int | None = None) -> None:
        self.throttled = throttled
        self.retry_after = retry_after


async def probe_account_throttled(file_key: str, *, client: httpx.AsyncClient | None = None) -> ProbeResult:
    """Bare ``GET /files/{key}?depth=1``. A 429 here (the cheapest possible call) means the PAT is
    throttled ACCOUNT-WIDE, not just on one heavy endpoint. Any non-429 (incl. 403/404) → not an
    account-level throttle (let the real extraction classify it). Network error → not throttled
    (can't confirm; fall through to the extraction's own fail-loud path)."""
    own = client is None
    if own:
        client = httpx.AsyncClient(headers={"X-Figma-Token": _figma_pat()}, timeout=_PROBE_TIMEOUT_S)
    try:
        resp = await client.get(f"{_FIGMA_API_BASE}/files/{file_key}", params={"depth": 1})
    except httpx.HTTPError:
        return ProbeResult(False)
    finally:
        if own:
            await client.aclose()
    if resp.status_code == 429:
        ra = resp.headers.get("retry-after")
        try:
            ra_int = int(ra) if ra is not None else None
        except ValueError:
            ra_int = None
        return ProbeResult(True, ra_int)
    return ProbeResult(False)


async def classify_throttle_scope(
    target_key: str, *, reference_key: str | None = None, client: httpx.AsyncClient | None = None,
) -> dict:
    """On a target-file 429, probe a known-good REFERENCE file to tell per-file from PAT-wide.
    reference 200 -> 'per_file'; reference 429 -> 'pat_wide'; reference errors / other status, or no
    usable reference (unset / same as target) -> 'undetermined' (never fabricate a scope). Costs ONE
    extra API call, escalation-only. `reference_probe_result` is "200" | "429" | "error"."""
    ref = reference_key or os.environ.get(_SCOPE_REFERENCE_ENV)
    if not ref or ref == target_key:
        return {"throttle_scope": "undetermined", "reference_probe_result": None, "reference_key": None}
    own = client is None
    if own:
        client = httpx.AsyncClient(headers={"X-Figma-Token": _figma_pat()}, timeout=_PROBE_TIMEOUT_S)
    try:
        resp = await client.get(f"{_FIGMA_API_BASE}/files/{ref}", params={"depth": 1})
        code = resp.status_code
    except httpx.HTTPError:
        code = None  # network/transport error
    finally:
        if own:
            await client.aclose()
    if code == 200:
        scope, result = "per_file", "200"        # only the target is penalized
    elif code == 429:
        scope, result = "pat_wide", "429"         # PAT throttled broadly
    else:
        scope, result = "undetermined", "error"   # scope-detection failed — do NOT fabricate
    return {"throttle_scope": scope, "reference_probe_result": result, "reference_key": ref}


def account_level_escalation(
    *, retries_attempted: int, total_wait_seconds: float, now=_now_iso,
    retry_after_seconds: int | None = None, scope_info: dict | None = None,
    affected_file: str | None = None,
) -> dict:
    """Structured, fail-loud escalation when a sustained 429 outlasts the bounded retry window (v2
    contract). NO partial extraction results — the caller returns this instead. `error_class` keeps
    the ratified "account_level_rate_limit" name; `throttle_scope` (per_file | pat_wide | undetermined,
    from the reference cross-probe) surfaces the empirically-correct scope without a contract break."""
    inferred = retry_after_seconds is None
    ra = _DEFAULT_RETRY_AFTER_S if inferred else int(retry_after_seconds)
    ts = now()
    scope_info = scope_info or {"throttle_scope": "undetermined", "reference_probe_result": None}
    return {
        "extraction_status": "throttled",
        "status": "failure",  # fail-loud: this is NOT a success
        "error_class": "account_level_rate_limit",  # ratified contract name (kept, v2 Option 3)
        "http_status": 429,
        "throttle_scope": scope_info.get("throttle_scope", "undetermined"),  # per_file|pat_wide|undetermined
        "affected_file": affected_file,
        "reference_probe_result": scope_info.get("reference_probe_result"),  # "200"|"429"|"error"|None
        "retry_after_seconds": ra,
        # Rule 12 [I]: Figma sends no Retry-After header, so a purely inferred value is flagged.
        "retry_after_confidence": "inferred" if inferred else "from_retry_after_header",
        "recommend_retry_at": _plus_seconds_iso(ts, ra),
        "client_should_retry": True,
        "retries_attempted": retries_attempted,
        "total_wait_time_seconds": round(total_wait_seconds, 3),
        "note": (
            "Figma sustained 429 persists after bounded auto-retry. Pipeline preserved fail-loud "
            "discipline; no partial extraction results returned. See throttle_scope for guidance: "
            "per_file -> retry after wait; pat_wide -> back off broader operations too; undetermined "
            "-> scope-detection failed, treat as pat_wide out of caution. Distinct from per-request "
            "429s (handled per-lane by MR !25 backoff)."
        ),
        "provenance": {"guard": "account_level_retry", "llm_involvement": "none"},
        "extracted_at": ts,
    }


async def with_account_level_retry(
    run_extraction, *, probe, backoff: tuple[float, ...] = _ACCOUNT_LEVEL_BACKOFF,
    sleep=_sleep, now=_now_iso, scope_classifier=None, target_key: str | None = None,
) -> dict:
    """Run ``run_extraction()`` guarded by sustained-429 detection.

    Preflight-probe the target file for a throttle; while throttled, wait a bounded, deterministic
    sequence (30/60/120 s) re-probing between waits; the moment it clears, run the extraction and
    return its result. If the throttle outlasts the window, return a structured escalation hint
    (fail-loud — no partial results). Never raises past this boundary.

    `run_extraction`: zero-arg async returning the extraction dict.
    `probe`: zero-arg async returning a ``ProbeResult`` (True=throttled). A raising probe is treated
    as not-throttled (we can't confirm; fall through to the extraction's own fail-loud).
    `scope_classifier` (optional): zero-arg async returning a scope dict, called ONCE on escalation to
    tell per-file from account-wide (reference cross-probe). A raising classifier -> scope 'unknown'.
    """
    waited = 0.0
    last_retry_after: int | None = None
    for attempt in range(len(backoff) + 1):
        try:
            pr = await probe()
        except Exception:  # noqa: BLE001 — probe must never break the guard
            pr = ProbeResult(False)
        if not pr.throttled:
            return await run_extraction()
        last_retry_after = pr.retry_after if pr.retry_after is not None else last_retry_after
        if attempt == len(backoff):
            break  # window exhausted, still throttled -> escalate
        await sleep(backoff[attempt])
        waited += backoff[attempt]
    scope_info = None
    if scope_classifier is not None:
        try:
            scope_info = await scope_classifier()
        except Exception:  # noqa: BLE001 — scope cross-probe must never break the escalation
            scope_info = None
    return account_level_escalation(
        retries_attempted=len(backoff), total_wait_seconds=waited, now=now,
        retry_after_seconds=last_retry_after, scope_info=scope_info, affected_file=target_key,
    )


def make_account_probe(file_key: str):
    """Zero-arg async probe closure over `file_key` (each call opens/closes a short-lived client)."""
    async def _probe() -> ProbeResult:
        return await probe_account_throttled(file_key)
    return _probe


def make_scope_classifier(target_key: str, *, reference_key: str | None = None):
    """Zero-arg async closure that cross-probes a known-good reference file to classify throttle scope."""
    async def _classify() -> dict:
        return await classify_throttle_scope(target_key, reference_key=reference_key)
    return _classify
