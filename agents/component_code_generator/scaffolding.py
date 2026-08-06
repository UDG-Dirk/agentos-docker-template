"""3d Component Code Generator — deterministic transforms (Phase 1).

Pure, zero-LLM functions:
  * ``route_element`` — decide Path A (deterministic fork) vs Path B (from-spec, deferred in P1)
    from a 3c element descriptor.
  * ``fork_component`` — the Path-A transform: fork a baseline Lit component's source verbatim
    (Architecture B — tag, class, and ``--helix-*`` token refs are preserved, not renamed; branding
    is a token *value* swap applied later in the fork's Style Dictionary). Byte-identical for
    identical inputs.
  * ``find_baseline_source`` — locate a baseline component's ``.ts`` in the helix-code checkout
    (READ-ONLY) by its ref/name.
  * package scaffolding, minimal CEM, and the 3d PROVENANCE.md extension.

SP-9: the customer scope/slug is a caller argument; nothing hardcoded. helix-code is READ-ONLY.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

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


# D-p1-3: helix-code path conventions to try per branch, in order (organism branches first).
def _path_candidates(name: str) -> list[str]:
    base = "packages/elements/src"
    return [
        f"{base}/organisms/{name}/{name}.ts",   # feature/organisms/* convention
        f"{base}/organisms/{name}.ts",           # feature/modules flat convention
        f"{base}/molecules/{name}/{name}.ts",
        f"{base}/atoms/{name}/{name}.ts",
    ]


def get_configured_fork_branches() -> list[str]:
    """Branch precedence list for Path-A forking (D-p1-1/D-p1-2). From HELIX_CODE_FORK_BRANCHES
    (comma-separated), default ['master']. Master stays default → backwards-compatible with v0.1."""
    raw = os.environ.get("HELIX_CODE_FORK_BRANCHES", "").strip()
    if not raw:
        return ["master"]
    return [b.strip() for b in raw.split(",") if b.strip()]


def is_valid_lit_source(src: str) -> bool:
    """Namespace-AGNOSTIC Lit-validity check for WIP branch assessment (D-p1-4). NOT the customer
    structural gate (that requires customer token namespace, which raw branch source lacks). Just:
    is this a plausibly-complete Lit component (balanced, @customElement, extends LitElement, render)?"""
    if not src:
        return False
    depth = 0
    for ch in src:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and "@customElement" in src and "extends LitElement" in src and "render" in src


def _default_git_show(helix_root: str | Path, branch: str, path: str) -> Optional[str]:
    """READ-ONLY `git show <branch>:<path>` (Q3 α — no checkout, no writes). None on any failure."""
    try:
        r = subprocess.run(["git", "-C", str(helix_root), "show", f"{branch}:{path}"],
                           capture_output=True, text=True, timeout=20)
        return r.stdout if r.returncode == 0 and r.stdout.strip() else None
    except Exception:  # noqa: BLE001
        return None


def find_baseline_source(baseline_ref: str, helix_root: str | Path,
                         branches: Optional[list[str]] = None,
                         git_show: Optional[Callable[[str | Path, str, str], Optional[str]]] = None,
                         ) -> Optional[tuple[str, str]]:
    """Locate a baseline component's Lit source in helix-code (READ-ONLY), branch-aware (Path 1).

    Searches each configured branch (default HELIX_CODE_FORK_BRANCHES / ['master']) across the D-p1-3
    path candidates via ``git show`` (no checkout). D-p1-5: across all (branch, path) hits, returns
    the LARGEST-LOC source (structural-completeness proxy). Returns (source, "branch:path") or None.
    ``git_show`` is injectable for tests. Backwards-compatible: no config → master only.
    """
    name = _baseline_name(baseline_ref)
    if not name:
        return None
    branch_list = branches if branches is not None else get_configured_fork_branches()
    show = git_show or _default_git_show
    best: Optional[tuple[str, str, int]] = None  # (source, ref, loc)
    for branch in branch_list:
        for path in _path_candidates(name):
            src = show(helix_root, branch, path)
            if src:
                loc = len(src.splitlines())
                if best is None or loc > best[2]:
                    best = (src, f"{branch}:{path}", loc)
    return (best[0], best[1]) if best else None


def fork_component(baseline_source: str, customer_slug: str) -> tuple[str, str, str]:
    """Fork a baseline Lit component into a client-fork of helix-code — VERBATIM (Correction #18).

    Returns ``(forked_source, element_tag, class_name)``. Under the ratified **Architecture B**
    (fork-then-overlay, rev.8.5 Ratification 1), customer branding is applied by swapping token
    *values* in the fork's Style Dictionary — NOT by renaming refs. The component is therefore
    copied byte-for-byte and its HELIX identity is PRESERVED:
      * tag   ``hx-…``           — kept (the fork's custom elements stay ``hx-``)
      * class ``Hx… extends LitElement`` — kept (exported class name unchanged)
      * token ``var(--helix-…)`` — kept (the fork's Style Dictionary emits ``--helix-*``; renamed
                                    ``--{slug}-*`` refs would DANGLE — the empirical failure Sascha
                                    caught in the 2026-08-03 FE-DEV review)

    ``customer_slug`` is retained in the signature (callers pass it) but no longer rewrites source:
    it namespaces the *package location*, resolved by the packager (Phase 3), not the component
    internals. Deterministic: identical input → byte-identical output.
    """
    m_tag = _CUSTOM_ELEMENT_RE.search(baseline_source)
    m_cls = _CLASS_RE.search(baseline_source)
    tag = m_tag.group(1) if m_tag else "hx-component"
    cls = m_cls.group(1) if m_cls else pascal_case(tag) + "Element"
    return baseline_source, tag, cls


# Sibling files that live alongside a component in helix-code's own-directory convention and must
# be forked verbatim with it (Correction #18 / Ratification 1): the component's ``types.ts`` and the
# barrel ``index.ts``. Never applied to the flat ``organisms/Name.ts`` convention (whose directory
# holds unrelated shared files, not the component's own siblings).
_SIBLING_FILES = ("types.ts", "index.ts")


def find_sibling_sources(source_ref: str, helix_root: str | Path,
                         git_show: Optional[Callable[[str | Path, str, str], Optional[str]]] = None,
                         branches: Optional[list[str]] = None,
                         ) -> dict[str, str]:
    """Locate a forked component's sibling ``types.ts`` / ``index.ts`` in helix-code (READ-ONLY).

    ``source_ref`` is the provenance ref from ``find_baseline_source`` — either ``"branch:path"``
    or a bare ``path``. Returns ``{filename: source}`` for whichever siblings exist in the SAME
    directory, but only when the component lives in its OWN directory (``Foo/Foo.ts``); the flat
    convention yields no siblings. ``git_show`` is injectable for tests (defaults to READ-ONLY
    ``git show``). Never writes; never raises past ``git_show`` (which swallows failures → None).
    """
    if not source_ref:
        return {}
    if ":" in source_ref:
        branch, path = source_ref.split(":", 1)
        branch_list = [branch]
    else:
        path = source_ref
        branch_list = branches if branches is not None else get_configured_fork_branches()
    p = Path(path)
    if p.stem != p.parent.name:   # own-directory convention only (e.g. MediaText/MediaText.ts)
        return {}
    show = git_show or _default_git_show
    out: dict[str, str] = {}
    for fname in _SIBLING_FILES:
        sib_path = p.parent.joinpath(fname).as_posix()
        for branch in branch_list:
            s = show(helix_root, branch, sib_path)
            if s:
                out[fname] = s
                break
    return out


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
