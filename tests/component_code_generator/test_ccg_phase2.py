"""3d Component Code Generator — Phase 2 (agentic paths + structural gate) tests.

Deterministic Mock generator (no live LLM); structural-gate + typo-normalization are exact; the
Path-B integration + retry/SP-6 use shape/invariant assertions (SP-17/SP-20).
"""
from __future__ import annotations

import json
from pathlib import Path

from agents._shared.observability import CircuitBreaker
from agents.component_code_generator import generate_component_code
from agents.component_code_generator.generation import (
    GeneratedElement,
    GenerationInput,
    MockGenerator,
    normalize_variant_value,
    parse_variant_axes,
    run_structural_gate,
)

TS = "2026-07-31T00:00:00.000Z"
_FIX = Path(__file__).parent / "fixtures" / "path_b_organism_fixtures.json"

_BASELINE = '''\
import { LitElement, html, css } from "lit";
import { customElement, property } from "lit/decorators.js";
@customElement("hx-label")
export class HxLabel extends LitElement {
    @property({ reflect: true }) text = "";
    static styles = css`:host{ color: var(--helix-colors-text-primary); }`;
    render() { return html`<span>${this.text}</span>`; }
}
'''


def _reader(ref, root):
    return (_BASELINE, "packages/elements/src/atoms/Label/Label.ts") if ref else None


# --------------------------------------------------------------------------- #
# variant typo normalization (P3 "Activ"/"Focu")
# --------------------------------------------------------------------------- #

def test_normalize_state_typos():
    assert normalize_variant_value("Activ") == "Active"
    assert normalize_variant_value("Focu") == "Focus"
    assert normalize_variant_value("hover") == "Hover"     # canonical-cased
    assert normalize_variant_value("Custom") == "Custom"   # unknown left as-is


def test_parse_variant_axes_multi_axis_with_typos():
    axes = parse_variant_axes(["State=Activ, Size=md", "State=Focu, Size=lg", "State=Default, Size=md"])
    assert axes["State"] == ["Active", "Focus", "Default"]   # typos fixed, order preserved, deduped
    assert axes["Size"] == ["md", "lg"]


# --------------------------------------------------------------------------- #
# structural validation gate (Adjustment 1 / VT-17)
# --------------------------------------------------------------------------- #

_GOOD = '''\
import { LitElement, html, css } from "lit";
import { customElement, property } from "lit/decorators.js";
@customElement("acme-widget")
export class AcmeWidgetElement extends LitElement {
    @property({ reflect: true }) variant = "";
    static styles = css`:host{ color: var(--acme-colors-text-primary); }`;
    render() { return html`<div></div>`; }
}
'''


def test_gate_passes_valid_source():
    g = run_structural_gate(_GOOD, customer_slug="acme", element_slug="widget")
    assert g.applied and g.passed and not g.failures


def test_gate_fails_on_helix_token_namespace():
    bad = _GOOD.replace("var(--acme-colors", "var(--helix-colors")
    g = run_structural_gate(bad, customer_slug="acme", element_slug="widget")
    assert not g.passed and any("token_namespace" in f for f in g.failures)


def test_gate_fails_on_wrong_tag():
    bad = _GOOD.replace("acme-widget", "hx-widget")
    g = run_structural_gate(bad, customer_slug="acme", element_slug="widget")
    assert not g.passed and any("naming" in f or "registration" in f for f in g.failures)


def test_gate_fails_on_unbalanced_or_missing_lit():
    g = run_structural_gate("export class Foo {", customer_slug="acme", element_slug="widget")
    assert not g.passed


def test_gate_accepts_single_quote_import_and_no_properties():
    # SP-26 regression (Phase-5 live-run): real claude-sonnet-4-6 emits single-quote imports and a
    # prop-less component (e.g. HeaderLogo) — both are valid Lit and MUST pass the gate.
    src = ("import { LitElement, html, css } from 'lit';\n"
           "import { customElement } from 'lit/decorators.js';\n"
           "@customElement('acme-logo')\n"
           "export class AcmeLogoElement extends LitElement {\n"
           "  static styles = css`:host{ color: var(--acme-colors-text-primary); }`;\n"
           "  render() { return html`<slot></slot>`; }\n"
           "}\n")
    g = run_structural_gate(src, customer_slug="acme", element_slug="logo")
    assert g.passed, g.failures


# --------------------------------------------------------------------------- #
# MockGenerator → gate-passing from-spec skeleton
# --------------------------------------------------------------------------- #

def test_mock_generator_output_passes_gate():
    spec = GenerationInput(slot="HeroTeaser", customer_slug="acme",
                           variant_axes={"State": ["Default", "Active"]})
    res = MockGenerator().generate(spec)
    g = run_structural_gate(res.source, customer_slug="acme", element_slug="heroteaser")
    assert g.passed
    assert '@customElement("acme-heroteaser")' in res.source
    assert "AcmeHeroteaserElement" in res.source
    assert "--acme-" in res.source and "--helix-" not in res.source


# --------------------------------------------------------------------------- #
# Path-B integration + retry/SP-6 + breaker
# --------------------------------------------------------------------------- #

def _pathb_element(slot="HeroTeaser", variants=None):
    return {"slot": slot, "baseline_ref": None, "derivation": "customer_passthrough", "confidence": "high",
            "figma_meta": {"variant_names": variants or ["State=Default", "State=Activ"]}}


def test_pathb_generated_and_gate_passed():
    env = generate_component_code(customer_slug="acme", scope="msq-dx",
                                  elements=[_pathb_element()], source_reader=_reader, timestamp=TS)
    assert env.status == "success" and env.summary.from_spec == 1 and env.summary.gate_passed == 1
    el = env.provenance.elements[0]
    assert el.path == "from_spec" and el.structural_gate.applied and el.structural_gate.passed
    assert env.cost_summary.fine_grained_invocations >= 1
    assert env.cost_summary.total_input_tokens == 0        # Mock → no real tokens
    assert env.non_deterministic is False                  # Mock → deterministic


class _BadGenerator:
    """Emits gate-failing source (wrong token namespace) — exercises retry + SP-6."""
    name = "mock"
    def generate(self, spec):
        return GeneratedElement(source='import {LitElement} from "lit";\n@customElement("acme-x")\n'
                                       'export class AcmeXElement extends LitElement { '
                                       'render(){return "";} } // var(--helix-bad)',
                                confidence="high", rationale="deliberately bad")


def test_gate_failure_after_retry_flags_sp6_no_fabrication():
    env = generate_component_code(customer_slug="acme", scope="msq-dx",
                                  elements=[_pathb_element(slot="X")], generator=_BadGenerator(),
                                  source_reader=_reader, timestamp=TS)
    assert env.status == "partial"                         # gate_failed → not success
    assert env.summary.gate_failed == 1
    assert any(w.code == "structural_gate_failed" for w in env.blocking_warnings)
    # not emitted (no file), not fabricated
    assert env.provenance.elements[0].path == "from_spec" and env.provenance.elements[0].confidence == "unresolved"


def test_breaker_blocks_generation_and_sets_anomaly():
    breaker = CircuitBreaker(max_fine_grained=0, max_coarse_chunks=6)
    env = generate_component_code(customer_slug="acme", scope="msq-dx",
                                  elements=[_pathb_element()], breaker=breaker,
                                  source_reader=_reader, timestamp=TS)
    assert env.cost_summary.breaker_tripped is True
    assert env.summary.deferred == 1 and env.status == "partial"


class _FakeRealGenerator:
    name = "agent"
    def generate(self, spec):
        return MockGenerator().generate(spec)
    def usage(self):
        return (1234, 567)


def test_real_generator_marks_non_deterministic_and_captures_tokens():
    env = generate_component_code(customer_slug="acme", scope="msq-dx",
                                  elements=[_pathb_element()], generator=_FakeRealGenerator(),
                                  source_reader=_reader, timestamp=TS)
    assert env.non_deterministic is True                   # real generator name != mock
    assert env.cost_summary.total_input_tokens == 1234 and env.cost_summary.total_output_tokens == 567


# --------------------------------------------------------------------------- #
# Adjustment-2 fixture probe: Mock + gate over all 5 organism fixtures
# --------------------------------------------------------------------------- #

def test_path_b_fixture_probe_gate_passes_all():
    fixtures = json.loads(_FIX.read_text())["fixtures"]
    passed = 0
    for fx in fixtures:
        spec = GenerationInput(slot=fx["slot"], customer_slug="acme",
                               variant_axes=parse_variant_axes(fx["figma_meta"].get("variant_names") or []),
                               tokens_consumed=fx["figma_meta"].get("tokens_consumed") or [])
        res = MockGenerator().generate(spec)
        from agents.component_code_generator.scaffolding import slugify
        if run_structural_gate(res.source, customer_slug="acme", element_slug=slugify(fx["slot"])).passed:
            passed += 1
    assert passed == len(fixtures)                          # Mock skeleton passes the gate for all 5 (≥80% target)
