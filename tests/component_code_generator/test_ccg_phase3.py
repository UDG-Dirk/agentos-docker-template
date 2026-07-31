"""3d Component Code Generator — Phase 3: end-to-end integration + determinism + MCP-safety.

Runs the full pipeline over a 3c-style HELIX_Modules-shaped input (Path-A fork + Path-B from-spec),
with an injected realistic baseline source (self-contained; the real-helix-code fork is Phase 5).
Mock generator → deterministic, CI-safe. Shape/invariant + determinism assertions.
"""
from __future__ import annotations

import json
from pathlib import Path

from agents._shared.observability import engagement_seed
from agents.component_code_generator import ComponentCodeGeneratorOutput, generate_component_code

TS = "2026-07-31T00:00:00.000Z"
_INPUT = Path(__file__).parent / "fixtures" / "ccg_integration_input.json"

# a realistic-but-trimmed baseline Lit component (stands in for helix-code source; injected)
_BASELINE = '''\
import { LitElement, html, css } from "lit";
import { customElement, property } from "lit/decorators.js";
import "../Icon/Icon.js";
@customElement("hx-button")
export class HxButton extends LitElement {
    @property({ reflect: true }) variant = "solid";
    @property({ type: Boolean, reflect: true }) disabled = false;
    static styles = css`:host{ color: var(--helix-colors-text-primary); gap: var(--helix-dimension-spacing-2xs); }`;
    render() { return html`<button ?disabled=${this.disabled}><hx-icon></hx-icon><slot></slot></button>`; }
}
'''


def _reader(baseline_ref, helix_root):
    # every baseline_ref resolves to the trimmed baseline (self-contained integration)
    return (_BASELINE, f"packages/elements/src/atoms/{baseline_ref}.ts") if baseline_ref else None


def _cfg():
    return json.loads(_INPUT.read_text())


def _run(output_dir=None):
    cfg = _cfg()
    return generate_component_code(
        customer_slug=cfg["customer_slug"], scope=cfg["scope"], elements=cfg["elements"],
        tokens_json=cfg["tokens_json"], source_reader=_reader, timestamp=TS, output_dir=output_dir,
    )


def test_end_to_end_forks_and_generates(tmp_path):
    env = _run(output_dir=str(tmp_path / "pkg"))
    # 2 Path-A forks (Button, Label) + 3 Path-B from-spec (HeaderLogo, HeroTeaser, FlyoutNavigationElement)
    assert env.summary.total == 5
    assert env.summary.fork_deterministic == 2
    assert env.summary.from_spec == 3
    assert env.summary.deferred == 0
    assert env.status == "success"
    # Path-A forked source is re-namespaced (no hx-/--helix- leakage)
    root = tmp_path / "pkg" / "packages" / "helix-modules-int-elements" / "src" / "elements"
    btn = next(root.glob("button/*.ts"))
    txt = btn.read_text()
    assert "helix-modules-int-button" in txt and "hx-" not in txt and "--helix-" not in txt.replace("--helix-modules-int-", "")
    # Path-B from-spec source exists + is customer-namespaced
    fly = next(root.glob("flyoutnavigationelement/*.ts"))
    assert "helix-modules-int-flyoutnavigationelement" in fly.read_text()
    # CEM + provenance
    pkg = tmp_path / "pkg" / "packages" / "helix-modules-int-elements"
    assert (pkg / "custom-elements.json").is_file() and (pkg / "docs" / "PROVENANCE.md").is_file()


def test_variant_typos_normalized_in_generated_flyout(tmp_path):
    env = _run(output_dir=str(tmp_path / "pkg"))
    fly = next((tmp_path / "pkg").rglob("flyoutnavigationelement/*.ts"))
    src = fly.read_text()
    # "Activ"/"Focu" from the fixture must be normalized away in the generated component
    assert "Activ" not in src.replace("Active", "") and "Focu" not in src.replace("Focus", "")


def test_cost_summary_and_gate_accounting():
    env = _run()
    assert env.summary.gate_passed == 3 and env.summary.gate_failed == 0   # 3 from-spec all pass gate
    assert env.cost_summary.expected_fine_grained == 3
    assert env.cost_summary.fine_grained_invocations >= 3
    assert env.cost_summary.total_input_tokens == 0        # Mock → no real tokens
    assert env.cost_summary.anomaly is None and env.cost_summary.breaker_tripped is False
    assert env.non_deterministic is False                  # Mock → deterministic


def test_mcp_json_serialisable():
    env = _run()
    d = json.dumps(env.model_dump())
    assert ComponentCodeGeneratorOutput(**json.loads(d)).status == "success"


def test_byte_identical_across_runs(tmp_path):
    a = _run(output_dir=str(tmp_path / "a"))
    b = _run(output_dir=str(tmp_path / "b"))
    # every generated file is byte-identical across runs (fixed timestamp, deterministic Mock)
    files = sorted(p.relative_to(tmp_path / "a") for p in (tmp_path / "a").rglob("*") if p.is_file())
    assert files, "no files emitted"
    for rel in files:
        assert (tmp_path / "a" / rel).read_text() == (tmp_path / "b" / rel).read_text()
    da, db = a.model_dump(), b.model_dump()
    da.pop("package_path"), db.pop("package_path")
    assert da == db


def test_engagement_seed_stable():
    assert engagement_seed("helix-modules-int", TS) == engagement_seed("helix-modules-int", TS)
    assert engagement_seed("other", TS) != engagement_seed("helix-modules-int", TS)
