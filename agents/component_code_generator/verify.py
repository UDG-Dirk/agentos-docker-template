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
        {"slot": "HeroTeaser", "baseline_ref": None, "derivation": "customer_passthrough", "confidence": "high"},
    ]
    return generate_component_code(customer_slug="acme", scope="msq-dx", elements=elements,
                                   tokens_json='{"--color": "#000"}', source_reader=_reader,
                                   timestamp="2026-07-31T00:00:00.000Z")


def build_report() -> list[tuple[str, str, bool]]:
    env = _run()
    forked = next((r for r in env.provenance.elements if r.slot == "Label"), None)
    src = env  # convenience
    checks: list[tuple[str, str, bool]] = []

    def chk(vt, desc, ok):
        checks.append((vt, desc, bool(ok)))

    chk("VT-1", "Path-A element forked deterministically", forked is not None and forked.path == "fork_deterministic")
    chk("VT-2", "tag re-namespaced to customer (acme-label)", forked and forked.element_tag == "acme-label")
    chk("VT-3", "class PascalCase+Element (AcmeLabelElement)", forked and forked.class_name == "AcmeLabelElement")
    chk("VT-6", "no-baseline element DEFERRED, not fabricated (SP-6)",
        any(r.slot == "HeroTeaser" and r.path == "deferred" for r in env.provenance.elements)
        and any(w.code == "deferred_to_phase_2" for w in env.blocking_warnings))
    chk("VT-9", "status partial (a deferral exists)", env.status == "partial")
    chk("VT-11", "Phase 1 is deterministic (non_deterministic False, zero LLM)",
        env.non_deterministic is False and env.cost_summary.fine_grained_invocations == 0)
    chk("MCP-safe", "envelope is plain-JSON serialisable", _json_ok(env))
    return checks


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
