"""3d Component Code Generator — Phase 1 (deterministic) tests. Exact assertions (no LLM)."""
from __future__ import annotations

import json

from agents.component_code_generator import (
    ComponentCodeGeneratorOutput,
    fork_component,
    generate_component_code,
    render_cem,
    route_element,
    slugify,
)

TS = "2026-07-31T00:00:00.000Z"

_BASELINE = '''\
import { LitElement, html, css } from "lit";
import { customElement, property } from "lit/decorators.js";
import "../Icon/Icon.js";

@customElement("hx-icon-button")
export class HxIconButton extends LitElement {
    @property({ reflect: true })
    variant = "solid";
    static styles = css`:host{ color: var(--helix-colors-text-primary); gap: var(--helix-dimension-spacing-2xs); }`;
    render() { return html`<button><hx-icon .name=${this.variant}></hx-icon></button>`; }
}
'''


def _reader_ok(ref, root):
    return (_BASELINE, "packages/elements/src/atoms/IconButton/IconButton.ts") if ref else None


def _reader_missing(ref, root):
    return None


# --------------------------------------------------------------------------- #
# fork_component — deterministic transforms
# --------------------------------------------------------------------------- #

def test_fork_retags_to_customer_namespace():
    src, tag, cls = fork_component(_BASELINE, "acme")
    assert tag == "acme-icon-button"
    assert '@customElement("acme-icon-button")' in src
    assert "hx-" not in src            # every hx- prefix rewritten (incl. nested <hx-icon>)
    assert "</acme-icon>" in src       # nested custom-element tag rewritten (closing tag is bare)


def test_fork_renames_class_pascal_plus_element():
    src, tag, cls = fork_component(_BASELINE, "acme")
    assert cls == "AcmeIconButtonElement"
    assert "export class AcmeIconButtonElement extends LitElement" in src
    assert "HxIconButton" not in src


def test_fork_retokenizes_to_customer_token_namespace():
    src, _, _ = fork_component(_BASELINE, "acme")
    assert "var(--acme-colors-text-primary)" in src
    assert "--helix-" not in src


def test_fork_is_byte_deterministic():
    a, _, _ = fork_component(_BASELINE, "acme")
    b, _, _ = fork_component(_BASELINE, "acme")
    assert a == b


def test_slugify_multiword_customer():
    assert slugify("Acme Corp GmbH") == "acme-corp-gmbh"


# --------------------------------------------------------------------------- #
# route_element
# --------------------------------------------------------------------------- #

def test_route_forked_with_baseline_is_path_a():
    assert route_element(derivation="forked_from_baseline", baseline_ref="Button") == "fork_deterministic"


def test_route_agent_reconciled_with_baseline_is_path_a():
    assert route_element(derivation="agent_reconciled", baseline_ref="core.Card") == "fork_deterministic"


def test_route_passthrough_is_deferred():
    assert route_element(derivation="customer_passthrough", baseline_ref=None) == "deferred"


def test_route_flagged_is_deferred():
    assert route_element(derivation="agent_flagged_review", baseline_ref=None) == "deferred"


def test_route_forked_without_baseline_is_deferred():
    assert route_element(derivation="forked_from_baseline", baseline_ref=None) == "deferred"


# --------------------------------------------------------------------------- #
# generate_component_code — end to end (injected source reader)
# --------------------------------------------------------------------------- #

def _els():
    return [
        {"slot": "IconButton", "baseline_ref": "IconButton", "derivation": "forked_from_baseline", "confidence": "authoritative"},
        {"slot": "HeroTeaser", "baseline_ref": None, "derivation": "customer_passthrough", "confidence": "high"},
    ]


def test_generate_forks_path_a_and_defers_path_b(tmp_path):
    env = generate_component_code(customer_slug="acme", scope="msq-dx", elements=_els(),
                                  tokens_json='{"--x":"1"}', source_reader=_reader_ok, timestamp=TS,
                                  output_dir=str(tmp_path / "pkg"))
    assert env.status == "partial"                          # HeroTeaser deferred
    assert env.summary.fork_deterministic == 1 and env.summary.deferred == 1
    assert env.non_deterministic is False and env.cost_summary.fine_grained_invocations == 0
    # forked file materialised at the customer-namespaced path
    f = tmp_path / "pkg" / "packages" / "acme-elements" / "src" / "elements" / "iconbutton" / "AcmeIconButtonElement.ts"
    assert f.is_file() and "acme-icon-button" in f.read_text()
    # CEM + provenance emitted
    assert (tmp_path / "pkg" / "packages" / "acme-elements" / "custom-elements.json").is_file()
    assert (tmp_path / "pkg" / "packages" / "acme-elements" / "docs" / "PROVENANCE.md").is_file()


def test_generate_all_forked_is_success():
    els = [{"slot": "IconButton", "baseline_ref": "IconButton", "derivation": "forked_from_baseline", "confidence": "high"}]
    env = generate_component_code(customer_slug="acme", scope="msq-dx", elements=els,
                                  source_reader=_reader_ok, timestamp=TS)
    assert env.status == "success" and env.summary.deferred == 0


def test_generate_missing_baseline_source_flags_and_defers():
    els = [{"slot": "IconButton", "baseline_ref": "IconButton", "derivation": "forked_from_baseline", "confidence": "high"}]
    env = generate_component_code(customer_slug="acme", scope="msq-dx", elements=els,
                                  source_reader=_reader_missing, timestamp=TS)
    assert env.status == "partial"
    assert any(w.code == "baseline_source_unavailable" for w in env.blocking_warnings)


def test_generate_empty_input_is_failure():
    env = generate_component_code(customer_slug="acme", scope="msq-dx", elements=[], timestamp=TS)
    assert env.status == "failure"
    assert "input_unavailable" in {w.code for w in env.blocking_warnings}


def test_envelope_is_mcp_json_serialisable():
    env = generate_component_code(customer_slug="acme", scope="msq-dx", elements=_els(),
                                  source_reader=_reader_ok, timestamp=TS)
    d = json.dumps(env.model_dump())
    assert ComponentCodeGeneratorOutput(**json.loads(d)).status == "partial"


def test_cem_lists_elements():
    cem = json.loads(render_cem([{"element_tag": "acme-label", "class_name": "AcmeLabelElement",
                                  "file_path": "packages/acme-elements/src/elements/label/AcmeLabelElement.ts"}]))
    assert cem["schemaVersion"] == "1.0.0"
    assert cem["modules"][0]["declarations"][0]["tagName"] == "acme-label"


def test_verify_gate_green():
    from agents.component_code_generator import verify
    report = verify.build_report()
    assert all(ok for _, _, ok in report), [c for c in report if not c[2]]
    assert verify.main() == 0
