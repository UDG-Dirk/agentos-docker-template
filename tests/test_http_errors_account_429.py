"""Tests for the sustained-429 bounded-retry guard v3.1 (task: account-level-429-bounded-retry-and-
escalation). Deterministic + offline: probe, sleep, now, and the cross-probe gate are all injected."""
from __future__ import annotations

import asyncio

import httpx
import pytest

import agents.figma_extractor.http_errors as he

DGX = "wjSOPgJLnuDztSDx4OIXSM"
_ALLOW = lambda: True  # cross-probe gate that always allows (no module window in tests)
_REF_FILE = "refFile01234567890abc"  # stand-in reference file id (public, not a secret)


@pytest.fixture(autouse=True)
def _reset_window():
    he._reset_cross_probe_window()
    yield
    he._reset_cross_probe_window()


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


# ---- throttle then clears within window -> extraction runs (sleeps chunked for heartbeats) ----
def test_throttle_then_clears_runs_extraction():
    probe, _calls = _probe_seq(True, True, False)  # 2 throttled probes, then clear
    sleep, slept = _record_sleep()

    async def run():
        return {"status": "success"}
    out = asyncio.run(he.with_account_level_retry(run, probe=probe, sleep=sleep, now=lambda: "T"))
    assert out["status"] == "success"
    # waits 30 + 60 = 90s total, chunked into <=40s segments for heartbeats: [30] + [40,20]
    assert slept == [30.0, 40.0, 20.0] and sum(slept) == 90.0


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
    assert sum(slept) == 210.0  # full bounded window (30+60+120), chunked
    assert out["retry_after_confidence"] == "inferred"  # Figma sends no Retry-After header
    assert out["retry_after_seconds"] == 900 and out["provenance"]["llm_involvement"] == "none"
    # no scope_classifier -> escalation default reason
    assert out["throttle_scope"] == "undetermined" and out["undetermined_reason"] == "reference_env_unset"
    # heartbeats: 30->1, 60->2, 120->3 = 6 (40s cadence), counted deterministically
    assert out["heartbeats_emitted"] == 6 and out["metrics"]["heartbeats_emitted"] == 6


# ---- throttle scope: reference cross-probe (per_file | pat_wide | undetermined) ----
def test_scope_classifier_per_file_pat_wide_and_error():
    def transport_for(code):
        if code is None:
            return httpx.MockTransport(lambda req: (_ for _ in ()).throw(httpx.ConnectError("down")))
        return httpx.MockTransport(lambda req: httpx.Response(code, json={"s": code}))

    ref_file = "helixCoreRefFile01234"  # a stand-in known-good reference file id (public, not a secret)

    async def go(ref_code, reference=ref_file):
        client = httpx.AsyncClient(transport=transport_for(ref_code))
        try:
            return await he.classify_throttle_scope(DGX, reference_key=reference, client=client,
                                                    cross_probe_gate=_ALLOW)
        finally:
            await client.aclose()
    per_file = asyncio.run(go(200))
    assert per_file["throttle_scope"] == "per_file" and per_file["reference_probe_result"] == "200"
    assert per_file["cross_probe_attempted"] is True and per_file["undetermined_reason"] is None
    patwide = asyncio.run(go(429))
    assert patwide["throttle_scope"] == "pat_wide" and patwide["reference_probe_result"] == "429"
    # reference errors (network) OR non-200/429 status -> undetermined + reference_probe_error
    err = asyncio.run(go(None))
    assert err["throttle_scope"] == "undetermined" and err["reference_probe_result"] == "error"
    assert err["undetermined_reason"] == "reference_probe_error"
    other = asyncio.run(go(500))
    assert other["undetermined_reason"] == "reference_probe_error"
    # reference == target -> can't distinguish -> undetermined + skipped, no probe
    same = asyncio.run(he.classify_throttle_scope(DGX, reference_key=DGX, cross_probe_gate=_ALLOW))
    assert same["throttle_scope"] == "undetermined" and same["reference_probe_result"] == "skipped"
    assert same["undetermined_reason"] == "reference_probe_error" and same["cross_probe_attempted"] is False


def test_scope_undetermined_when_no_reference_configured(monkeypatch):
    # env-only reference (no hardcoded default): unset + no reference_key -> undetermined (fail-safe)
    monkeypatch.delenv("FIGMA_RATE_LIMIT_REFERENCE_FILE", raising=False)
    out = asyncio.run(he.classify_throttle_scope(DGX, cross_probe_gate=_ALLOW))
    assert out["throttle_scope"] == "undetermined" and out["reference_probe_result"] == "skipped"
    assert out["undetermined_reason"] == "reference_env_unset" and out["cross_probe_attempted"] is False


def test_scope_reference_from_env(monkeypatch):
    # a deployment-configured reference is honored (env FIGMA_RATE_LIMIT_REFERENCE_FILE)
    monkeypatch.setenv("FIGMA_RATE_LIMIT_REFERENCE_FILE", "someRefFile01234567x")
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    try:
        out = asyncio.run(he.classify_throttle_scope(DGX, client=client, cross_probe_gate=_ALLOW))
    finally:
        asyncio.run(client.aclose())
    assert out["throttle_scope"] == "per_file" and out["cross_probe_attempted"] is True


def test_scope_cross_probe_cap_exceeded():
    # gate denies (cap hit) -> skip probe, undetermined + cross_probe_cap_exceeded
    out = asyncio.run(he.classify_throttle_scope(DGX, reference_key=_REF_FILE,
                                                 cross_probe_gate=lambda: False))
    assert out["throttle_scope"] == "undetermined" and out["undetermined_reason"] == "cross_probe_cap_exceeded"
    assert out["reference_probe_result"] == "skipped" and out["cross_probe_attempted"] is False


# ---- FM-1 sliding-window cross-probe limiter ----
def test_cross_probe_sliding_window_cap(monkeypatch):
    monkeypatch.delenv("FIGMA_RATE_LIMIT_CROSS_PROBE_MAX_PER_HOUR", raising=False)  # default 10
    he._reset_cross_probe_window()
    t = 1000.0
    allowed = [he._cross_probe_allow(now_ts=t + i) for i in range(10)]  # 10 within the hour
    assert all(allowed)
    assert he._cross_probe_allow(now_ts=t + 10) is False  # 11th -> capped
    # slide the window past 1h -> allowed again
    assert he._cross_probe_allow(now_ts=t + 3601) is True


def test_cross_probe_cap_env_override(monkeypatch):
    monkeypatch.setenv("FIGMA_RATE_LIMIT_CROSS_PROBE_MAX_PER_HOUR", "2")
    he._reset_cross_probe_window()
    assert he._cross_probe_allow(now_ts=1.0) is True
    assert he._cross_probe_allow(now_ts=2.0) is True
    assert he._cross_probe_allow(now_ts=3.0) is False  # cap=2


# ---- heartbeat emission: shape, count, determinism ----
def test_heartbeats_emitted_shape_and_count():
    probe, _ = _probe_seq(True)  # always throttled -> full window
    sleep, _ = _record_sleep()
    seen: list[dict] = []

    async def run():
        return {"status": "success"}
    out = asyncio.run(he.with_account_level_retry(
        run, probe=probe, sleep=sleep, now=lambda: "T", target_key=DGX, emit=seen.append))
    assert len(seen) == 6 and out["heartbeats_emitted"] == 6  # 1+2+3
    hb = seen[0]
    assert hb["event_type"] == "sustained_429_retry_heartbeat" and hb["severity"] == "info"
    assert hb["retry_attempt"] == 1 and hb["workflow_status"] == "throttled_waiting"
    assert hb["affected_file"] == DGX and hb["elapsed_wait_ms"] == 0
    # retry_attempt sequence across the three waits: [1], [2,2], [3,3,3]
    assert [e["retry_attempt"] for e in seen] == [1, 2, 2, 3, 3, 3]


def test_heartbeat_determinism_modulo_timestamps():
    def run_once():
        probe, _ = _probe_seq(True)
        sleep, _ = _record_sleep()
        seen: list[dict] = []

        async def run():
            return {"status": "success"}
        asyncio.run(he.with_account_level_retry(
            run, probe=probe, sleep=sleep, now=lambda: "T", target_key=DGX, emit=seen.append))
        return seen
    # now=lambda:"T" -> next_probe_at is None (unparseable) -> events fully byte-identical cross-run
    assert run_once() == run_once()


# ---- escalation v3.1 fields ----
def test_escalation_includes_scope_reason_and_metrics():
    probe, _ = _probe_seq(True)  # always throttled
    sleep, _ = _record_sleep()

    async def run():
        return {"status": "success"}

    async def scope():
        return {"throttle_scope": "per_file", "undetermined_reason": None,
                "reference_probe_result": "200", "cross_probe_attempted": True}
    out = asyncio.run(he.with_account_level_retry(
        run, probe=probe, sleep=sleep, now=lambda: "T", scope_classifier=scope, target_key=DGX))
    assert out["error_class"] == "account_level_rate_limit"  # ratified name kept
    assert out["throttle_scope"] == "per_file" and out["reference_probe_result"] == "200"
    assert out["affected_file"] == DGX and out["cross_probe_attempted"] is True
    m = out["metrics"]
    assert m["sustained_429_encounters"] == 4 and m["cross_probe_attempts"] == 1
    assert m["total_backoff_wait_ms"] == 210000 and m["heartbeats_emitted"] == 6
    assert isinstance(m["retry_decisions"], list) and m["retry_decisions"][-1]["event"] == "escalate"


def test_escalation_scope_undetermined_without_classifier():
    probe, _ = _probe_seq(True)
    sleep, _ = _record_sleep()

    async def run():
        return {"status": "success"}
    out = asyncio.run(he.with_account_level_retry(run, probe=probe, sleep=sleep, now=lambda: "T"))
    assert out["throttle_scope"] == "undetermined" and out["reference_probe_result"] == "skipped"
    assert out["undetermined_reason"] == "reference_env_unset"


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
    assert out["undetermined_reason"] == "reference_probe_error"


# ---- Phase 0 startup config warning ----
def test_startup_warning_when_unset(monkeypatch, caplog):
    monkeypatch.delenv("FIGMA_RATE_LIMIT_REFERENCE_FILE", raising=False)
    import logging
    with caplog.at_level(logging.WARNING, logger="figma_extractor.http_errors"):
        ok = he.check_rate_limit_reference_config()
    assert ok is False and any("not set" in r.message for r in caplog.records)


def test_startup_ok_when_set(monkeypatch):
    monkeypatch.setenv("FIGMA_RATE_LIMIT_REFERENCE_FILE", "someRefFile01234567x")
    assert he.check_rate_limit_reference_config() is True


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


# ---- MO-2 cross-run determinism regression (escalation path, modulo timestamps) ----
def test_escalation_deterministic_across_runs():
    def run_once():
        probe, _ = _probe_seq(True)
        sleep, _ = _record_sleep()

        async def run():
            return {"status": "success"}

        async def scope():
            return {"throttle_scope": "pat_wide", "undetermined_reason": None,
                    "reference_probe_result": "429", "cross_probe_attempted": True}
        return asyncio.run(he.with_account_level_retry(
            run, probe=probe, sleep=sleep, now=lambda: "T", scope_classifier=scope, target_key=DGX))
    # now="T" pins extracted_at + recommend_retry_at(None) -> byte-identical incl. metrics structure
    assert run_once() == run_once()


def test_clear_after_retry_deterministic():
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
