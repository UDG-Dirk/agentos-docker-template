"""Cycle 2 Baseline Reader wiring tests (task cycle-2-baseline-reader-wiring):
- reader honours an ABSOLUTE `cem_source` (the seam that lets the CI-built CEM be consumed)
- `baseline_read_executor` threads HELIX_CODE_CEM_PATH -> cem_source (env-only, SP-9)
- standalone `helix-baseline-reader` workflow shape + registration
Deterministic + offline (git is used only on a throwaway tmp repo; no network, no pnpm)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from agents.baseline_reader import step
from agents.baseline_reader.reader import read_baseline
from agents.baseline_reader.step import ENV_CEM_PATH, baseline_read_executor

_FIXTURE_CEM = (Path(__file__).parent / "fixtures" / "helix-code-snapshot-2026-07-14"
                / "packages" / "elements" / "custom-elements.json")


def _init_git_repo(path: Path) -> None:
    """A throwaway git repo with one commit so read_baseline's `git rev-parse` works."""
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(path),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, env=env)
    (path / "README.md").write_text("baseline\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True, env=env)


class _FakeStepInput:
    def __init__(self, additional_data=None):
        self.additional_data = additional_data or {}

    def get_step_output(self, name):  # unused here
        return None


# ---- the seam: absolute cem_source lets the reader read a CI-built CEM ------
def test_reader_reads_absolute_cem_source(tmp_path):
    repo = tmp_path / "baseline"
    _init_git_repo(repo)  # repo has NO committed CEM (mirrors helix-code State 3)
    assert _FIXTURE_CEM.exists(), "fixture CEM missing"

    # cem_source = absolute path to the (CI-built) CEM elsewhere on disk -> reader consumes it
    inv = read_baseline(str(repo), cem_source=str(_FIXTURE_CEM))
    codes = [b["code"] for b in inv.get("blocking_warnings", [])]
    assert "CEM_MISSING" not in codes
    assert inv["components"]["cem_present"] is True
    total = sum(len(inv["components"][b]) for b in inv["components"]
                if isinstance(inv["components"][b], list))
    assert total >= 1  # the 3-module fixture yields >=1 custom element

    # no cem_source and none committed -> CEM_MISSING (fail-loud), never raises
    inv2 = read_baseline(str(repo))
    assert "CEM_MISSING" in [b["code"] for b in inv2.get("blocking_warnings", [])]


# ---- executor threads the env var into cem_source (SP-9 env-only) ----------
def _stub_checkout(*a, **k):
    return (0.0, None)  # (duration, error) — success, no real git


def test_executor_threads_cem_env_to_cem_source(monkeypatch):
    captured = {}

    def fake_read_baseline(*, baseline_repo_path, ref, cem_source, output_dir):
        captured["cem_source"] = cem_source
        return {"meta": {}, "components": {"cem_present": bool(cem_source)}, "blocking_warnings": []}

    monkeypatch.setattr(step, "ensure_baseline_checkout", _stub_checkout)
    monkeypatch.setattr(step, "read_baseline", fake_read_baseline)
    monkeypatch.setenv(step.ENV_USERNAME, "deploy-user")
    monkeypatch.setenv(step.ENV_TOKEN, "tok")
    monkeypatch.setenv(ENV_CEM_PATH, "/data/custom-elements.json")

    out = baseline_read_executor(_FakeStepInput())
    assert captured["cem_source"] == "/data/custom-elements.json"
    assert out.success is True


def test_executor_cem_env_unset_passes_none(monkeypatch):
    captured = {}

    def fake_read_baseline(*, baseline_repo_path, ref, cem_source, output_dir):
        captured["cem_source"] = cem_source
        return {"meta": {}, "components": {}, "blocking_warnings": []}

    monkeypatch.setattr(step, "ensure_baseline_checkout", _stub_checkout)
    monkeypatch.setattr(step, "read_baseline", fake_read_baseline)
    monkeypatch.setenv(step.ENV_USERNAME, "deploy-user")
    monkeypatch.setenv(step.ENV_TOKEN, "tok")
    monkeypatch.delenv(ENV_CEM_PATH, raising=False)

    baseline_read_executor(_FakeStepInput())
    assert captured["cem_source"] is None  # unset -> reader default -> CEM_MISSING loud


# ---- standalone workflow shape + registration ------------------------------
def test_workflow_shape():
    import app.workflows.helix_baseline_reader as w
    wf = w.helix_baseline_reader_workflow
    assert wf.id == "helix-baseline-reader"
    assert len(wf.steps) == 1 and wf.steps[0].name == "baseline-read"


def test_registered_in_agentos():
    try:
        import app.main as m
    except Exception as e:  # noqa: BLE001  # pragma: no cover - env-dependent
        import pytest
        pytest.skip(f"app.main import needs runtime env: {e!r}")
    ids = {getattr(x, "id", None) for x in m.agent_os.workflows}
    assert "helix-baseline-reader" in ids
