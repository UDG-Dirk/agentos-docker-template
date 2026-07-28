"""Tests for the account-level 429 bounded-retry guard (task: account-level-429-bounded-retry-and-
escalation). Deterministic + offline: probe, sleep and now are all injected."""
from __future__ import annotations

import asyncio

import httpx
import pytest

import agents.figma_extractor.http_errors as he

DGX = "wjSOPgJLnuDztSDx4OIXSM"


def _probe_seq(*throttled_flags, retry_after=None):
    """Async probe returning ProbeResult per call, following `throttled_flags` in order."""
    calls = {"i": 0}

    async def _p():
        i = calls["i"]
        calls["i"] += 1
        flag = throttled_flags[min(i, len(throttled_flags) - 1)]
        return he.ProbeResult(flag, retry_after if flag else None)
    return _p, calls


def _record_sleep():
    slept: list[float] = []

    async def _s(sec: float):
        slept.append(sec)
    return _s, slept


# ---- happy path: not throttled -> extraction runs immediately, no sleeps ----
def test_no_throttle_runs_extraction_immediately():
    probe, calls = _probe_seq(False)
    sleep, slept = _record_sleep()

    async def run():
        return {"status": "success", "ran": True}
    out = asyncio.run(he.with_account_level_retry(run, probe=probe, sleep=sleep, now=lambda: "T"))
    assert out == {"status": "success", "ran": True}
    assert calls["i"] == 1 and slept == []  # one preflight probe, zero waits


# ---- throttle then clears within window -> extraction runs, bounded sleeps ----
def test_throttle_then_clears_runs_extraction():
    probe, _calls = _probe_seq(True, True, False)  # 2 throttled probes, then clear
    sleep, slept = _record_sleep()

    async def run():
        return {"status": "success"}
    out = asyncio.run(he.with_account_level_retry(run, probe=probe, sleep=sleep, now=lambda: "T"))
    assert out["status"] == "success"
    assert slept == [30.0, 60.0]  # waited before the 2nd and 3rd probe; deterministic, no jitter


# ---- throttle persists whole window -> structured escalation, extraction NEVER runs ----
def test_persistent_throttle_escalates_without_running_extraction():
    probe, _ = _probe_seq(True)  # always throttled
    sleep, slept = _record_sleep()
    ran = {"v": False}

    async def run():
        ran["v"] = True
        return {"status": "success"}
    out = asyncio.run(he.with_account_level_retry(run, probe=probe, sleep=sleep, now=lambda: "T"))
    assert ran["v"] is False  # fail-loud: no extraction, no partial results
    assert out["status"] == "failure" and out["error_class"] == "account_level_rate_limit"
    assert out["extraction_status"] == "throttled" and out["client_should_retry"] is True
    assert out["retries_attempted"] == 3 and out["total_wait_time_seconds"] == 210.0
    assert slept == [30.0, 60.0, 120.0]  # full bounded window, deterministic
    assert out["retry_after_confidence"] == "inferred"  # Figma sends no Retry-After header
    assert out["retry_after_seconds"] == 900 and out["provenance"]["llm_involvement"] == "none"


# ---- throttle scope: reference cross-probe (v2: per_file | pat_wide | undetermined) ----
def test_scope_classifier_per_file_pat_wide_and_error():
    def transport_for(code):
        if code is None:
            return httpx.MockTransport(lambda req: (_ for _ in ()).throw(httpx.ConnectError("down")))
        return httpx.MockTransport(lambda req: httpx.Response(code, json={"s": code}))

    ref_file = "helixCoreRefFile01234"  # a stand-in known-good reference file id (public, not a secret)

    async def go(ref_code, reference=ref_file):
        client = httpx.AsyncClient(transport=transport_for(ref_code))
        try:
            return await he.classify_throttle_scope(DGX, reference_key=reference, client=client)
        finally:
            await client.aclose()
    per_file = asyncio.run(go(200))
    assert per_file["throttle_scope"] == "per_file" and per_file["reference_probe_result"] == "200"
    patwide = asyncio.run(go(429))
    assert patwide["throttle_scope"] == "pat_wide" and patwide["reference_probe_result"] == "429"
    # reference errors (network) OR non-200/429 status -> undetermined, never fabricate
    err = asyncio.run(go(None))
    assert err["throttle_scope"] == "undetermined" and err["reference_probe_result"] == "error"
    other = asyncio.run(go(500))
    assert other["throttle_scope"] == "undetermined" and other["reference_probe_result"] == "error"
    # reference == target -> can't distinguish -> undetermined, no extra probe
    same = asyncio.run(he.classify_throttle_scope(DGX, reference_key=DGX))
    assert same["throttle_scope"] == "undetermined" and same["reference_probe_result"] is None


def test_scope_undetermined_when_no_reference_configured(monkeypatch):
    # env-only reference (no hardcoded default): unset + no reference_key -> undetermined (fail-safe)
    monkeypatch.delenv("FIGMA_RATE_LIMIT_REFERENCE_FILE", raising=False)
    out = asyncio.run(he.classify_throttle_scope(DGX))
    assert out["throttle_scope"] == "undetermined" and out["reference_probe_result"] is None


def test_scope_reference_from_env(monkeypatch):
    # a deployment-configured reference is honored (env FIGMA_RATE_LIMIT_REFERENCE_FILE)
    monkeypatch.setenv("FIGMA_RATE_LIMIT_REFERENCE_FILE", "someRefFileKey123456")
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    try:
        out = asyncio.run(he.classify_throttle_scope(DGX, client=client))
    finally:
        asyncio.run(client.aclose())
    assert out["throttle_scope"] == "per_file" and out["reference_probe_result"] == "200"


def test_escalation_includes_scope_and_affected_file():
    probe, _ = _probe_seq(True)  # always throttled
    sleep, _ = _record_sleep()

    async def run():
        return {"status": "success"}

    async def scope():
        return {"throttle_scope": "per_file", "reference_probe_result": "200"}
    out = asyncio.run(he.with_account_level_retry(
        run, probe=probe, sleep=sleep, now=lambda: "T", scope_classifier=scope, target_key=DGX))
    assert out["error_class"] == "account_level_rate_limit"  # ratified name kept
    assert out["throttle_scope"] == "per_file" and out["reference_probe_result"] == "200"
    assert out["affected_file"] == DGX


def test_escalation_scope_undetermined_without_classifier():
    probe, _ = _probe_seq(True)
    sleep, _ = _record_sleep()

    async def run():
        return {"status": "success"}
    out = asyncio.run(he.with_account_level_retry(run, probe=probe, sleep=sleep, now=lambda: "T"))
    assert out["throttle_scope"] == "undetermined" and out["reference_probe_result"] is None


def test_scope_classifier_exception_yields_undetermined():
    probe, _ = _probe_seq(True)
    sleep, _ = _record_sleep()

    async def run():
        return {"status": "success"}

    async def boom_scope():
        raise RuntimeError("ref probe failed")
    out = asyncio.run(he.with_account_level_retry(
        run, probe=probe, sleep=sleep, now=lambda: "T", scope_classifier=boom_scope))
    assert out["throttle_scope"] == "undetermined"  # classifier failure never breaks escalation


# ---- Retry-After header, when present, is honored over the inferred default ----
def test_retry_after_header_is_honored():
    probe, _ = _probe_seq(True, retry_after=42)
    sleep, _ = _record_sleep()

    async def run():
        return {"status": "success"}
    out = asyncio.run(he.with_account_level_retry(run, probe=probe, sleep=sleep, now=lambda: "T"))
    assert out["retry_after_seconds"] == 42 and out["retry_after_confidence"] == "from_retry_after_header"


# ---- a raising probe must not break the guard -> treated as not-throttled ----
def test_probe_exception_falls_through_to_extraction():
    async def boom():
        raise RuntimeError("network")
    sleep, slept = _record_sleep()

    async def run():
        return {"status": "success", "fell_through": True}
    out = asyncio.run(he.with_account_level_retry(run, probe=boom, sleep=sleep, now=lambda: "T"))
    assert out["fell_through"] is True and slept == []


# ---- recommend_retry_at: real timestamp adds; injected 'T' -> None (determinism) ----
def test_escalation_recommend_retry_at():
    real = he.account_level_escalation(retries_attempted=3, total_wait_seconds=210.0,
                                       now=lambda: "2026-07-28T15:00:00.000Z")
    assert real["recommend_retry_at"] == "2026-07-28T15:15:00.000Z"  # +900s
    fake = he.account_level_escalation(retries_attempted=3, total_wait_seconds=210.0, now=lambda: "T")
    assert fake["recommend_retry_at"] is None


# ---- probe classification: 429 -> throttled; non-429 (incl 403/200) -> not ----
def test_probe_account_throttled_classifies_status():
    def handler_for(code, headers=None):
        def _h(request):
            return httpx.Response(code, headers=headers or {}, json={"status": code})
        return _h

    async def go(code, headers=None):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler_for(code, headers)))
        try:
            return await he.probe_account_throttled(DGX, client=client)
        finally:
            await client.aclose()
    r429 = asyncio.run(go(429, {"retry-after": "120"}))
    assert r429.throttled is True and r429.retry_after == 120
    assert asyncio.run(go(200)).throttled is False   # accessible
    assert asyncio.run(go(403)).throttled is False   # export-lock etc. is NOT account-level 429
    assert asyncio.run(go(404)).throttled is False


def test_probe_network_error_is_not_throttled():
    def _boom(request):
        raise httpx.ConnectError("down")

    async def go():
        client = httpx.AsyncClient(transport=httpx.MockTransport(_boom))
        try:
            return await he.probe_account_throttled(DGX, client=client)
        finally:
            await client.aclose()
    assert asyncio.run(go()).throttled is False


# ---- determinism: identical throttle-clear scenario -> byte-identical result ----
def test_deterministic_across_runs():
    def run_once():
        probe, _ = _probe_seq(True, False)
        sleep, _ = _record_sleep()

        async def run():
            return {"status": "success", "v": 1}
        return asyncio.run(he.with_account_level_retry(run, probe=probe, sleep=sleep, now=lambda: "T"))
    assert run_once() == run_once()


# ---- workflow-executor wiring (offline; guard + probe monkeypatched, no network/sleep) ----
def _skip_if_no_runtime(modpath):
    try:
        import importlib
        return importlib.import_module(modpath)
    except Exception as e:  # noqa: BLE001  # pragma: no cover - env-dependent
        pytest.skip(f"{modpath} needs runtime env: {e!r}")


def test_composition_executor_wires_guard_happy(monkeypatch):
    w = _skip_if_no_runtime("app.workflows.helix_composition_only_extractor")
    from agno.workflow.types import StepInput
    captured = {}

    async def fake_run(fk, *, registered_libraries, file_role):
        captured.update(fk=fk, libs=registered_libraries, role=file_role)
        return {"extraction_mode": "composition-only", "status": "success"}

    async def not_throttled():
        return he.ProbeResult(False)
    monkeypatch.setattr(w, "run_composition_extraction", fake_run)
    monkeypatch.setattr(w, "make_account_probe", lambda fk: not_throttled)  # no network
    out = asyncio.run(w.composition_only_executor(StepInput(input=f"design/{DGX}")))
    assert out.success is True and captured["fk"] == DGX and captured["libs"] == []


def test_composition_executor_propagates_escalation(monkeypatch):
    w = _skip_if_no_runtime("app.workflows.helix_composition_only_extractor")
    from agno.workflow.types import StepInput
    esc = {"status": "failure", "error_class": "account_level_rate_limit", "extraction_status": "throttled"}

    async def fake_guard(run_extraction, *, probe, **kw):
        return esc
    monkeypatch.setattr(w, "with_account_level_retry", fake_guard)
    out = asyncio.run(w.composition_only_executor(StepInput(input=f"design/{DGX}")))
    assert out.success is False and out.content["error_class"] == "account_level_rate_limit"


def test_client_executor_short_circuits_on_escalation(monkeypatch):
    wc = _skip_if_no_runtime("app.workflows.helix_client_extractor")
    from agno.workflow.types import StepInput
    esc = {"status": "failure", "error_class": "account_level_rate_limit", "extraction_status": "throttled"}

    async def fake_guard(run_extraction, *, probe, **kw):
        return esc
    monkeypatch.setattr(wc, "with_account_level_retry", fake_guard)
    out = asyncio.run(wc.client_extract_executor(
        StepInput(input=f"core={DGX} client={DGX}")))
    # flat throttled failure -> no client_extraction wrapper, success False
    assert out.success is False and out.content["error_class"] == "account_level_rate_limit"
    assert "client_extraction" not in out.content
