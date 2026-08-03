"""Executable verification gate for 3d Component Code Generator (Phase 1 stub; SP-23 candidate).

    python -m agents.component_code_generator.verify

Runs the deterministic Phase-1 pipeline over a self-contained synthetic baseline (a tiny Lit
component) with an injected source reader — no helix-code checkout, no LLM — and asserts the
Path-A fork invariants. Exit 0 = green. Grows as Phases 2-4 land (structural-gate + from-spec VTs).
"""
from __future__ import annotations

import json

from agents.component_code_generator import generate_component_code

_FAKE_BASELINE = '''\
import { LitElement, html, css } from "lit";
import { customElement, property } from "lit/decorators.js";

@customElement("hx-label")
export class HxLabel extends LitElement {
    @property({ reflect: true })
    text = "";
    static styles = css`:host{ color: var(--helix-colors-text-primary); }`;
    render() { return html`<span><hx-icon></hx-icon>${this.text}</span>`; }
}
'''


def _reader(baseline_ref, helix_root):  # injected — returns the fake baseline for "Label"
    return (_FAKE_BASELINE, "packages/elements/src/atoms/Label/Label.ts") if baseline_ref else None


def _run():
    elements = [
        {"slot": "Label", "baseline_ref": "Label", "derivation": "forked_from_baseline", "confidence": "authoritative"},
        # no baseline → Path B from-spec; thin Figma meta with a typo'd variant ("Activ") to exercise normalization
        {"slot": "HeroTeaser", "baseline_ref": None, "derivation": "customer_passthrough", "confidence": "high",
         "figma_meta": {"variant_names": ["State=Default", "State=Activ"]}},
    ]
    return generate_component_code(customer_slug="acme", scope="msq-dx", elements=elements,
                                   tokens_json='{"--color": "#000"}', source_reader=_reader,
                                   timestamp="2026-07-31T00:00:00.000Z")


def build_report() -> list[tuple[str, str, bool]]:
    env = _run()
    forked = next((r for r in env.provenance.elements if r.slot == "Label"), None)
    spec = next((r for r in env.provenance.elements if r.slot == "HeroTeaser"), None)
    checks: list[tuple[str, str, bool]] = []

    def chk(vt, desc, ok):
        checks.append((vt, desc, bool(ok)))

    chk("VT-1", "Path-A element forked deterministically", forked is not None and forked.path == "fork_deterministic")
    chk("VT-2", "HELIX tag preserved verbatim (hx-label) — Correction #18", forked and forked.element_tag == "hx-label")
    chk("VT-3", "HELIX class preserved verbatim (HxLabel) — Correction #18", forked and forked.class_name == "HxLabel")
    chk("VT-6", "no-baseline element GENERATED from-spec (not fabricated blindly)",
        spec is not None and spec.path == "from_spec" and spec.element_tag == "acme-heroteaser")
    chk("VT-17", "structural validation gate applied + passed on generated code",
        spec is not None and spec.structural_gate.applied and spec.structural_gate.passed)
    chk("VT-9", "status success (fork + from-spec both resolved)", env.status == "success")
    chk("VT-11", "Mock path deterministic (non_deterministic False, zero real tokens)",
        env.non_deterministic is False and env.cost_summary.total_input_tokens == 0)
    chk("VT-5", "variant typo normalized in generated output (Activ→Active)",
        spec is not None and spec.file_path is not None and "Active" in _spec_src(env) and "Activ," not in _spec_src(env))
    chk("VT-8", "cost_summary carries invocation + token fields (capture wired; value pending real run)",
        env.cost_summary.expected_fine_grained >= 1 and env.cost_summary.fine_grained_invocations >= 1
        and isinstance(env.cost_summary.total_input_tokens, int))
    chk("VT-14", "anomaly clean + breaker not tripped at expected invocation count",
        env.cost_summary.anomaly is None and env.cost_summary.breaker_tripped is False)
    chk("VT-10", "byte-identical envelope across runs (determinism)", _deterministic())
    chk("MCP-safe", "envelope is plain-JSON serialisable", _json_ok(env))
    return checks


def _spec_src(env) -> str:
    """The generated from-spec source for HeroTeaser (re-run to a tmp string via the Mock)."""
    from agents.component_code_generator.generation import GenerationInput, MockGenerator, parse_variant_axes
    spec = GenerationInput(slot="HeroTeaser", customer_slug="acme",
                           variant_axes=parse_variant_axes(["State=Default", "State=Activ"]))
    return MockGenerator().generate(spec).source


def _deterministic() -> bool:
    a, b = _run().model_dump(), _run().model_dump()
    a.pop("package_path"), b.pop("package_path")
    return a == b


def _json_ok(env) -> bool:
    try:
        json.dumps(env.model_dump())
        return True
    except (TypeError, ValueError):
        return False


def main() -> int:
    report = build_report()
    print("3d Component Code Generator — verification gate (Phase 1, deterministic)\n")
    w = max(len(vt) for vt, _, _ in report)
    for vt, desc, ok in report:
        print(f"  [{'PASS' if ok else 'FAIL'}] {vt.ljust(w)}  {desc}")
    passed = sum(1 for _, _, ok in report if ok)
    print(f"\n{passed}/{len(report)} checks passed.")
    print("(Phase-2 structural-gate + from-spec VTs and Phase-5 live-run VTs land in later phases.)")
    return 0 if passed == len(report) else 1


if __name__ == "__main__":
    raise SystemExit(main())
