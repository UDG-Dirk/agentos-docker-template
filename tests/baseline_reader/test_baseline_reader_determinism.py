"""Determinism suite for Agent 3a Baseline Reader — frozen fixtures, exact values.

Fixtures live under fixtures/helix-code-snapshot-2026-07-14/. Edge-case variants
are derived into tmp_path from the base fixture so the committed tree stays small.
A fixed ``_now`` is injected so the (otherwise wall-clock) meta.read_at does not
break byte-identical assertions; git metadata is constant within a test run.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agents.baseline_reader.reader import read_baseline, BaselineReaderError

FIXTURE = Path(__file__).parent / "fixtures" / "helix-code-snapshot-2026-07-14"
FROZEN = datetime(2026, 7, 14, 14, 53, 0, tzinfo=timezone.utc)


def _read(path, **kw):
    return read_baseline(str(path), _now=FROZEN, **kw)


def _copy_fixture(tmp_path) -> Path:
    """Copy the base fixture and make it a real git repo (reader requires one)."""
    dst = tmp_path / "baseline"
    shutil.copytree(FIXTURE, dst)
    subprocess.run(["git", "init", "-q", str(dst)], check=True)
    return dst


# --- byte-identical determinism ------------------------------------------- #
def test_byte_identical_across_three_runs():
    a = _read(FIXTURE)
    b = _read(FIXTURE)
    c = _read(FIXTURE)
    da = json.dumps(a, sort_keys=True)
    assert da == json.dumps(b, sort_keys=True) == json.dumps(c, sort_keys=True)


def test_json_serializable():
    inv = _read(FIXTURE)
    json.dumps(inv)  # must not raise


# --- exact token extraction ----------------------------------------------- #
def test_exact_token_count_and_names():
    inv = _read(FIXTURE)
    names = inv["tokens"]["css_var_names"]
    # 8 unique css vars, hand-verified from the fixture token files
    assert len(names) == 8
    for expected in (
        "--helix-size-8",
        "--helix-size-16",
        "--helix-stroke-weight-md",
        "--helix-color-brand-purple",
        "--helix-color-error-bg",
        "--helix-dimension-spacing-components-md",
        "--helix-dimension-layout-margin",
        "--helix-font-family-body-2-strong",
    ):
        assert expected in names
    assert inv["tokens"]["naming_pattern"] == "--helix-{category}-{variant}-{modifier}"
    assert inv["tokens"]["categories"]["size"]["layer"] == "primitive"
    assert inv["tokens"]["categories"]["color"]["layer"] == "semantic"


def test_collision_suffix_flagged():
    inv = _read(FIXTURE)
    assert any("-N" in w or "collision" in w.lower()
               for w in inv["warnings"] if isinstance(w, str))


# --- CEM parsing ----------------------------------------------------------- #
def test_cem_parsed_atoms_and_molecules():
    inv = _read(FIXTURE)
    comp = inv["components"]
    assert comp["cem_present"] is True
    tags = {a["tag"] for a in comp["atoms"]}
    assert {"hx-button", "hx-heading"} <= tags
    assert {m["tag"] for m in comp["molecules"]} == {"hx-input-group"}
    button = next(a for a in comp["atoms"] if a["tag"] == "hx-button")
    assert button["attrs"] == ["disabled", "size", "variant"]
    assert button["slots"] == ["default"]
    assert button["events"] == ["click"]
    # static member 'styles' must be filtered out of props
    assert "styles" not in button["props"]
    assert set(button["props"]) == {"disabled", "variant"}


def test_missing_cem_blocking_warning(tmp_path):
    base = _copy_fixture(tmp_path)
    (base / "packages/elements/custom-elements.json").unlink()
    inv = _read(base)
    assert inv["components"]["cem_present"] is False
    assert inv["components"]["atoms"] == []
    blk = inv["blocking_warnings"]
    assert len(blk) == 1
    assert blk[0]["code"] == "CEM_MISSING"
    assert blk[0]["message"]
    assert blk[0]["remediation"]  # non-empty, actionable
    assert "analyze" in blk[0]["remediation"]


def test_malformed_token_json_fails_with_path(tmp_path):
    base = _copy_fixture(tmp_path)
    bad = base / "packages/tokens/tokens/primitive/Core.tokens.json"
    bad.write_text("{ not valid json ", encoding="utf-8")
    with pytest.raises(BaselineReaderError) as exc:
        _read(base)
    assert "Core.tokens.json" in str(exc.value)


# --- storybook resolution -------------------------------------------------- #
def test_storybook_parsed():
    inv = _read(FIXTURE)
    sb = inv["storybook"]
    assert sb["framework"] == "@storybook/web-components-vite"
    assert sb["cem_driven_controls"] is True
    assert sb["story_taxonomy_order"][:3] == ["tokens", "atoms", "molecules"]
    assert "Button" in sb["existing_stories"].get("atoms", [])


def test_storybook_discovery_when_default_missing(tmp_path):
    base = _copy_fixture(tmp_path)
    # move the .storybook out of the default packages/storybook location
    src = base / "packages/storybook/.storybook"
    dst_pkg = base / "packages/ui"
    (dst_pkg / "src").mkdir(parents=True)
    shutil.copytree(src, dst_pkg / ".storybook")
    (dst_pkg / "src" / "X.stories.ts").write_text("export default {}\n", encoding="utf-8")
    shutil.rmtree(base / "packages/storybook")
    inv = _read(base)
    assert inv["storybook"] is not None
    assert inv["meta"]["resolved_paths"]["storybook_config"] != \
        "packages/storybook/.storybook/"
    assert any("discovered" in w.lower() for w in inv["warnings"] if isinstance(w, str))


def test_ambiguous_storybook_warns_and_picks_first(tmp_path):
    base = _copy_fixture(tmp_path)
    src = base / "packages/storybook/.storybook"
    # create TWO valid candidates under non-default packages
    for pkg in ("ui", "ui2"):
        other = base / "packages" / pkg
        (other / "src").mkdir(parents=True)
        shutil.copytree(src, other / ".storybook")
        (other / "src" / "Y.stories.ts").write_text("export default {}\n", encoding="utf-8")
    # remove the default so heuristic discovery runs over the two candidates
    shutil.rmtree(base / "packages/storybook")
    inv = _read(base)
    assert inv["storybook"] is not None
    assert any("multiple candidate" in w.lower() for w in inv["warnings"]
               if isinstance(w, str))


# --- constraints ----------------------------------------------------------- #
def test_constraints_detected():
    inv = _read(FIXTURE)
    con = inv["constraints"]
    assert con["override_registry_status"] == "open"
    assert "@lit/context" in con["override_registry_candidates"]
    # the fixture Button.ts has a hardcoded padding-bottom: 25px
    hits = con["hardcoded_values_detected"]
    assert any("25px" in h["value"] for h in hits)
    # the calc()/var() line must NOT be flagged
    assert all("calc" not in h["value"] for h in hits)


# --- output_dir persistence ----------------------------------------------- #
def test_output_dir_writes_file_and_pointer(tmp_path):
    out = tmp_path / "runs"
    inv = _read(FIXTURE, output_dir=str(out))
    files = list(out.glob("baseline-inventory-*.json"))
    timestamped = [f for f in files if f.name != "baseline-inventory-latest.json"]
    assert len(timestamped) == 1
    pointer = out / "baseline-inventory-latest.json"
    assert pointer.exists()
    assert json.loads(pointer.read_text()) == json.loads(timestamped[0].read_text())


def test_output_dir_null_writes_nothing(tmp_path):
    _read(FIXTURE, output_dir=None)
    assert not any(tmp_path.iterdir())


def test_output_dir_not_writable_warns_but_returns(tmp_path):
    blocker = tmp_path / "iamafile"
    blocker.write_text("x", encoding="utf-8")
    target = blocker / "sub" / "runs"  # parent is a file -> mkdir fails
    inv = _read(FIXTURE, output_dir=str(target))
    assert inv["components"]["cem_present"] is True  # in-memory contract intact
    assert any("output_dir" in w for w in inv["warnings"] if isinstance(w, str))


def test_symlink_failure_falls_back_to_copy(tmp_path, monkeypatch):
    out = tmp_path / "runs"

    def boom(*a, **k):
        raise OSError("symlinks not supported")

    monkeypatch.setattr("agents.baseline_reader.reader.os.symlink", boom)
    inv = _read(FIXTURE, output_dir=str(out))
    pointer = out / "baseline-inventory-latest.json"
    assert pointer.exists() and not pointer.is_symlink()
    assert any("copy" in w.lower() for w in inv["warnings"] if isinstance(w, str))
