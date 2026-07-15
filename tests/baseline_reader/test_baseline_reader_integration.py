"""Integration suite for Agent 3a Baseline Reader — live helix-code clone.

Assertions are on structural SHAPE, not exact values, so the tests survive
baseline evolution. Skips cleanly if the clone is absent.

CEM-ADAPTIVE (deviation from spec, ratified with Dirk during implementation):
the CEM (custom-elements.json) is a gitignored BUILD ARTIFACT, so a fresh
helix-code clone does not contain it and the task forbids running `pnpm analyze`
to generate it. The "healthy CEM" assertions therefore run only when the CEM is
actually present; when absent, we instead assert the CEM_MISSING blocking-warning
path behaves correctly. This embodies the reader's own "adapt to the baseline"
principle. To exercise the healthy path, run
`pnpm --filter @helix/elements run analyze` in the clone first.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agents.baseline_reader.reader import read_baseline

HELIX = Path("/home/dirk/opencode/workbench/agno-setup/helix-code")
CSS_VAR_RE = re.compile(r"^--helix-[a-z0-9]+(?:-[a-z0-9]+)*$")

pytestmark = pytest.mark.skipif(
    not (HELIX / ".git").exists(),
    reason="live helix-code clone not present",
)


@pytest.fixture(scope="module")
def inv():
    return read_baseline(str(HELIX))


def test_json_serializable(inv):
    json.dumps(inv)


def test_meta_resolved_paths_populated(inv):
    rp = inv["meta"]["resolved_paths"]
    assert rp["cem_source"] and rp["token_sources"] and rp["storybook_config"]
    assert inv["meta"]["commit"] and inv["meta"]["branch"]


def test_tokens_present_and_well_named(inv):
    names = inv["tokens"]["css_var_names"]
    assert names, "expected a non-empty css var vocabulary"
    assert all(CSS_VAR_RE.match(n) for n in names), \
        [n for n in names if not CSS_VAR_RE.match(n)][:5]
    # semantic-dimension breakpoint files should have been read
    assert any("semantic-dimension" in k for k in inv["tokens"]["sources"])


def test_components_cem_adaptive(inv):
    """Healthy shape IF the CEM exists; else the CEM_MISSING path must fire."""
    comp = inv["components"]
    if comp["cem_present"]:
        assert comp["atoms"], "CEM present but no atoms parsed"
        for a in comp["atoms"]:
            assert a["tag"] and a["class_name"] and a["path"]
        assert not any(b["code"] == "CEM_MISSING"
                       for b in inv["blocking_warnings"])
    else:
        codes = [b["code"] for b in inv["blocking_warnings"]]
        assert "CEM_MISSING" in codes
        miss = next(b for b in inv["blocking_warnings"]
                    if b["code"] == "CEM_MISSING")
        assert miss["remediation"] and "analyze" in miss["remediation"]
        assert comp["atoms"] == []


def test_storybook_discoverable(inv):
    sb = inv["storybook"]
    assert sb is not None, "Storybook config should be discoverable"
    order = [t.lower() for t in sb["story_taxonomy_order"]]
    for expected in ("tokens", "atoms", "molecules"):
        assert expected in order


def test_helix_code_untouched():
    """Reader must not have modified the baseline working tree."""
    import subprocess
    before = subprocess.run(
        ["git", "-C", str(HELIX), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    read_baseline(str(HELIX))
    after = subprocess.run(
        ["git", "-C", str(HELIX), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert before == after
