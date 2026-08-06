"""Overlay Packager — workflow step skeleton (v0.3 Phase 2).

Wires token-value substitution as step 2 of the packager's 7-step mechanism
(Phase 3 fleshes out steps 1, 3-7 against a live GitLab client-fork; this
module is the skeleton those steps attach to). Phase 2 proves the mechanism
against a LOCAL SCRATCH FORK — a temp-dir copy of helix-code — per the cheap-
proof pattern (shared-results:v0-3-cheap-proof-fork-delivery-model-validation-
result-2026-08-03). helix-code itself is READ-ONLY throughout: ``make_scratch_fork``
only ever reads it (shutil.copytree); nothing here writes back to it.

Step 2 mechanism:
  1. Load the fork's packages/tokens/tokens/semantic-*/*.tokens.json files
  2. substitute_token_values(sd_source_tree, customer_token_map) per file
  3. assert_coverage per file (FM1 gate; caller-supplied known_uncovered)
  4. Write the modified JSON back to the fork's token files
  5. Run `pnpm build:tokens` inside the fork (structural gate)
  6. Capture the gate result (pass/fail + timing) for the MR body
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from agents.overlay_packager.token_substitution import (
    CoverageGateError,
    CoverageReport,
    KnownUncovered,
    assert_coverage,
    substitute_token_values,
)

STEP_NAME_TOKEN_SUB = "overlay-packager-token-substitution"

DEFAULT_TOKEN_GLOB = "packages/tokens/tokens/semantic-*/*.tokens.json"
_BUILD_TIMEOUT_SECONDS = 180


@dataclass
class BuildGateResult:
    ran: bool
    passed: bool
    duration_seconds: float = 0.0
    stdout_tail: str = ""
    stderr_tail: str = ""


@dataclass
class TokenSubstitutionResult:
    status: str  # "success" | "failure"
    fork_path: str
    files_written: list[str] = field(default_factory=list)
    coverage: dict[str, CoverageReport] = field(default_factory=dict)
    build_gate: Optional[BuildGateResult] = None
    error: Optional[str] = None


def _tail(text: str, n: int = 4000) -> str:
    return text[-n:] if text else ""


def make_scratch_fork(helix_code_root: str, dest_dir: Optional[str] = None) -> str:
    """Copy helix-code into a scratch temp dir — Phase 2's local stand-in for
    a live GitLab client-fork. Read-only on ``helix_code_root``: source is
    copied, never written to. ``.git``/``dist`` are skipped (not needed for a
    token-file edit + build); every ``node_modules`` dir is SYMLINKED back
    into the source instead of copied (228MB+ at helix-code's current size —
    copying it per scratch fork would make every VT/CI run slow for no
    benefit, since `pnpm build:tokens` only ever reads deps, never writes
    them). The caller still needs a real, already-`pnpm install`-ed
    ``helix_code_root`` for the build gate to succeed."""
    src = Path(helix_code_root)
    dest = Path(dest_dir or tempfile.mkdtemp(prefix="overlay-packager-scratch-"))

    def _ignore(dirpath: str, names: list[str]) -> set[str]:
        return {n for n in names if n in ("node_modules", ".git", "dist")}

    shutil.copytree(src, dest, dirs_exist_ok=True, ignore=_ignore)

    for nm_src in src.rglob("node_modules"):
        if ".git" in nm_src.parts:
            continue
        rel = nm_src.relative_to(src)
        nm_dest = dest / rel
        nm_dest.parent.mkdir(parents=True, exist_ok=True)
        if not nm_dest.exists():
            nm_dest.symlink_to(nm_src, target_is_directory=True)

    return str(dest)


def run_build_tokens_gate(fork_path: str) -> BuildGateResult:
    """Structural gate: `pnpm build:tokens` inside the (scratch or live) fork."""
    start = time.monotonic()
    try:
        proc = subprocess.run(
            ["pnpm", "build:tokens"],
            cwd=fork_path,
            capture_output=True,
            text=True,
            timeout=_BUILD_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return BuildGateResult(
            ran=True,
            passed=False,
            duration_seconds=time.monotonic() - start,
            stderr_tail=_tail(str(exc)),
        )
    return BuildGateResult(
        ran=True,
        passed=proc.returncode == 0,
        duration_seconds=time.monotonic() - start,
        stdout_tail=_tail(proc.stdout),
        stderr_tail=_tail(proc.stderr),
    )


def apply_token_substitution(
    fork_path: str,
    customer_token_map: dict,
    *,
    known_uncovered: Optional[dict[str, list[KnownUncovered]]] = None,
    token_glob: str = DEFAULT_TOKEN_GLOB,
    run_build_gate: bool = True,
) -> TokenSubstitutionResult:
    """Step 2 of the packager mechanism. One ``customer_token_map`` is applied
    across every semantic-*/*.tokens.json file found under ``fork_path``
    (each file's leaves are matched independently — a path present in one
    file but absent from another counts as that file's own unmatched/extra).
    ``known_uncovered`` maps a token file's fork-relative posix path -> its
    annotated FM1 exceptions."""
    root = Path(fork_path)
    files = sorted(root.glob(token_glob))
    if not files:
        return TokenSubstitutionResult(
            status="failure",
            fork_path=fork_path,
            error=f"no token files matched {token_glob!r} under {fork_path}",
        )

    known_uncovered = known_uncovered or {}
    coverage: dict[str, CoverageReport] = {}
    written: list[str] = []
    for f in files:
        rel = f.relative_to(root).as_posix()
        try:
            sd_source_tree = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return TokenSubstitutionResult(
                status="failure",
                fork_path=fork_path,
                error=f"malformed token JSON in {rel}: {exc}",
            )
        modified, report = substitute_token_values(sd_source_tree, customer_token_map)
        try:
            assert_coverage(report, known_uncovered.get(rel))
        except CoverageGateError as exc:
            return TokenSubstitutionResult(
                status="failure",
                fork_path=fork_path,
                coverage={rel: report},
                error=str(exc),
            )
        f.write_text(json.dumps(modified, indent=2) + "\n", encoding="utf-8")
        written.append(rel)
        coverage[rel] = report

    gate = run_build_tokens_gate(fork_path) if run_build_gate else None
    status = "success" if (gate is None or gate.passed) else "failure"
    return TokenSubstitutionResult(
        status=status,
        fork_path=fork_path,
        files_written=written,
        coverage=coverage,
        build_gate=gate,
    )
