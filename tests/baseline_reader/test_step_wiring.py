"""Cycle 2 wiring tests for Agent 3a's Workflow Step wrapper.

Covers the deterministic, offline-verifiable parts of the pull-on-invocation
wiring: ref resolution, credential-safety of the git auth, the real
clone/fetch/checkout flow (against a local file:// repo, no network/creds), the
non-adjacent-access smoke test, and the Workflow topology. The full parallel
run against Figma MCP + models is PROD-only (Stage 12).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agents.baseline_reader import step as bstep
from agents.baseline_reader.step import (
    STEP_NAME_BASELINE,
    STEP_NAME_SMOKE,
    _credential_helper,
    _resolve_ref,
    _scrub,
    baseline_access_smoke_test_executor,
    ensure_baseline_checkout,
)


class _FakeStepInput:
    """Minimal StepInput stand-in for executor unit tests."""

    def __init__(self, additional_data=None, step_outputs=None, step_contents=None):
        self.additional_data = additional_data or {}
        self._step_outputs = step_outputs or {}
        self._step_contents = step_contents or {}

    def get_step_output(self, name):
        return self._step_outputs.get(name)

    def get_step_content(self, name):
        return self._step_contents.get(name)


class _FakeStepOutput:
    def __init__(self, content):
        self.content = content


# --- ref resolution ---------------------------------------------------------
def test_resolve_ref_defaults_to_master():
    # helix-code's default branch is master; main does not exist on the remote.
    assert _resolve_ref(_FakeStepInput()) == "master"
    assert _resolve_ref(_FakeStepInput(additional_data={"baseline_ref": "  "})) == "master"


def test_resolve_ref_honours_override():
    si = _FakeStepInput(additional_data={"baseline_ref": "feature/x"})
    assert _resolve_ref(si) == "feature/x"


# --- credential safety (Container Hosting Security Baseline, Rule 5) ---------
def test_credential_helper_contains_no_secret(monkeypatch):
    monkeypatch.setenv("BASELINE_REPO_USERNAME", "deploy-user")
    monkeypatch.setenv("BASELINE_REPO_TOKEN", "super-secret-token")
    helper = _credential_helper()
    assert "super-secret-token" not in helper
    assert "deploy-user" not in helper
    # references the env var NAMES so git reads them from the environment
    assert "BASELINE_REPO_USERNAME" in helper
    assert "BASELINE_REPO_TOKEN" in helper


def test_scrub_redacts_token(monkeypatch):
    monkeypatch.setenv("BASELINE_REPO_TOKEN", "glpat-DEADBEEF")
    assert "glpat-DEADBEEF" not in (_scrub("failed for glpat-DEADBEEF") or "")


# --- real clone/fetch/checkout against a local file:// repo (offline) -------
@pytest.fixture()
def source_repo(tmp_path):
    """A real git repo with a commit on 'main', served over file://."""
    src = tmp_path / "origin"
    src.mkdir()
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(tmp_path)}

    def g(*args):
        subprocess.run(["git", *args], cwd=src, env=env, check=True,
                       capture_output=True, text=True)

    g("init", "-b", "main")
    g("config", "user.email", "t@t.t")
    g("config", "user.name", "t")
    (src / "packages").mkdir()
    (src / "marker.txt").write_text("baseline-content\n")
    g("add", "-A")
    g("commit", "-m", "baseline")
    return src, f"file://{src}", env


def test_ensure_checkout_clones_then_fetches_without_persisting_creds(source_repo, tmp_path):
    src, url, env = source_repo
    target = tmp_path / "baseline"

    # first call: empty target -> clone + fetch + checkout
    dur1, err1 = ensure_baseline_checkout(str(target), "main", clean_url=url, env=env)
    assert err1 is None, err1
    assert (target / ".git").exists()
    assert (target / "marker.txt").read_text() == "baseline-content\n"
    assert dur1 >= 0.0

    # remote URL persisted in .git/config must be credential-free (== clean url)
    cfg = subprocess.run(
        ["git", "-C", str(target), "remote", "get-url", "origin"],
        env=env, capture_output=True, text=True,
    ).stdout.strip()
    assert cfg == url
    assert "@" not in cfg.split("//", 1)[-1]  # no user:token@host

    # second call: existing repo -> fetch + checkout path
    dur2, err2 = ensure_baseline_checkout(str(target), "main", clean_url=url, env=env)
    assert err2 is None, err2


def test_ensure_checkout_reports_error_on_bad_ref(source_repo, tmp_path):
    src, url, env = source_repo
    target = tmp_path / "baseline"
    _, err = ensure_baseline_checkout(str(target), "no-such-ref", clean_url=url, env=env)
    assert err is not None


# --- non-adjacent access smoke test -----------------------------------------
def test_smoke_test_proves_non_adjacent_access():
    inventory = {"meta": {"baseline_ref": "main", "baseline_repo_path": "/x"},
                 "tokens": {}, "components": {}, "blocking_warnings": []}
    si = _FakeStepInput(
        step_outputs={STEP_NAME_BASELINE: _FakeStepOutput(inventory)},
        step_contents={"normalize": {"normalized": True}},
    )
    out = baseline_access_smoke_test_executor(si)
    summary = out.content["baseline_access_smoke"]
    assert summary["access_proven"] is True
    assert summary["shape_ok"] is True
    assert out.success is True
    # normalized content passed through
    assert out.content["normalized"] == {"normalized": True}


def test_smoke_test_flags_missing_baseline_output():
    si = _FakeStepInput(step_outputs={}, step_contents={})
    out = baseline_access_smoke_test_executor(si)
    assert out.content["baseline_access_smoke"]["access_proven"] is False
    assert out.success is False


# --- workflow topology ------------------------------------------------------
def test_workflow_has_parallel_then_smoke_topology():
    from app.workflows.helix_figma_extractor import helix_figma_extractor_workflow as wf

    assert wf.id == "helix-figma-extractor"
    steps = wf.steps
    assert len(steps) == 2, "expected [Parallel(...), smoke_test]"
    parallel, smoke = steps[0], steps[1]
    # Parallel branch contains the baseline step and the client Steps group
    assert type(parallel).__name__ == "Parallel"
    assert getattr(smoke, "name", None) == STEP_NAME_SMOKE
