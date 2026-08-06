"""v0.3 Phase 2 verification tests — token-value substitution + packager foundation.

VT-1..4, 6, 8 are fixture-based unit tests (always run). VT-5 uses the frozen
helix-code snapshot already committed for the baseline_reader determinism
suite (tests/baseline_reader/fixtures/helix-code-snapshot-2026-07-14/) — a
real light-mode.tokens.json, no live clone needed. VT-7 needs the live
helix-code clone with node_modules installed (pnpm build:tokens); it skips
cleanly when that clone is absent, mirroring
tests/baseline_reader/test_baseline_reader_integration.py's precedent.
"""

from __future__ import annotations

import copy
import json
import logging
import shutil
from pathlib import Path

import pytest

from agents.overlay_packager.fork_ops import ForkOpsError, create_or_fetch_client_fork
from agents.overlay_packager.step import (
    apply_token_substitution,
    make_scratch_fork,
    run_build_tokens_gate,
)
from agents.overlay_packager.token_substitution import (
    CoverageGateError,
    CoverageReport,
    KnownUncovered,
    assert_coverage,
    substitute_token_values,
)

FIXTURE = Path(__file__).parent.parent / "baseline_reader" / "fixtures" / "helix-code-snapshot-2026-07-14"
REAL_LIGHT_MODE = FIXTURE / "packages" / "tokens" / "tokens" / "semantic-color" / "light-mode.tokens.json"

HELIX_LIVE = Path("/home/dirk/opencode/workbench/agno-setup/helix-code")
_live_available = (HELIX_LIVE / ".git").exists() and shutil.which("pnpm") is not None


def _mini_tree() -> dict:
    """3 leaves: one literal color, one alias, one with $extensions to preserve."""
    return {
        "colors": {
            "brand": {
                "primary": {
                    "$type": "color",
                    "$value": {"colorSpace": "srgb", "components": [1, 1, 1], "alpha": 1, "hex": "#FFFFFF"},
                    "$extensions": {"com.figma.variableId": "VariableID:1:1"},
                },
                "secondary": {"$type": "color", "$value": "{colors.brand.primary}"},
            },
            "icon": {
                "disabled": {
                    "$type": "color",
                    "$value": {"colorSpace": "srgb", "components": [0, 0, 0], "alpha": 1, "hex": "#000000"},
                },
            },
        }
    }


def _walk(node, path, out):
    if isinstance(node, dict) and "$value" in node:
        out.append(".".join(path))
        return
    if isinstance(node, dict):
        for k, v in node.items():
            if k.startswith("$"):
                continue
            _walk(v, path + [k], out)


def _leaf_paths(tree: dict) -> list[str]:
    out: list[str] = []
    _walk(tree, [], out)
    return sorted(out)


# --------------------------------------------------------------------------- #
# VT-1 — full customer map: all matched, $value replaced, $type/$extensions kept
# --------------------------------------------------------------------------- #
def test_vt1_full_customer_map_all_matched():
    tree = _mini_tree()
    customer_map = {
        "colors.brand.primary": {"colorSpace": "srgb", "components": [0, 0, 1], "alpha": 1, "hex": "#0000FF"},
        "colors.brand.secondary": "#00FF00",
        "colors.icon.disabled": {"colorSpace": "srgb", "components": [0.5, 0.5, 0.5], "alpha": 1, "hex": "#808080"},
    }
    modified, report = substitute_token_values(tree, customer_map)

    assert report.total_leaves == 3
    assert report.matched == 3
    assert report.unmatched == []
    assert report.extra == []

    assert modified["colors"]["brand"]["primary"]["$value"] == customer_map["colors.brand.primary"]
    assert modified["colors"]["brand"]["primary"]["$type"] == "color"
    assert modified["colors"]["brand"]["primary"]["$extensions"] == {"com.figma.variableId": "VariableID:1:1"}
    assert modified["colors"]["brand"]["secondary"]["$value"] == "#00FF00"
    assert modified["colors"]["icon"]["disabled"]["$value"] == customer_map["colors.icon.disabled"]
    # names/paths unchanged
    assert _leaf_paths(modified) == _leaf_paths(tree)
    # input untouched (deep copy)
    assert tree["colors"]["brand"]["primary"]["$value"]["hex"] == "#FFFFFF"


# --------------------------------------------------------------------------- #
# VT-2 — unmatched leaves, no known_uncovered -> fail loud, lists the paths
# --------------------------------------------------------------------------- #
def test_vt2_unmatched_without_known_uncovered_raises():
    tree = _mini_tree()
    customer_map = {"colors.brand.primary": "#0000FF"}  # 2 of 3 leaves unmatched
    _modified, report = substitute_token_values(tree, customer_map)

    assert report.unmatched == ["colors.brand.secondary", "colors.icon.disabled"]
    with pytest.raises(CoverageGateError) as exc_info:
        assert_coverage(report)
    assert "colors.brand.secondary" in str(exc_info.value)
    assert "colors.icon.disabled" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# VT-3 — unmatched + valid known_uncovered annotations -> gate passes
# --------------------------------------------------------------------------- #
def test_vt3_known_uncovered_annotation_passes_gate():
    tree = _mini_tree()
    customer_map = {"colors.brand.primary": "#0000FF"}
    _modified, report = substitute_token_values(tree, customer_map)

    known = [
        KnownUncovered(path="colors.brand.secondary", reason="alias-only, no direct customer value", owner="dirk"),
        KnownUncovered(path="colors.icon.disabled", reason="baseline default acceptable", owner="dirk"),
    ]
    result = assert_coverage(report, known_uncovered=known)
    assert result is report
    assert report.known_uncovered == known


# --------------------------------------------------------------------------- #
# VT-4 — extra paths not in the tree -> WARN, no exception, extras listed
# --------------------------------------------------------------------------- #
def test_vt4_extra_paths_warn_not_fail(caplog):
    tree = _mini_tree()
    customer_map = {
        "colors.brand.primary": "#0000FF",
        "colors.brand.secondary": "#00FF00",
        "colors.icon.disabled": "#808080",
        "colors.brand.tertiary": "#ABCDEF",  # not in tree
    }
    _modified, report = substitute_token_values(tree, customer_map)
    assert report.unmatched == []
    assert report.extra == ["colors.brand.tertiary"]

    with caplog.at_level(logging.WARNING):
        assert_coverage(report)  # must not raise
    assert any("colors.brand.tertiary" in rec.message for rec in caplog.records)


# --------------------------------------------------------------------------- #
# VT-5 — round-trip against the real light-mode.tokens.json (frozen snapshot)
# --------------------------------------------------------------------------- #
def test_vt5_round_trip_real_light_mode_preserves_names():
    real_tree = json.loads(REAL_LIGHT_MODE.read_text(encoding="utf-8"))
    before_paths = _leaf_paths(real_tree)
    assert before_paths, "fixture must have leaves"

    # identity map: every leaf keeps its own value -> total coverage, no drift
    identity_map = {}

    def _collect(node, path):
        if isinstance(node, dict) and "$value" in node:
            identity_map[".".join(path)] = copy.deepcopy(node["$value"])
            return
        if isinstance(node, dict):
            for k, v in node.items():
                if k.startswith("$"):
                    continue
                _collect(v, path + [k])

    _collect(real_tree, [])

    modified, report = substitute_token_values(real_tree, identity_map)
    assert_coverage(report)  # full coverage, no gate failure

    after_paths = _leaf_paths(modified)
    assert after_paths == before_paths  # no renames, no drops, no additions
    assert report.total_leaves == len(before_paths)
    assert report.matched == len(before_paths)

    # values are byte-identical (identity substitution) — proves only $value
    # is ever touched and every other field (incl. $extensions) survives.
    assert modified == real_tree


# --------------------------------------------------------------------------- #
# VT-6 — blank slug fails loud; non-blank slug resolves a ForkResult
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad_slug", [None, "", "   "])
def test_vt6_blank_slug_fails_loud(bad_slug):
    with pytest.raises(ForkOpsError) as exc_info:
        create_or_fetch_client_fork(bad_slug, "master")
    assert exc_info.value.code == "FORK_SLUG_BLANK"


def test_vt6_nonblank_slug_returns_fork_result():
    class _FakeProc:
        returncode = 0
        stdout = "abc123def4567890abc123def4567890abc123d\trefs/heads/master\n"
        stderr = ""

    def _fake_runner(args, *, env):
        return _FakeProc()

    result = create_or_fetch_client_fork(
        "Acme Corp",
        "master",
        _git_runner=_fake_runner,
    )
    assert result.slug == "Acme Corp"
    assert result.resolved_sha == "abc123def4567890abc123def4567890abc123d"
    assert result.fork_path.endswith("/Acme Corp")


def test_vt6_unresolved_ref_fails_loud():
    class _FakeProc:
        returncode = 0
        stdout = ""
        stderr = ""

    result_runner = lambda args, *, env: _FakeProc()
    with pytest.raises(ForkOpsError) as exc_info:
        create_or_fetch_client_fork("acme", "no-such-ref", _git_runner=result_runner)
    assert exc_info.value.code == "FORK_BASELINE_REF_UNRESOLVED"


# --------------------------------------------------------------------------- #
# VT-7 — scratch fork + `pnpm build:tokens` gate (live helix-code clone)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not _live_available, reason="live helix-code clone (with node_modules) not present")
class TestScratchForkBuildGate:
    def _identity_map_from(self, fork_path: str) -> dict:
        out: dict = {}
        for f in sorted(Path(fork_path).glob("packages/tokens/tokens/semantic-*/*.tokens.json")):
            data = json.loads(f.read_text(encoding="utf-8"))

            def _collect(node, path):
                if isinstance(node, dict) and "$value" in node:
                    out[".".join(path)] = copy.deepcopy(node["$value"])
                    return
                if isinstance(node, dict):
                    for k, v in node.items():
                        if k.startswith("$"):
                            continue
                        _collect(v, path + [k])

            _collect(data, [])
        return out

    def test_vt7_substituted_values_produce_green_build(self, tmp_path):
        fork = make_scratch_fork(str(HELIX_LIVE), str(tmp_path / "fork-green"))
        identity_map = self._identity_map_from(fork)

        result = apply_token_substitution(fork, identity_map)

        assert result.status == "success"
        assert result.build_gate is not None
        assert result.build_gate.passed is True
        assert result.files_written  # at least one *.tokens.json rewritten

    def test_vt7_broken_value_produces_red_build_with_actionable_error(self, tmp_path):
        fork = make_scratch_fork(str(HELIX_LIVE), str(tmp_path / "fork-red"))
        identity_map = self._identity_map_from(fork)
        # deliberately break one leaf: alias a token that doesn't exist
        some_path = next(iter(identity_map))
        identity_map[some_path] = "{colors.does.not.exist}"

        result = apply_token_substitution(fork, identity_map)

        assert result.status == "failure"
        assert result.build_gate is not None
        assert result.build_gate.passed is False
        assert result.build_gate.stderr_tail  # actionable, non-empty


# --------------------------------------------------------------------------- #
# VT-8 — coverage report shape is internally consistent
# --------------------------------------------------------------------------- #
def test_vt8_coverage_report_shape_consistent():
    tree = _mini_tree()
    customer_map = {
        "colors.brand.primary": "#0000FF",  # matched
        "colors.brand.tertiary": "#ABCDEF",  # extra (not in tree)
        "colors.brand.quaternary": "#123456",  # extra (not in tree)
    }
    _modified, report = substitute_token_values(tree, customer_map)

    assert report.total_leaves == 3
    assert report.matched == 1
    assert report.matched + len(report.unmatched) == report.total_leaves
    assert set(report.unmatched) == {"colors.brand.secondary", "colors.icon.disabled"}
    assert set(report.extra) == {"colors.brand.tertiary", "colors.brand.quaternary"}
    assert len(report.extra) == 2
