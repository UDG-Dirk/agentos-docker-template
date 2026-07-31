"""3d v0.2 Path 1 — branch-aware Path-A tests (VT-p1-*). Injected git_show → CI-safe (no helix-code)."""
from __future__ import annotations

import os

from agents.component_code_generator import generate_component_code
from agents.component_code_generator.scaffolding import (
    find_baseline_source,
    get_configured_fork_branches,
    is_valid_lit_source,
)

_VALID_LIT = ('import { LitElement, html } from "lit";\n'
              'import { customElement } from "lit/decorators.js";\n'
              '@customElement("hx-media-text")\n'
              'export class HxMediaText extends LitElement { render() { return html`<slot></slot>`; } }\n')
_BIG_LIT = _VALID_LIT + "\n".join(f"// filler line {i}" for i in range(200)) + "\n"


def _fake_git_show(mapping):
    """mapping: {(branch, path): source}. Returns a git_show(helix_root, branch, path) callable."""
    return lambda root, branch, path: mapping.get((branch, path))


# --------------------------------------------------------------------------- #
# get_configured_fork_branches (D-p1-1) — VT-p1-2/7
# --------------------------------------------------------------------------- #

def test_branches_default_is_master(monkeypatch):
    monkeypatch.delenv("HELIX_CODE_FORK_BRANCHES", raising=False)
    assert get_configured_fork_branches() == ["master"]


def test_branches_from_env_parsed_in_order(monkeypatch):
    monkeypatch.setenv("HELIX_CODE_FORK_BRANCHES", "feature/organisms/MediaText, feature/modules ,master")
    assert get_configured_fork_branches() == ["feature/organisms/MediaText", "feature/modules", "master"]


# --------------------------------------------------------------------------- #
# find_baseline_source branch-aware (VT-p1-1/2/7) — injected git_show
# --------------------------------------------------------------------------- #

def test_master_only_backwards_compat():
    gs = _fake_git_show({("master", "packages/elements/src/atoms/Button/Button.ts"): _VALID_LIT})
    src = find_baseline_source("Button", "/helix", branches=["master"], git_show=gs)
    assert src is not None and src[1] == "master:packages/elements/src/atoms/Button/Button.ts"


def test_finds_organism_in_feature_branch():
    gs = _fake_git_show({("feature/organisms/MediaText",
                          "packages/elements/src/organisms/MediaText/MediaText.ts"): _VALID_LIT})
    src = find_baseline_source("MediaText", "/helix",
                               branches=["master", "feature/organisms/MediaText"], git_show=gs)
    assert src is not None and "feature/organisms/MediaText:" in src[1]


def test_returns_none_when_not_found():
    gs = _fake_git_show({})
    assert find_baseline_source("Nonexistent", "/helix", branches=["master"], git_show=gs) is None


def test_largest_loc_wins_across_branches():
    # same component in two branches; the larger source wins (D-p1-5 completeness proxy)
    gs = _fake_git_show({
        ("feature/modules", "packages/elements/src/organisms/HeroTeaser.ts"): _VALID_LIT,      # small
        ("feature/big", "packages/elements/src/organisms/HeroTeaser/HeroTeaser.ts"): _BIG_LIT,  # large
    })
    src = find_baseline_source("HeroTeaser", "/helix", branches=["feature/modules", "feature/big"], git_show=gs)
    assert src is not None and src[0] == _BIG_LIT


def test_strips_baseline_prefix():
    gs = _fake_git_show({("master", "packages/elements/src/molecules/Card/Card.ts"): _VALID_LIT})
    src = find_baseline_source("core.Card", "/helix", branches=["master"], git_show=gs)  # prefix stripped → Card
    assert src is not None and "molecules/Card/Card.ts" in src[1]


# --------------------------------------------------------------------------- #
# is_valid_lit_source (D-p1-4 WIP detection) — VT-p1-3
# --------------------------------------------------------------------------- #

def test_valid_lit_source_true():
    assert is_valid_lit_source(_VALID_LIT) is True


def test_invalid_lit_source_false():
    assert is_valid_lit_source("export class Foo {") is False          # unbalanced + not Lit
    assert is_valid_lit_source("const x = 1;") is False                 # not a component


# --------------------------------------------------------------------------- #
# WIP branch source: still forked + flagged (D-p1-4 / SP-6) — VT-p1-5
# --------------------------------------------------------------------------- #

def test_wip_branch_source_still_forked_and_flagged():
    wip_src = 'import { LitElement } from "lit";\nexport class HxWip extends LitElement { // unfinished\n'  # unbalanced → WIP
    reader = lambda ref, root: (wip_src, "feature/wip:packages/elements/src/organisms/Wip/Wip.ts")
    env = generate_component_code(
        customer_slug="acme", scope="msq-dx",
        elements=[{"slot": "Wip", "baseline_ref": "Wip", "derivation": "forked_from_baseline", "confidence": "high"}],
        source_reader=reader, timestamp="T")
    # STILL forked (not deferred, not fabricated) + WIP flagged
    assert env.summary.fork_deterministic == 1
    assert any(w.code == "baseline_source_wip" for w in env.blocking_warnings)
    assert env.provenance.elements[0].baseline_ref == "feature/wip:packages/elements/src/organisms/Wip/Wip.ts"


def test_clean_branch_source_no_wip_warning():
    reader = lambda ref, root: (_VALID_LIT, "feature/organisms/MediaText:x")
    env = generate_component_code(
        customer_slug="acme", scope="msq-dx",
        elements=[{"slot": "MediaText", "baseline_ref": "MediaText", "derivation": "forked_from_baseline", "confidence": "high"}],
        source_reader=reader, timestamp="T")
    assert env.summary.fork_deterministic == 1
    assert not any(w.code == "baseline_source_wip" for w in env.blocking_warnings)
