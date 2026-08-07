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
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from agno.workflow.types import StepInput, StepOutput

from agents.overlay_packager import fork_ops, mr_ops, packaging
from agents.overlay_packager import lock as lock_mod
from agents.overlay_packager.token_substitution import (
    CoverageGateError,
    CoverageReport,
    KnownUncovered,
    assert_coverage,
    substitute_token_values,
)

STEP_NAME_TOKEN_SUB = "overlay-packager-token-substitution"
STEP_NAME_PACKAGER = "overlay-packager"

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
    build_gate: BuildGateResult | None = None
    error: str | None = None


def _tail(text: str, n: int = 4000) -> str:
    return text[-n:] if text else ""


def make_scratch_fork(helix_code_root: str, dest_dir: str | None = None) -> str:
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


def run_pnpm_gate(fork_path: str, *pnpm_args: str) -> BuildGateResult:
    """Generic structural gate: `pnpm <pnpm_args>` inside the (scratch or live)
    fork, capturing pass/fail + timing. Same mechanism `build:tokens` proved in
    Phase 2 (MR !51); Phase 3 reuses it verbatim for the other real gates."""
    start = time.monotonic()
    try:
        proc = subprocess.run(
            ["pnpm", *pnpm_args],
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


def run_build_tokens_gate(fork_path: str) -> BuildGateResult:
    """Structural gate: `pnpm build:tokens` inside the (scratch or live) fork."""
    return run_pnpm_gate(fork_path, "build:tokens")


def run_typecheck_gate(fork_path: str) -> BuildGateResult:
    """Structural gate: `pnpm typecheck` inside the (scratch or live) fork."""
    return run_pnpm_gate(fork_path, "typecheck")


def run_build_storybook_gate(fork_path: str) -> BuildGateResult:
    """Structural gate: `pnpm build-storybook` inside the (scratch or live) fork."""
    return run_pnpm_gate(fork_path, "build-storybook")


def run_all_structural_gates(fork_path: str) -> dict[str, BuildGateResult]:
    """Phase 3 step 7: all 4 real gates against the cloned fork, in order.
    `pnpm install --frozen-lockfile` gates the rest — on failure the other
    three are omitted (not attempted against a broken install) rather than
    reported as failed, so the MR body distinguishes "install broke" from
    "install fine, typecheck/tokens/storybook broke"."""
    gates: dict[str, BuildGateResult] = {
        "pnpm install --frozen-lockfile": run_pnpm_gate(fork_path, "install", "--frozen-lockfile"),
    }
    if not gates["pnpm install --frozen-lockfile"].passed:
        return gates
    gates["pnpm typecheck"] = run_typecheck_gate(fork_path)
    gates["pnpm build:tokens"] = run_build_tokens_gate(fork_path)
    gates["pnpm build-storybook"] = run_build_storybook_gate(fork_path)
    return gates


# =========================================================================== #
# Phase 3 — packager workflow entry (the 7-step mechanism against a LIVE fork)
# =========================================================================== #


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _blocking(exc) -> dict:
    """{code, message, remediation} -> the ratified blocking_warning envelope
    (see fork_ops.ForkOpsError / packaging.PackagerInputError / mr_ops.MrOpsError)."""
    return {
        "status": "failure",
        "blocking_warning": {"code": exc.code, "message": exc.message, "remediation": exc.remediation},
    }


def _gate_outcomes(gates: dict[str, BuildGateResult]) -> list[mr_ops.GateOutcome]:
    return [mr_ops.GateOutcome(name=name, passed=g.passed, duration_seconds=g.duration_seconds) for name, g in gates.items()]


def overlay_packager_executor(step_input: StepInput, **kwargs) -> StepOutput:
    """Phase 3 orchestrator. additional_data contract:

      customer_slug        (required — fail-loud if blank, never defaulted)
      baseline_ref          (default "master")
      customer_token_map    (3c output: DTCG leaf-path -> customer $value)
      known_uncovered         (optional FM1 exceptions: {rel_token_file: [{path,reason,owner}]})
      elements                 (Path-A + Path-B combined: [{class_name, file_path}, ...])
      incompatibilities          (flagged design gaps: [{token_path, nearest_neighbor}])
      figma_file_key / figma_revision

    Every failure surfaces as a ``blocking_warning`` (ratified convention) —
    this never raises past its own boundary, so the workflow run always
    returns. Structural-gate / MR failures still complete the run (per the
    ratified "no pipeline-pause" contract for flagged items); only packager
    input/infra errors (blank slug, missing token, GitLab unreachable, gate
    failure) mark the run ``success=False``.
    """
    data = getattr(step_input, "additional_data", None) or {}
    run_id = getattr(step_input, "run_id", None) or str(uuid.uuid4())
    timestamp = _now_iso()

    try:
        slug = packaging.validate_customer_slug(data.get("customer_slug"))
    except packaging.PackagerInputError as exc:
        return StepOutput(step_name=STEP_NAME_PACKAGER, content=_blocking(exc), success=False)

    baseline_ref = data.get("baseline_ref") or "master"
    env = dict(os.environ)

    try:
        run_type = packaging.detect_run_type(slug, env=env)
    except packaging.PackagerInputError as exc:
        return StepOutput(step_name=STEP_NAME_PACKAGER, content=_blocking(exc), success=False)

    token = env[packaging.ENV_TOKEN]
    api_base = packaging._api_base(env[packaging.ENV_REPO_URL])
    subgroup_url = env[packaging.ENV_SUBGROUP_URL]
    subgroup_path = packaging._subgroup_path(subgroup_url)
    project_path = packaging.client_fork_project_path(slug, subgroup_url=subgroup_url)

    figma_source = mr_ops.FigmaSource(
        file_key=data.get("figma_file_key", ""),
        revision=data.get("figma_revision", ""),
    )

    try:
        if run_type == "first_run":
            subgroup_id = fork_ops.get_subgroup_id(subgroup_path, api_base, token)
            fork_result = fork_ops.create_or_fetch_client_fork(slug, baseline_ref, env=env)
            project = fork_ops.create_client_project(slug, subgroup_id, api_base, token)
            fork_ops.seed_client_project(project, fork_ops.DEFAULT_BASELINE_CLEAN_URL, fork_result.resolved_sha, env=env)
            fork_path = fork_ops.clone_or_fetch_existing_fork(project, env=env)
            client_lock = lock_mod.HelixLock(
                customer_slug=slug,
                forked_from=lock_mod.ForkedFrom(repo="msq-turbo/helix-code", ref=baseline_ref, sha=fork_result.resolved_sha),
            )
            source_shifts: list[lock_mod.SourceShift] = []
        else:
            project = fork_ops.get_client_project(project_path, api_base, token)
            fork_path = fork_ops.clone_or_fetch_existing_fork(project, env=env)
            client_lock = lock_mod.read_lock(fork_path)
            organism_sources = {
                el["class_name"]: lock_mod.OrganismSource(source=el.get("source", baseline_ref), sha=el.get("sha", ""))
                for el in data.get("elements", [])
                if el.get("category") == "organisms" and el.get("class_name")
            }
            source_shifts = lock_mod.detect_source_shift(client_lock, organism_sources)
    except (packaging.PackagerInputError, fork_ops.ForkOpsError, lock_mod.LockError) as exc:
        return StepOutput(step_name=STEP_NAME_PACKAGER, content=_blocking(exc), success=False)

    # Step 3: 3c token substitution (FM1 coverage gate) — Phase 2 mechanism, unmodified.
    coverage_failure: str | None = None
    if data.get("customer_token_map"):
        sub_result = apply_token_substitution(
            fork_path,
            data["customer_token_map"],
            known_uncovered={
                rel: [KnownUncovered(**ku) for ku in kus] for rel, kus in (data.get("known_uncovered") or {}).items()
            },
            run_build_gate=False,  # the real gate runs once, below, alongside typecheck/storybook
        )
        if sub_result.status == "failure":
            coverage_failure = sub_result.error

    # Steps 4-5: Path-A + Path-B elements (already-produced package content, copied in).
    elements = data.get("elements", [])
    package_path = data.get("package_path")
    applied_files: list[str] = []
    if package_path and elements:
        applied_files = packaging.apply_generated_elements(fork_path, package_path, elements)

    # Step 6: barrel regen + Phase 4/5 hooks (KNOWN STUB — see packaging.story_gen_hook /
    # cem_analyze_hook docstrings; Phase 4/5 haven't landed yet, forward-reference in the
    # original spec, Desktop-confirmed sequencing bug, amendment 1).
    barrel_added = packaging.regenerate_barrel(fork_path, elements) if elements else []
    story_gen_result = packaging.story_gen_hook(fork_path, elements)
    cem_result = packaging.cem_analyze_hook(fork_path)

    # Step 7: real structural gates.
    gates = run_all_structural_gates(fork_path)
    gates_passed = all(g.passed for g in gates.values())

    # helix-lock.json write/update (FM3) — always, even on a gate failure, so the
    # next re-run still has an accurate prior-state baseline to diff against.
    client_lock.runs.append(
        lock_mod.RunRecord(run_id=run_id, timestamp=timestamp, figma=lock_mod.RunFigma(**vars(figma_source)))
    )
    for el in elements:
        if el.get("category") == "organisms" and el.get("class_name"):
            client_lock.organisms[el["class_name"]] = lock_mod.OrganismSource(
                source=el.get("source", baseline_ref), sha=el.get("sha", "")
            )
    lock_mod.write_lock(fork_path, client_lock)

    incompatibility_comments = [
        mr_ops.render_incompatibility_comment(inc["token_path"], inc["nearest_neighbor"])
        for inc in data.get("incompatibilities", [])
    ]
    shift_comments = [mr_ops.render_source_shift_comment(s) for s in source_shifts]

    result = {
        "status": "success" if gates_passed and coverage_failure is None else "failure",
        "run_id": run_id,
        "timestamp": timestamp,
        "run_type": run_type,
        "project": {"path_with_namespace": project.path_with_namespace, "web_url": project.web_url},
        "applied_files": applied_files,
        "barrel_added": barrel_added,
        "story_gen": story_gen_result,  # None = KNOWN STUB (Phase 4 not landed)
        "cem_analyze": cem_result,      # None = KNOWN STUB (Phase 5 not landed)
        "structural_gates": {name: {"passed": g.passed, "duration_seconds": g.duration_seconds} for name, g in gates.items()},
        "coverage_failure": coverage_failure,
        "incompatibility_comments": incompatibility_comments,
        "source_shift_comments": shift_comments,
        "lock_path": f"{fork_path}/{lock_mod.LOCK_FILENAME}",
    }

    # Step 6/8: stacked MR bundle only against a real GitLab project id (>0) —
    # a fake ClientProject in a unit test (id=0) never opens live MRs.
    if project.id:
        stage_summaries = {stage: f"Run {run_id}: {stage.replace('-', ' ')} changes." for stage in mr_ops.MR_STACK_ORDER}
        stage_gates = {"organisms": _gate_outcomes(gates)}  # gates reported once, on the last (organisms) MR
        try:
            mrs = mr_ops.open_stacked_mr_bundle(
                project.id, run_id, timestamp, figma_source, stage_summaries, stage_gates, api_base, token,
            )
        except mr_ops.MrOpsError as exc:
            result["status"] = "failure"
            result["blocking_warning"] = {"code": exc.code, "message": exc.message, "remediation": exc.remediation}
            return StepOutput(step_name=STEP_NAME_PACKAGER, content=result, success=False)

        result["merge_requests"] = {stage: {"iid": mr.iid, "web_url": mr.web_url} for stage, mr in mrs.items()}
        for comment in [*incompatibility_comments, *shift_comments]:
            mr_ops.post_mr_comment(project.id, mrs["organisms"].iid, comment, api_base, token)

    return StepOutput(step_name=STEP_NAME_PACKAGER, content=result, success=result["status"] == "success")


def apply_token_substitution(
    fork_path: str,
    customer_token_map: dict,
    *,
    known_uncovered: dict[str, list[KnownUncovered]] | None = None,
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
