"""3d Component Code Generator — deterministic transforms (Phase 1).

Pure, zero-LLM functions:
  * ``route_element`` — decide Path A (deterministic fork) vs Path B (from-spec, deferred in P1)
    from a 3c element descriptor.
  * ``fork_component`` — the Path-A transform: re-tag, re-class, re-tokenise a baseline Lit
    component's source into the customer namespace. Byte-identical for identical inputs.
  * ``find_baseline_source`` — locate a baseline component's ``.ts`` in the helix-code checkout
    (READ-ONLY) by its ref/name.
  * package scaffolding, minimal CEM, and the 3d PROVENANCE.md extension.

SP-9: the customer scope/slug is a caller argument; nothing hardcoded. helix-code is READ-ONLY.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

PackageTree = dict[str, str]

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_CUSTOM_ELEMENT_RE = re.compile(r"""@customElement\(\s*["']([a-z][a-z0-9-]*)["']\s*\)""")
_CLASS_RE = re.compile(r"""\bclass\s+(\w+)\s+extends\s+LitElement\b""")


def slugify(name: str) -> str:
    s = _SLUG_RE.sub("-", (name or "").strip().lower()).strip("-")
    return s or "customer"


def pascal_case(kebab: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in kebab.split("-") if part)


def _baseline_name(ref: Optional[str]) -> Optional[str]:
    """Normalise a 3c baseline_ref to a bare component name (strips a 'baseline.'/'core.' prefix)."""
    if not ref:
        return None
    return ref.split(".")[-1].strip() or None


def route_element(*, derivation: str, baseline_ref: Optional[str]) -> str:
    """Deterministic path routing from a 3c element descriptor.

    Path A (fork_deterministic) when the slot maps to a baseline component; otherwise Path B,
    which Phase 1 records as 'deferred' (Phase 2 implements the agentic from-spec path).
    """
    if derivation in ("forked_from_baseline", "agent_reconciled") and _baseline_name(baseline_ref):
        return "fork_deterministic"
    return "deferred"  # customer_passthrough / agent_flagged_review / no baseline → Path B (Phase 2)


def find_baseline_source(baseline_ref: str, helix_root: str | Path) -> Optional[tuple[str, str]]:
    """Locate a baseline component's Lit source in helix-code (READ-ONLY). Returns (source, relpath)
    or None. Searches packages/elements/src/{atoms,molecules}/<Name>/<Name>.ts."""
    name = _baseline_name(baseline_ref)
    if not name:
        return None
    root = Path(helix_root)
    for tier in ("atoms", "molecules"):
        cand = root / "packages" / "elements" / "src" / tier / name / f"{name}.ts"
        if cand.is_file():
            return cand.read_text(encoding="utf-8"), str(cand.relative_to(root))
    return None


def fork_component(baseline_source: str, customer_slug: str) -> tuple[str, str, str]:
    """Deterministically fork a baseline Lit component into the customer namespace.

    Returns (forked_source, element_tag, class_name). Transforms:
      1. tag:   every ``hx-`` custom-element prefix → ``{slug}-`` (@customElement + template tags)
      2. class: the exported ``class Hx… extends LitElement`` → PascalCase(customer tag)+"Element"
      3. token: ``var(--helix-…)`` / ``--helix-`` → ``--{slug}-`` (the customer token namespace)
    Deterministic: identical inputs → byte-identical output (no gate needed, Adjustment 1).
    """
    slug = slugify(customer_slug)
    m_tag = _CUSTOM_ELEMENT_RE.search(baseline_source)
    m_cls = _CLASS_RE.search(baseline_source)
    old_tag = m_tag.group(1) if m_tag else "hx-component"
    element_core = old_tag[3:] if old_tag.startswith("hx-") else old_tag
    new_tag = f"{slug}-{element_core}"
    new_class = pascal_case(new_tag) + "Element"

    src = baseline_source
    # 3. tokens first (independent namespace)
    src = src.replace("--helix-", f"--{slug}-")
    # 1. tag prefix everywhere it denotes a custom element
    src = src.replace("hx-", f"{slug}-")
    # 2. exported class rename (only if we found one)
    if m_cls:
        src = re.sub(rf"\b{re.escape(m_cls.group(1))}\b", new_class, src)
    return src, new_tag, new_class


def render_cem(elements: list[dict]) -> str:
    """Minimal deterministic Custom Elements Manifest for the emitted elements.

    A lightweight, byte-stable manifest (schema 1.0.0) listing each element's tag + class + path.
    The full ``cem analyze`` pass (rich attrs from JSDoc) is a helix-code build step; 3d emits this
    structural manifest so the package is self-describing.
    """
    modules = [{
        "kind": "javascript-module",
        "path": e["file_path"],
        "declarations": [{"kind": "class", "name": e["class_name"],
                          "customElement": True, "tagName": e["element_tag"]}],
        "exports": [{"kind": "custom-element-definition", "name": e["element_tag"],
                     "declaration": {"name": e["class_name"], "module": e["file_path"]}}],
    } for e in elements if e.get("element_tag")]
    return json.dumps({"schemaVersion": "1.0.0", "readme": "", "modules": modules},
                      indent=2, sort_keys=True) + "\n"


def render_ccg_provenance_md(customer_slug: str, provenance: dict) -> str:
    """3d's PROVENANCE.md — extends 3c's with per-element code-generation derivation."""
    lines = [
        "# PROVENANCE — component code (3d)",
        "",
        "_Lit + TypeScript component code generated by the HELIX 3d Component Code Generator,",
        "on top of the 3c theme package._",
        "",
        f"- Generator: {provenance.get('generator_version', '0.1.0-3d')}",
        f"- Baseline source ref: `{provenance.get('baseline_source_ref') or 'n/a'}`",
        f"- Model routing: {provenance.get('model_routing') or 'n/a (deterministic-only run)'}",
        "",
        "## Elements",
    ]
    for e in provenance.get("elements", []):
        tag = e.get("element_tag") or "(deferred)"
        ref = f" ← `{e['baseline_ref']}`" if e.get("baseline_ref") else ""
        lines.append(f"- `{e['slot']}` → `{tag}` [{e['path']}, {e['confidence']}]{ref}")
    lines.append("")
    return "\n".join(lines)


def write_package(tree: PackageTree, dest: str | Path) -> str:
    root = Path(dest)
    for relpath, contents in tree.items():
        target = root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")
    return str(root)
