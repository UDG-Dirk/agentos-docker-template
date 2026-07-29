"""Cycle 3 CEM delivery tests (task cycle-3-baseline-reader-implementation):
- fetch_cem: successful fetch writes the artifact; graceful-degradation on unset env / fetch error
- health fields: last_cem_fetch_timestamp + cem_size_bytes present (populated / null+0)
Deterministic + offline (urlopen is monkeypatched; no network)."""
from __future__ import annotations

import importlib.util
import pathlib

from agents.baseline_reader import step

_FC = pathlib.Path(__file__).parents[2] / "scripts" / "fetch_cem.py"
_spec = importlib.util.spec_from_file_location("fetch_cem", _FC)
fetch_cem = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fetch_cem)


class _FakeResp:
    def __init__(self, data): self._d = data
    def read(self): return self._d
    def __enter__(self): return self
    def __exit__(self, *a): return False


# ---- Component A: startup fetch ----
def test_fetch_writes_artifact(tmp_path, monkeypatch):
    dest = tmp_path / "custom-elements.json"
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Private-token")  # header names title-cased by urllib
        return _FakeResp(b'{"modules":[]}')
    monkeypatch.setattr(fetch_cem.urllib.request, "urlopen", fake_urlopen)
    n = fetch_cem.fetch("https://x/artifact", str(dest), "PRIVATE-TOKEN: sekret")
    assert n == 14 and dest.read_bytes() == b'{"modules":[]}'
    assert seen["url"] == "https://x/artifact" and seen["auth"] == "sekret"  # auth header applied


def test_main_success(tmp_path, monkeypatch):
    dest = tmp_path / "cem.json"
    monkeypatch.setenv(fetch_cem.URL_ENV, "https://x/a")
    monkeypatch.setenv(fetch_cem.PATH_ENV, str(dest))
    monkeypatch.delenv(fetch_cem.AUTH_ENV, raising=False)
    monkeypatch.setattr(fetch_cem.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResp(b"CEMDATA"))
    assert fetch_cem.main() == 0 and dest.read_bytes() == b"CEMDATA"


def test_main_skips_loud_when_env_unset(tmp_path, monkeypatch):
    monkeypatch.delenv(fetch_cem.URL_ENV, raising=False)
    monkeypatch.setenv(fetch_cem.PATH_ENV, str(tmp_path / "cem.json"))
    # URL unset -> skip, return 0, no file, never raises (SP-6 graceful)
    assert fetch_cem.main() == 0
    assert not (tmp_path / "cem.json").exists()


def test_main_graceful_on_fetch_error(tmp_path, monkeypatch):
    dest = tmp_path / "cem.json"
    monkeypatch.setenv(fetch_cem.URL_ENV, "https://x/a")
    monkeypatch.setenv(fetch_cem.PATH_ENV, str(dest))

    def boom(req, timeout=None):
        raise OSError("network down")
    monkeypatch.setattr(fetch_cem.urllib.request, "urlopen", boom)
    assert fetch_cem.main() == 0  # never blocks startup
    assert not dest.exists()      # no partial/garbage file; reader -> CEM_MISSING


# ---- Component B: health fields ----
def test_cem_health_present(tmp_path, monkeypatch):
    cem = tmp_path / "custom-elements.json"
    cem.write_bytes(b'{"modules":[]}')
    monkeypatch.setenv(step.ENV_CEM_PATH, str(cem))
    h = step._cem_health()
    assert h["cem_size_bytes"] == 14
    assert h["last_cem_fetch_timestamp"] and h["last_cem_fetch_timestamp"].endswith("Z")


def test_cem_health_absent(monkeypatch):
    monkeypatch.delenv(step.ENV_CEM_PATH, raising=False)
    h = step._cem_health()
    assert h == {"last_cem_fetch_timestamp": None, "cem_size_bytes": 0}


def test_cem_health_env_set_but_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv(step.ENV_CEM_PATH, str(tmp_path / "nope.json"))
    h = step._cem_health()
    assert h == {"last_cem_fetch_timestamp": None, "cem_size_bytes": 0}
