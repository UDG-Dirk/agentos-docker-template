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
import logging
import os
import time
from datetime import UTC, datetime, timedelta

import httpx

_log = logging.getLogger("figma_extractor.http_errors")

_FIGMA_API_BASE = "https://api.figma.com/v1"
_PROBE_TIMEOUT_S = 20.0
# Heartbeat cadence during the bounded wait (v3.1): emit at least this often so a foreground SSE
# connection never idles out (Traefik default idle ~180s) and background runs stay observable.
_HEARTBEAT_INTERVAL_S = 40.0

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

# Cross-probe frequency cap (v3 FM-1): a global sliding-window (1 h) limiter so that when many
# extractions hit sustained-429 at once, their scope cross-probes don't multiply API calls at exactly
# the wrong moment (which could escalate per-file throttles into a PAT-wide one). Over the cap →
# skip the cross-probe, report scope 'undetermined' + undetermined_reason 'cross_probe_cap_exceeded'.
_CROSS_PROBE_MAX_ENV = "FIGMA_RATE_LIMIT_CROSS_PROBE_MAX_PER_HOUR"
_DEFAULT_CROSS_PROBE_MAX_PER_HOUR = 10
_cross_probe_times: list[float] = []  # module-global window (workflow-process lifetime)


def _cross_probe_max() -> int:
    try:
        return int(os.environ.get(_CROSS_PROBE_MAX_ENV, _DEFAULT_CROSS_PROBE_MAX_PER_HOUR))
    except (TypeError, ValueError):
        return _DEFAULT_CROSS_PROBE_MAX_PER_HOUR


def _cross_probe_allow(now_ts: float | None = None) -> bool:
    """Global 1-h sliding-window gate (FM-1). Records a slot when it allows. Deterministic under an
    injected `now_ts`; wall-clock otherwise."""
    now = time.time() if now_ts is None else now_ts
    cutoff = now - 3600.0
    while _cross_probe_times and _cross_probe_times[0] < cutoff:
        _cross_probe_times.pop(0)
    if len(_cross_probe_times) >= _cross_probe_max():
        return False
    _cross_probe_times.append(now)
    return True


def _reset_cross_probe_window() -> None:  # test helper
    _cross_probe_times.clear()


def check_rate_limit_reference_config() -> bool:
    """Phase 0 (v3): loud startup warning if the scope-reference env is unset. Returns True if set.
    No customer file key is hardcoded — the reference is deployment config (SP-9 env-only substrate)."""
    if os.environ.get(_SCOPE_REFERENCE_ENV):
        _log.info("%s configured; sustained-429 scope classification enabled.", _SCOPE_REFERENCE_ENV)
        return True
    _log.warning(
        "%s env var not set. Scope classification (per-file vs PAT-wide) is DISABLED. All "
        "sustained-429 escalations will report throttle_scope='undetermined' with "
        "undetermined_reason='reference_env_unset', regardless of actual scope. Set %s="
        "<known-good-figma-file-key> in .env or the Coolify environment to enable classification. "
        "Recommended: a stable Core library file key.",
        _SCOPE_REFERENCE_ENV, _SCOPE_REFERENCE_ENV,
    )
    return False


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
    cross_probe_gate=None, now_ts: float | None = None,
) -> dict:
    """On a target-file 429, probe a known-good REFERENCE file (env `FIGMA_RATE_LIMIT_REFERENCE_FILE`,
    no hardcoded fallback) to tell per-file from PAT-wide. Returns v3 fields: `throttle_scope`
    (per_file|pat_wide|undetermined), `undetermined_reason` (reference_env_unset | cross_probe_cap_exceeded
    | reference_probe_error | None), `reference_probe_result` ("200"|"429"|"error"|"skipped"),
    `cross_probe_attempted` (bool). Never fabricates a scope. Cross-probe gated by the FM-1 cap.
    `cross_probe_gate`: zero-arg bool (test injection); default = module 1-h sliding-window limiter."""
    ref = reference_key or os.environ.get(_SCOPE_REFERENCE_ENV)
    if not ref:  # Phase 1b: env unset -> scope disabled (fail-safe), no probe, no cap consumption
        return {"throttle_scope": "undetermined", "undetermined_reason": "reference_env_unset",
                "reference_probe_result": "skipped", "reference_key": None, "cross_probe_attempted": False}
    if ref == target_key:  # misconfigured reference == target: can't distinguish
        return {"throttle_scope": "undetermined", "undetermined_reason": "reference_probe_error",
                "reference_probe_result": "skipped", "reference_key": ref, "cross_probe_attempted": False}
    allowed = cross_probe_gate() if cross_probe_gate is not None else _cross_probe_allow(now_ts)
    if not allowed:  # Phase 1c FM-1: over the global cross-probe cap -> skip, don't amplify throttling
        return {"throttle_scope": "undetermined", "undetermined_reason": "cross_probe_cap_exceeded",
                "reference_probe_result": "skipped", "reference_key": ref, "cross_probe_attempted": False}
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
        scope, result, reason = "per_file", "200", None       # only the target is penalized
    elif code == 429:
        scope, result, reason = "pat_wide", "429", None        # PAT throttled broadly
    else:
        scope, result, reason = "undetermined", "error", "reference_probe_error"  # do NOT fabricate
    return {"throttle_scope": scope, "undetermined_reason": reason, "reference_probe_result": result,
            "reference_key": ref, "cross_probe_attempted": True}


def account_level_escalation(
    *, retries_attempted: int, total_wait_seconds: float, now=_now_iso,
    retry_after_seconds: int | None = None, scope_info: dict | None = None,
    affected_file: str | None = None, heartbeats_emitted: int = 0, metrics: dict | None = None,
) -> dict:
    """Structured, fail-loud escalation when a sustained 429 outlasts the bounded retry window (v3.1
    contract). NO partial extraction results — the caller returns this instead. `error_class` keeps
    the ratified "account_level_rate_limit" name; `throttle_scope` + `undetermined_reason` (from the
    reference cross-probe) surface the empirically-correct scope without a contract break."""
    inferred = retry_after_seconds is None
    ra = _DEFAULT_RETRY_AFTER_S if inferred else int(retry_after_seconds)
    ts = now()
    scope_info = scope_info or {"throttle_scope": "undetermined",
                                "undetermined_reason": "reference_env_unset",
                                "reference_probe_result": "skipped", "cross_probe_attempted": False}
    return {
        "extraction_status": "throttled",
        "status": "failure",  # fail-loud: this is NOT a success
        "error_class": "account_level_rate_limit",  # ratified contract name (kept, v2 Option 3)
        "http_status": 429,
        "throttle_scope": scope_info.get("throttle_scope", "undetermined"),  # per_file|pat_wide|undetermined
        "undetermined_reason": scope_info.get("undetermined_reason"),  # env_unset|probe_error|cap_exceeded|None
        "affected_file": affected_file,
        "reference_probe_result": scope_info.get("reference_probe_result"),  # "200"|"429"|"error"|"skipped"
        "cross_probe_attempted": bool(scope_info.get("cross_probe_attempted", False)),
        "retry_after_seconds": ra,
        # Rule 12 [I]: Figma sends no Retry-After header, so a purely inferred value is flagged.
        "retry_after_confidence": "inferred" if inferred else "from_retry_after_header",
        "recommend_retry_at": _plus_seconds_iso(ts, ra),
        "client_should_retry": True,
        "retries_attempted": retries_attempted,
        "total_wait_time_seconds": round(total_wait_seconds, 3),
        "heartbeats_emitted": heartbeats_emitted,
        "metrics": metrics or {},  # MO-1/MO-4 observability (structure deterministic, values per-run)
        "note": (
            "Figma sustained 429 persists after bounded auto-retry. Pipeline preserved fail-loud "
            "discipline; no partial extraction results returned. See throttle_scope + undetermined_reason "
            "for guidance: per_file -> retry after wait; pat_wide -> back off broader operations too; "
            "undetermined -> scope-detection unavailable (reason given), treat as pat_wide out of caution. "
            "Distinct from per-request 429s (handled per-lane by MR !25 backoff)."
        ),
        "provenance": {"guard": "account_level_retry", "llm_involvement": "none"},
        "extracted_at": ts,
    }


async def _wait_with_heartbeats(
    sleep, total_s: float, interval: float, emit, *, retry_attempt: int, elapsed_before_ms: int,
    target_key: str | None, now,
) -> int:
    """Sleep `total_s` in ≤`interval` chunks, emitting a `sustained_429_retry_heartbeat` (INFO) before
    each chunk so a foreground SSE never idles out and background runs stay observable. `elapsed_wait_ms`
    is computed from PLANNED waits (deterministic); only `next_probe_at` (a timestamp) varies per run.
    Returns the number of heartbeats emitted."""
    remaining = total_s
    elapsed = 0.0
    emitted = 0
    while remaining > 0:
        # count the heartbeat regardless of a wired sink (cadence marker → deterministic count);
        # `emit` only forwards it onward (SSE / observability).
        emitted += 1
        if emit is not None:
            emit({
                "event_type": "sustained_429_retry_heartbeat",
                "retry_attempt": retry_attempt,
                "elapsed_wait_ms": elapsed_before_ms + int(elapsed * 1000),
                "next_probe_at": _plus_seconds_iso(now(), int(remaining)),
                "workflow_status": "throttled_waiting",
                "affected_file": target_key,
                "severity": "info",
            })
        chunk = min(interval, remaining)
        await sleep(chunk)
        elapsed += chunk
        remaining -= chunk
    return emitted


async def with_account_level_retry(
    run_extraction, *, probe, backoff: tuple[float, ...] = _ACCOUNT_LEVEL_BACKOFF,
    sleep=_sleep, now=_now_iso, scope_classifier=None, target_key: str | None = None,
    emit=None, heartbeat_interval: float = _HEARTBEAT_INTERVAL_S,
) -> dict:
    """Run ``run_extraction()`` guarded by sustained-429 detection (v3.1).

    Preflight-probe the target file for a throttle; while throttled, wait a bounded, deterministic
    sequence (30/60/120 s) re-probing between waits and emitting periodic heartbeats; the moment it
    clears, run the extraction and return its result. If the throttle outlasts the window, return a
    structured escalation hint (fail-loud — no partial results). Never raises past this boundary.

    `run_extraction`: zero-arg async returning the extraction dict.
    `probe`: zero-arg async returning a ``ProbeResult`` (True=throttled). A raising probe is treated
    as not-throttled (we can't confirm; fall through to the extraction's own fail-loud).
    `scope_classifier` (optional): zero-arg async returning a scope dict, called ONCE on escalation.
    `emit` (optional): per-heartbeat callback (SSE forwarding / observability). Bounded by AGNO's
    step-event model for live SSE, but always present + retrievable in the escalation output.
    """
    waited = 0.0
    last_retry_after: int | None = None
    heartbeats = 0
    sustained_encounters = 0
    retry_decisions: list[dict] = []
    for attempt in range(len(backoff) + 1):
        try:
            pr = await probe()
        except Exception:  # noqa: BLE001 — probe must never break the guard
            pr = ProbeResult(False)
        if not pr.throttled:
            return await run_extraction()  # not throttled / cleared after retry -> extraction resumes
        sustained_encounters += 1
        last_retry_after = pr.retry_after if pr.retry_after is not None else last_retry_after
        if attempt == len(backoff):
            retry_decisions.append({"event": "escalate", "attempt": attempt, "reason": "window_exhausted"})
            break  # window exhausted, still throttled -> escalate
        wait_s = backoff[attempt]
        retry_decisions.append({"event": "backoff", "attempt": attempt + 1, "planned_wait_s": wait_s})
        heartbeats += await _wait_with_heartbeats(
            sleep, wait_s, heartbeat_interval, emit, retry_attempt=attempt + 1,
            elapsed_before_ms=int(waited * 1000), target_key=target_key, now=now)
        waited += wait_s
    scope_info = None
    if scope_classifier is not None:
        try:
            scope_info = await scope_classifier()
        except Exception:  # noqa: BLE001 — scope cross-probe must never break the escalation
            scope_info = {"throttle_scope": "undetermined", "undetermined_reason": "reference_probe_error",
                          "reference_probe_result": "error", "cross_probe_attempted": True}
    metrics = {
        "sustained_429_encounters": sustained_encounters,
        "cross_probe_attempts": 1 if (scope_info and scope_info.get("cross_probe_attempted")) else 0,
        "total_backoff_wait_ms": int(waited * 1000),
        "heartbeats_emitted": heartbeats,
        "retry_decisions": retry_decisions,
    }
    return account_level_escalation(
        retries_attempted=len(backoff), total_wait_seconds=waited, now=now,
        retry_after_seconds=last_retry_after, scope_info=scope_info, affected_file=target_key,
        heartbeats_emitted=heartbeats, metrics=metrics,
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


# ===========================================================================================
# 403 classification — "File not exportable" content-protection lock (task: file-export-disabled)
# ===========================================================================================
# B-S 2026-07-28: a published library can be REST-body-export-LOCKED — /files & /nodes return
# 403 "File not exportable" while Lane 2 (/components,/component_sets,/styles) stays 200. Distinct
# from an Enterprise-scope 403 ("Invalid scope") and from an auth-failure 403 (bad PAT). Detected by
# a STABLE substring on the 403 body; other 403s keep their existing handling untouched.
_EXPORT_LOCK_MARKER = "not exportable"      # "File not exportable" (case-insensitive substring)
_ENTERPRISE_SCOPE_MARKER = "invalid scope"  # Enterprise scope gate


def classify_forbidden(body_text: str | None) -> str:
    """Sub-classify a 403 body: 'file_export_disabled' | 'enterprise_scope' | 'forbidden'.
    Deterministic, case-insensitive substring match on stable Figma markers (zero-LLM)."""
    t = (body_text or "").lower()
    if _EXPORT_LOCK_MARKER in t:
        return "file_export_disabled"
    if _ENTERPRISE_SCOPE_MARKER in t:
        return "enterprise_scope"
    return "forbidden"


def classify_http_error(status_code: int, body_text: str | None = "") -> str:
    """Distinct error_class for an HTTP error status (403 → forbidden sub-categories). Deterministic."""
    if status_code == 403:
        return classify_forbidden(body_text)
    if status_code == 401:
        return "auth_failure"
    if status_code == 429:
        return "rate_limit"
    if status_code == 404:
        return "not_found"
    if 500 <= status_code < 600:
        return "server_error"
    return "client_error"


def file_export_disabled_report(*, affected_lane: str, figma_message: str = "File not exportable") -> dict:
    """Structured, actionable guidance for a 'File not exportable' 403. Operators see immediately that
    it's a client-side file setting (owner must unlock export), NOT a pipeline/auth bug."""
    return {
        "error_class": "file_export_disabled",
        "http_status": 403,
        "error_message_from_figma": figma_message,
        "explanation": (
            "The file has a content-protection setting enabled: REST body/node export is blocked "
            "while published-library metadata endpoints (Lane 2) remain accessible."
        ),
        "action_required": (
            "File owner must disable content-protection (Figma: file → Share → turn OFF "
            "'Disable copying/exporting of this file'), or grant export-enabled access."
        ),
        "affected_lane": affected_lane,
        "accessible_lanes": "Lane 2 (published-library metadata) if the file publishes anything",
        "unblock_channel": "Cross-team ping to the file owner (client-side setting, not a pipeline fix)",
        "provenance": {"llm_involvement": "none"},
    }


class FileExportDisabledError(Exception):
    """Raised in place of a bare 403 when the body is the 'File not exportable' content-protection lock,
    so raise-based lanes (Pathway B, Lane 7) can surface a distinct `file_export_disabled` failure."""

    def __init__(self, figma_message: str = "File not exportable") -> None:
        super().__init__(f"file_export_disabled: {figma_message}")
        self.figma_message = figma_message


def raise_for_figma_status(resp: httpx.Response) -> None:
    """Like ``resp.raise_for_status()`` but raises ``FileExportDisabledError`` for the export-lock 403
    (stable marker) so callers classify it distinctly. All other statuses raise as usual (unchanged)."""
    if resp.status_code == 403 and _EXPORT_LOCK_MARKER in (resp.text or "").lower():
        raise FileExportDisabledError(_extract_figma_err(resp.text) or "File not exportable")
    resp.raise_for_status()


def _extract_figma_err(body_text: str | None) -> str | None:
    """Best-effort pull of Figma's ``err`` message from a JSON error body; None if absent/unparseable."""
    import json
    try:
        v = json.loads(body_text or "")
    except (ValueError, TypeError):
        return None
    return v.get("err") if isinstance(v, dict) and isinstance(v.get("err"), str) else None

# Phase 0 (v3): emit the config warning once at module import — the app imports this at boot via the
# workflow modules, so an unset reference is loud in Coolify startup logs.
check_rate_limit_reference_config()
