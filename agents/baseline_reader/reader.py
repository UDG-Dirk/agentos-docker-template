"""Agent 3a: Baseline Reader (HELIX pipeline Step 3a) — deterministic, no LLM.

Reads a helix-code baseline repository (or any helix-<customer> extension) and
produces a machine-readable inventory of tokens, components (from the Custom
Elements Manifest), Storybook conventions, and known constraints. Downstream
agents (Semantic Matcher 3b, Theme Generator 3c, New Component Scaffolder 3d)
consume this inventory instead of re-parsing raw files. Answers GAP-10.

Design principle: the reader ADAPTS to whatever baseline layout it is pointed
at — it does not assume helix-code's specific layout. All structural paths are
kwargs with defaults matching today's helix-code; heuristic discovery kicks in
when a default is missing; ``meta.resolved_paths`` echoes what was read.

Conventions implemented (first implementer of both):
- ``blocking_warnings`` per helix-poc-agno:decision:blocking-warnings-convention
  ({code, message, remediation}; never raises for these; workflow halts at HITL).
- output persistence per helix-poc-agno:lesson:agent-output-persistence-pattern
  (in-memory primary; opt-in ``output_dir``; symlink pointer with copy fallback).

Read-only on the baseline: never writes there, never runs pnpm, never checks out.
Dependency-free (json, pathlib, subprocess, shutil, os, re, datetime).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

READER_VERSION = "1.0.0"

# Defaults matching today's helix-code layout (all overridable via kwargs).
DEFAULT_CEM_SOURCE = "packages/elements/custom-elements.json"
DEFAULT_TOKEN_SOURCES = "packages/tokens/tokens/"
DEFAULT_STORYBOOK_CONFIG = "packages/storybook/.storybook/"

CSS_VAR_PREFIX = "helix"
NAMING_PATTERN = "--helix-{category}-{variant}-{modifier}"

# Atomic-design buckets, in canonical order.
ATOMIC_BUCKETS = ("atoms", "molecules", "organisms", "templates", "pages")


class BaselineReaderError(Exception):
    """Hard failure (bad path, not-a-git-repo, malformed token JSON).

    Distinct from ``blocking_warnings``: these conditions make the inventory
    meaningless, so the reader raises rather than returning a partial object.
    """


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _blocking(code: str, message: str, remediation: str) -> dict:
    return {"code": code, "message": message, "remediation": remediation}


def _git(repo: Path, *args: str) -> Optional[str]:
    """Run a read-only git command; return stripped stdout or None on failure.

    Never uses shell=True. Never mutates repo state (only rev-parse / status).
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _css_var_name(path: list[str]) -> str:
    """Derive the --helix-* CSS custom-property name from a token path.

    Mirrors helix-code's Style Dictionary transform (packages/tokens/
    style-dictionary.config.js cssVarName): prefix, join with '-', camelCase ->
    kebab, collapse whitespace/underscores to '-', lowercase.
    """
    joined = "-".join(path)
    joined = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", joined)
    joined = re.sub(r"[\s_]+", "-", joined)
    return f"--{CSS_VAR_PREFIX}-" + joined.lower()


def _walk_dtcg(node: Any, path: list[str], out: list[tuple[list[str], dict]]) -> None:
    """Collect DTCG leaves (dicts carrying ``$value``) with their path."""
    if isinstance(node, dict):
        if "$value" in node:
            out.append((path, node))
            return
        for key, child in node.items():
            if key.startswith("$"):
                continue
            _walk_dtcg(child, path + [key], out)


# --------------------------------------------------------------------------- #
# step 4 — tokens
# --------------------------------------------------------------------------- #
def _classify_layer(rel_path: str) -> str:
    p = rel_path.replace("\\", "/")
    if "/primitive/" in p or p.endswith("/primitive"):
        return "primitive"
    if "/semantic-" in p:
        return "semantic"
    return "unknown"


def _read_tokens(repo: Path, token_dir: Path, rel_dir: str,
                 warnings: list) -> dict:
    """Parse every *.tokens.json under ``token_dir`` (behavior step 4)."""
    sources: dict[str, Any] = {}
    css_var_names: set[str] = set()
    categories: dict[str, dict] = {}
    categories_observed: set[str] = set()
    collision_hits: set[str] = set()

    if not token_dir.exists():
        warnings.append(
            f"Token source directory not found: {rel_dir} — tokens inventory empty."
        )
        return {
            "sources": {},
            "css_var_names": [],
            "naming_pattern": NAMING_PATTERN,
            "conventions": {
                "unitless": True,
                "calc_wrap_expected": True,
                "categories_observed": [],
            },
            "categories": {},
        }

    files = sorted(token_dir.rglob("*.tokens.json"))
    if not files:
        warnings.append(f"No *.tokens.json files under {rel_dir}.")

    for f in files:
        rel = f.relative_to(repo).as_posix()
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:  # edge: malformed JSON -> hard fail
            raise BaselineReaderError(
                f"Malformed token JSON in {rel}: {exc}"
            ) from exc

        layer = _classify_layer(rel)
        # group source files by their parent collection dir under the token root
        rel_parent = f.parent.relative_to(token_dir).as_posix() or "."
        sources.setdefault(rel_parent, []).append(rel)

        leaves: list[tuple[list[str], dict]] = []
        _walk_dtcg(data, [], leaves)
        for token_path, leaf in leaves:
            if not token_path:
                continue
            category = token_path[0]
            categories_observed.add(category)
            var = _css_var_name(token_path)
            css_var_names.add(var)
            # -N collision suffix (Token Normalizer emits e.g. body-2.strong)
            for seg in token_path:
                if re.search(r"-\d+$", seg):
                    collision_hits.add(".".join(token_path))
            cat = categories.setdefault(
                category, {"layer": layer, "count": 0, "example_names": []}
            )
            cat["count"] += 1
            if layer != "unknown" and cat["layer"] == "unknown":
                cat["layer"] = layer
            if len(cat["example_names"]) < 3:
                cat["example_names"].append(var)

    if collision_hits:
        warnings.append(
            "Token path-collision suffixes ('-N') detected "
            f"({len(collision_hits)}): {sorted(collision_hits)[:5]}. "
            "Token Normalizer emits these on canonical-path collisions; "
            "downstream consumers keying on names should be aware."
        )

    # deterministic ordering everywhere
    for cat in categories.values():
        cat["example_names"] = sorted(cat["example_names"])
    sources = {k: sorted(v) for k, v in sorted(sources.items())}

    return {
        "sources": sources,
        "css_var_names": sorted(css_var_names),
        "naming_pattern": NAMING_PATTERN,
        "conventions": {
            "unitless": True,
            "calc_wrap_expected": True,
            "categories_observed": sorted(categories_observed),
        },
        "categories": {k: categories[k] for k in sorted(categories)},
    }


# --------------------------------------------------------------------------- #
# step 5 — components (CEM)
# --------------------------------------------------------------------------- #
def _atomic_bucket(module_path: str) -> Optional[str]:
    p = module_path.replace("\\", "/").lower()
    for bucket in ATOMIC_BUCKETS:
        if f"/{bucket}/" in p:
            return bucket
    return None


def _read_cem(repo: Path, cem_path: Path, rel_cem: str,
              blocking_warnings: list, warnings: list) -> dict:
    """Parse the Custom Elements Manifest (behavior step 5).

    Missing CEM -> CEM_MISSING blocking warning + empty inventory (never raise).
    """
    empty = {b: [] for b in ATOMIC_BUCKETS}
    if not cem_path.exists():
        blocking_warnings.append(
            _blocking(
                "CEM_MISSING",
                f"Custom Elements Manifest not found at {rel_cem} (and heuristic "
                "discovery found no candidate). Components inventory is empty; "
                "downstream matching will produce nonsense.",
                "cd <baseline> && pnpm --filter @helix/elements run analyze, "
                "then re-invoke the reader.",
            )
        )
        return {"cem_path": rel_cem, "cem_present": False, **empty}

    try:
        cem = json.loads(cem_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        blocking_warnings.append(
            _blocking(
                "CEM_MALFORMED",
                f"CEM at {rel_cem} is not valid JSON: {exc}. Components inventory "
                "is empty; downstream matching will produce nonsense.",
                "Regenerate it: pnpm --filter @helix/elements run analyze.",
            )
        )
        return {"cem_path": rel_cem, "cem_present": False, **empty}

    buckets: dict[str, list] = {b: [] for b in ATOMIC_BUCKETS}
    for module in cem.get("modules", []):
        mod_path = module.get("path", "")
        for decl in module.get("declarations", []):
            if not decl.get("customElement"):
                continue
            slots = []
            for s in decl.get("slots", []):
                slots.append(s.get("name") or "default")
            entry = {
                "tag": decl.get("tagName", ""),
                "class_name": decl.get("name", ""),
                "path": mod_path,
                "attrs": sorted(
                    a.get("name", "") for a in decl.get("attributes", [])
                ),
                "props": sorted(
                    m.get("name", "")
                    for m in decl.get("members", [])
                    if m.get("kind") == "field" and not m.get("static")
                ),
                "slots": sorted(set(slots)),
                "events": sorted(e.get("name", "") for e in decl.get("events", [])),
                "css_vars_consumed": [],  # v1: parked (spec Q3)
            }
            bucket = _atomic_bucket(mod_path)
            if bucket is None:
                bucket = "atoms"
                warnings.append(
                    f"Component {entry['tag'] or entry['class_name']} at "
                    f"{mod_path} has no atomic-design directory; bucketed as atoms."
                )
            buckets[bucket].append(entry)

    for b in buckets:
        buckets[b] = sorted(buckets[b], key=lambda e: (e["tag"], e["class_name"]))

    return {"cem_path": rel_cem, "cem_present": True, **buckets}


def _cem_staleness_warning(repo: Path, cem_path: Path, warnings: list) -> None:
    """Failure-mode 2: warn if CEM predates any elements/src file.

    NOTE: filesystem mtimes reset to clone time on a fresh git clone, so this
    check is only meaningful in a live working copy. Best-effort, non-fatal.
    """
    src_dir = repo / "packages" / "elements" / "src"
    if not cem_path.exists() or not src_dir.exists():
        return
    try:
        cem_mtime = cem_path.stat().st_mtime
        newest_src = max(
            (p.stat().st_mtime for p in src_dir.rglob("*.ts")), default=0.0
        )
        if newest_src > cem_mtime:
            warnings.append(
                "CEM may be stale: a source file under packages/elements/src/ "
                "is newer than custom-elements.json. Regenerate with "
                "'pnpm --filter @helix/elements run analyze'. (mtime-based; "
                "unreliable on fresh clones where mtimes reset.)"
            )
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# step 6 — storybook
# --------------------------------------------------------------------------- #
def _discover_storybook(repo: Path, warnings: list) -> Optional[Path]:
    """Heuristic: find a live .storybook dir whose main.ts references real paths."""
    candidates = sorted(repo.glob("packages/*/.storybook"))
    valid = []
    for c in candidates:
        main = c / "main.ts"
        if not main.exists():
            continue
        text = main.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"stories:\s*\[([^\]]*)\]", text, re.S)
        # a config is "live" if its stories glob resolves under the package
        pkg = c.parent
        if m and (list(pkg.glob("src/**/*.stories.*")) or "../src" not in m.group(1)):
            valid.append(c)
    if not valid:
        return None
    if len(valid) > 1:
        warnings.append(
            "Multiple candidate Storybook configs: "
            f"{[v.relative_to(repo).as_posix() for v in valid]}; picked first valid."
        )
    return valid[0]


def _read_storybook(repo: Path, sb_dir: Path, rel_sb: str,
                    warnings: list) -> Optional[dict]:
    """Parse Storybook config + enumerate stories (behavior step 6)."""
    main = sb_dir / "main.ts"
    preview = sb_dir / "preview.ts"
    framework = None
    stories_glob = None
    if main.exists():
        main_txt = main.read_text(encoding="utf-8", errors="ignore")
        fm = re.search(r"framework:\s*\{[^}]*name:\s*['\"]([^'\"]+)['\"]", main_txt, re.S)
        if not fm:
            fm = re.search(r"['\"](@storybook/[a-z-]+)['\"]", main_txt)
        framework = fm.group(1) if fm else None
        gm = re.search(r"stories:\s*\[\s*['\"]([^'\"]+)['\"]", main_txt)
        stories_glob = gm.group(1) if gm else None

    # version from the package.json that owns this .storybook dir
    version = None
    pkg_json = sb_dir.parent / "package.json"
    if pkg_json.exists():
        try:
            pj = json.loads(pkg_json.read_text(encoding="utf-8"))
            deps = {**pj.get("dependencies", {}), **pj.get("devDependencies", {})}
            for key in (framework, "storybook", "@storybook/web-components-vite"):
                if key and key in deps:
                    version = deps[key]
                    break
        except json.JSONDecodeError:
            warnings.append(f"Could not parse {pkg_json.relative_to(repo).as_posix()}.")

    # story taxonomy order from preview.ts storySort, else canonical default
    taxonomy = list(ATOMIC_BUCKETS)
    cem_driven = False
    if preview.exists():
        prev_txt = preview.read_text(encoding="utf-8", errors="ignore")
        cem_driven = "setCustomElementsManifest" in prev_txt
        om = re.search(r"order:\s*\[([^\]]*)\]", prev_txt, re.S)
        if om:
            taxonomy = [t.strip().lower() for t in re.findall(r"['\"]([^'\"]+)['\"]", om.group(1))]

    # enumerate stories, grouped by their directory under src/
    existing: dict[str, list[str]] = {}
    src = sb_dir.parent / "src"
    for story in sorted(src.rglob("*.stories.*")) if src.exists() else []:
        group = story.parent.name if story.parent != src else "root"
        name = re.sub(r"\.stories\.[a-z]+$", "", story.name)
        existing.setdefault(group, []).append(name)
    existing = {k: sorted(v) for k, v in sorted(existing.items())}

    convention = ".stories.ts"

    return {
        "config_path": rel_sb,
        "framework": framework,
        "version": version,
        "story_taxonomy_order": taxonomy,
        "existing_stories": existing,
        "story_file_convention": convention,
        "cem_driven_controls": cem_driven,
    }


# --------------------------------------------------------------------------- #
# step 7 — constraints
# --------------------------------------------------------------------------- #
def _detect_constraints(repo: Path, components: dict, warnings: list) -> dict:
    """Detect known baseline constraints from README + code (behavior step 7)."""
    readme_texts = []
    for candidate in (
        repo / "README.md",
        repo / ".github" / "instructions" / "helix-instructions.md",
        repo / ".github" / "copilot-instructions.md",
    ):
        if candidate.exists():
            readme_texts.append(candidate.read_text(encoding="utf-8", errors="ignore"))
    blob = "\n".join(readme_texts)

    override_status = "unknown"
    if re.search(r"override registry", blob, re.I):
        if re.search(r"override registry[^\n]*(open|unresolved|follow-?up)", blob, re.I) \
           or re.search(r"(UNRESOLVED|open follow)", blob):
            override_status = "open"
        else:
            override_status = "present"

    candidates = []
    for cand, needle in (
        ("@lit/context", r"@lit/context|context protocol"),
        ("public property injection", r"property/slot injection|property injection"),
        ("slot injection", r"slot injection"),
    ):
        if re.search(needle, blob, re.I):
            candidates.append(cand)

    # composite reference components = molecules present in the CEM
    composite_refs = sorted(
        e["tag"] for e in components.get("molecules", []) if e.get("tag")
    )
    composite_pattern = (
        "slot + direct light-DOM children" if composite_refs else "unknown"
    )

    # hardcoded px literals in element styles NOT wrapped in calc()/var()
    hardcoded = []
    src = repo / "packages" / "elements" / "src"
    if src.exists():
        for ts in sorted(src.rglob("*.ts")):
            try:
                for i, line in enumerate(ts.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                    m = re.search(r"([a-z-]+)\s*:\s*(\d+px)\s*;", line)
                    if m and "var(" not in line and "calc(" not in line:
                        hardcoded.append({
                            "location": f"{ts.relative_to(repo).as_posix()}:{i}",
                            "value": f"{m.group(1)}: {m.group(2)}",
                            "note": "hardcoded px literal not resolved via a token/calc",
                        })
            except OSError:
                continue
    hardcoded = sorted(hardcoded, key=lambda h: h["location"])

    return {
        "override_registry_status": override_status,
        "override_registry_candidates": candidates,
        "composite_pattern_in_use": composite_pattern,
        "composite_pattern_reference_components": composite_refs,
        "hardcoded_values_detected": hardcoded,
    }


# --------------------------------------------------------------------------- #
# step 9 — output persistence
# --------------------------------------------------------------------------- #
def _write_output(inventory: dict, output_dir: str, commit: str,
                  iso_ts: str, warnings: list) -> None:
    """Opt-in disk write (behavior step 9). Never fails the run."""
    out = Path(output_dir)
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        warnings.append(f"output_dir not creatable ({output_dir}): {exc}. "
                        "In-memory return is unaffected.")
        return

    safe_ts = iso_ts.replace(":", "").replace("-", "").replace(".", "")
    fname = f"baseline-inventory-{commit}-{safe_ts}.json"
    target = out / fname
    payload = json.dumps(inventory, indent=2, sort_keys=True) + "\n"
    try:
        target.write_text(payload, encoding="utf-8")
    except OSError as exc:
        warnings.append(f"output_dir not writable ({output_dir}): {exc}. "
                        "In-memory return is unaffected.")
        return

    pointer = out / "baseline-inventory-latest.json"
    try:
        if pointer.exists() or pointer.is_symlink():
            pointer.unlink()
        os.symlink(target.name, pointer)
    except OSError:
        try:
            shutil.copy2(target, pointer)
            warnings.append("Symlink unsupported here; wrote "
                            "baseline-inventory-latest.json as a COPY, not a link.")
        except OSError as exc:
            warnings.append(f"Could not create latest-pointer: {exc}.")


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #
def read_baseline(
    baseline_repo_path: str,
    ref: Optional[str] = None,
    cem_source: Optional[str] = None,
    token_sources: Optional[str] = None,
    storybook_config: Optional[str] = None,
    output_dir: Optional[str] = None,
    *,
    _now: Optional[datetime] = None,
) -> dict:
    """Read a helix-code baseline and return a JSON-serializable inventory.

    Only ``baseline_repo_path`` is required. ``_now`` is a private test hook to
    freeze the timestamp for byte-identical determinism tests (not part of the
    public contract). See module docstring / spec:baseline-reader-v1.
    """
    warnings: list = []
    blocking_warnings: list = []

    repo = Path(baseline_repo_path)
    if not repo.exists():
        raise BaselineReaderError(
            f"baseline_repo_path does not exist: {baseline_repo_path}"
        )
    if _git(repo, "rev-parse", "--is-inside-work-tree") != "true":
        raise BaselineReaderError(
            f"Not a git repository: {baseline_repo_path}"
        )

    # step 2 — git metadata
    commit = _git(repo, "rev-parse", "--short", "HEAD") or "unknown"
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD") or "unknown"
    dirty = bool(_git(repo, "status", "--porcelain"))
    if ref is not None:
        resolved_ref = _git(repo, "rev-parse", "--short", ref)
        if resolved_ref and resolved_ref != commit:
            warnings.append(
                f"ref '{ref}' ({resolved_ref}) differs from current HEAD "
                f"({commit}). v1 reads the working tree at HEAD (no checkout, "
                "baseline is read-only); metadata reflects HEAD."
            )

    # resolve structural paths (config override -> default -> heuristic)
    rel_cem = cem_source or DEFAULT_CEM_SOURCE
    cem_path = repo / rel_cem
    if not cem_path.exists() and cem_source is None:
        found = sorted(repo.glob("packages/*/custom-elements.json")) or \
            sorted(repo.glob("packages/*/dist/custom-elements.json"))
        if found:
            cem_path = found[0]
            rel_cem = cem_path.relative_to(repo).as_posix()
            warnings.append(f"CEM not at default; discovered {rel_cem}.")

    rel_tokens = token_sources or DEFAULT_TOKEN_SOURCES
    token_dir = repo / rel_tokens

    rel_sb = storybook_config or DEFAULT_STORYBOOK_CONFIG
    sb_dir = repo / rel_sb
    if not sb_dir.exists() and storybook_config is None:
        discovered = _discover_storybook(repo, warnings)
        if discovered is not None:
            sb_dir = discovered
            rel_sb = sb_dir.relative_to(repo).as_posix()
            warnings.append(f"Storybook config not at default; discovered {rel_sb}.")

    # steps 4-7
    tokens = _read_tokens(repo, token_dir, rel_tokens, warnings)
    components = _read_cem(repo, cem_path, rel_cem, blocking_warnings, warnings)
    _cem_staleness_warning(repo, cem_path, warnings)
    storybook = None
    if sb_dir.exists():
        storybook = _read_storybook(repo, sb_dir, rel_sb, warnings)
    else:
        warnings.append(
            f"Storybook config not found ({rel_sb}); storybook section omitted."
        )
    constraints = _detect_constraints(repo, components, warnings)

    now = _now or datetime.now(timezone.utc)
    iso_ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    inventory = {
        "meta": {
            "baseline_repo": repo.name,
            "commit": commit,
            "branch": branch,
            "read_at": iso_ts,
            "reader_version": READER_VERSION,
            "resolved_paths": {
                "cem_source": rel_cem,
                "token_sources": rel_tokens,
                "storybook_config": rel_sb,
            },
            "working_tree_dirty": dirty,
        },
        "tokens": tokens,
        "components": components,
        "storybook": storybook,
        "constraints": constraints,
        "blocking_warnings": blocking_warnings,
        "warnings": warnings,
    }

    # step 9 — opt-in disk write (never fails the run)
    if output_dir is not None:
        _write_output(inventory, output_dir, commit, iso_ts, warnings)

    return inventory
