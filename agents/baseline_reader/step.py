"""
Agno Workflow Step wrapper for Agent 3a (Baseline Reader)
=========================================================

Wires the deterministic ``read_baseline`` function (spec:baseline-reader-v1) into
the HELIX Workflow as a parallel branch alongside Extract -> Normalize. The reader
itself is READ-ONLY and reads the working tree at HEAD; it does not fetch or
checkout. Pull-on-invocation (decision:cycle-2-wiring-2026-07-21) therefore lives
here, in the Step wrapper, NOT in reader.py (which stays authoritative/unmodified).

Flow per invocation:
  1. Resolve ``baseline_ref`` from the Workflow run's ``additional_data`` (default "master").
  2. Ensure the baseline is checked out at that ref inside a Coolify PERSISTENT
     VOLUME (``BASELINE_REPO_PATH``, default ``/var/lib/helix/baseline``):
       - empty volume -> shallow clone; else -> ``fetch --depth 1`` + checkout.
  3. Call ``read_baseline`` on the checkout, persisting the inventory to
     ``BASELINE_OUTPUT_DIR`` (output_persistence per spec) and returning it
     in-memory as the Step's content (workflow-state primary).

Credential handling (Container Hosting Security Baseline, Rule 5):
  The Deploy Token (``BASELINE_REPO_USERNAME`` / ``BASELINE_REPO_TOKEN``) is fed to
  git via a per-command credential helper that reads the process ENVIRONMENT. The
  token never appears in argv, and the remote URL persisted in ``.git/config`` is
  credential-free (the clean ``BASELINE_REPO_URL``). The token is never logged,
  never written to any file, never embedded in a stored URL.

Non-adjacent access (3b criterion 7): a downstream smoke-test Step reads this
Step's output by name via ``StepInput.get_step_output("baseline-read")`` across the
Parallel boundary — the exact mechanism 3b/3c/3d will use.
"""

from __future__ import annotations

import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from agno.workflow.types import StepInput, StepOutput

from agents.baseline_reader.reader import BaselineReaderError, read_baseline

# --- canonical names (3b criterion 7 sub-item i: Step name pin) --------------
STEP_NAME_BASELINE = "baseline-read"
STEP_NAME_SMOKE = "baseline-access-smoke-test"

# --- configuration (env-driven) ----------------------------------------------
# helix-code's default branch is `master` (verified: remote HEAD -> refs/heads/
# master; `main` does not exist). The Cycle 2 decision said "main" but that was a
# wrong assumption about the baseline's mainline — corrected here to match reality.
# Override per-invocation via additional_data={"baseline_ref": "..."}.
DEFAULT_BASELINE_REF = "master"
DEFAULT_BASELINE_REPO_PATH = "/var/lib/helix/baseline"
DEFAULT_BASELINE_OUTPUT_DIR = "/var/lib/helix/inventory"
DEFAULT_BASELINE_CLEAN_URL = "https://rmvc01.rm.udg.de/msq-turbo/helix-code.git"

ENV_USERNAME = "BASELINE_REPO_USERNAME"
ENV_TOKEN = "BASELINE_REPO_TOKEN"
ENV_REPO_PATH = "BASELINE_REPO_PATH"
ENV_OUTPUT_DIR = "BASELINE_OUTPUT_DIR"
ENV_CLEAN_URL = "BASELINE_REPO_URL"  # credential-free remote URL (override-able)
# Cycle 2 (decision 1b): the CEM is built by a SEPARATE CI job (pnpm analyze:flat) and delivered to
# the container — it is NOT committed to helix-code. Point the reader at that CI-built CEM via this
# env var (absolute path recommended; SP-9 env-only, no hardcoded default). Unset -> reader uses its
# in-repo default and, finding no committed CEM, emits CEM_MISSING (fail-loud) as before.
ENV_CEM_PATH = "HELIX_CODE_CEM_PATH"

_GIT_TIMEOUT_SECONDS = 180


def _cem_health() -> dict:
    """Cycle 3 minimal health: size + last-fetch of the CI-delivered CEM at HELIX_CODE_CEM_PATH.
    `last_cem_fetch_timestamp` = the file's mtime (the startup fetch wrote it), None if no CEM;
    `cem_size_bytes` = its size, 0 if CEM_MISSING. Present on every response, incl. blocking ones."""
    p = os.environ.get(ENV_CEM_PATH)
    if p and os.path.isfile(p):
        try:
            st = os.stat(p)
            ts = datetime.fromtimestamp(st.st_mtime, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            return {"last_cem_fetch_timestamp": ts, "cem_size_bytes": st.st_size}
        except OSError:
            pass
    return {"last_cem_fetch_timestamp": None, "cem_size_bytes": 0}


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------
def _resolve_ref(step_input: StepInput) -> str:
    """baseline_ref from the run's additional_data, defaulting to 'master'."""
    data = getattr(step_input, "additional_data", None) or {}
    ref = data.get("baseline_ref") if isinstance(data, dict) else None
    return ref.strip() if isinstance(ref, str) and ref.strip() else DEFAULT_BASELINE_REF


def _credential_helper() -> str:
    """A git credential helper that reads the token from the ENVIRONMENT.

    Contains only env-var NAMES, never a secret, and is passed per-command via
    ``git -c`` so it is never written to .git/config. git runs it via ``sh``.
    """
    return "!f() { echo username=$%s; echo password=$%s; }; f" % (ENV_USERNAME, ENV_TOKEN)


def _scrub(text: str | None) -> str | None:
    """Defensive: replace any accidental credential material in surfaced text."""
    if not text:
        return text
    out = text
    for var in (ENV_TOKEN, ENV_USERNAME):
        val = os.environ.get(var)
        if val:
            out = out.replace(val, "${%s}" % var)
    return out.strip()


def _run_git(args: list[str], *, cwd: str | None = None, env: dict | None = None):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    )


def ensure_baseline_checkout(
    repo_path: str,
    ref: str,
    *,
    clean_url: str,
    env: dict,
) -> tuple[float, str | None]:
    """Clone (if empty) then fetch+checkout ``ref`` at depth 1.

    Credentials come from a per-command credential helper reading ``env``; the
    persisted remote URL stays credential-free. Returns (duration_seconds, error).
    ``error`` is None on success, else a credential-scrubbed message.
    """
    repo = Path(repo_path)
    cred = ["-c", "credential.helper=%s" % _credential_helper()]
    start = time.monotonic()
    try:
        if not (repo / ".git").exists():
            repo.mkdir(parents=True, exist_ok=True)
            p = _run_git([*cred, "clone", "--depth", "1", clean_url, str(repo)], env=env)
            if p.returncode != 0:
                return time.monotonic() - start, _scrub(p.stderr) or "git clone failed"
        # Fetch the requested ref (branch / tag / SHA) shallowly.
        p = _run_git(
            [*cred, "-C", str(repo), "fetch", "--depth", "1", "origin", ref],
            env=env,
        )
        if p.returncode != 0:
            return time.monotonic() - start, _scrub(p.stderr) or "git fetch failed"
        # Detached checkout of what we just fetched. reader.py reads HEAD.
        p = _run_git(["-C", str(repo), "checkout", "--force", "FETCH_HEAD"], env=env)
        if p.returncode != 0:
            return time.monotonic() - start, _scrub(p.stderr) or "git checkout failed"
    except (subprocess.TimeoutExpired, OSError) as e:
        return time.monotonic() - start, _scrub(str(e))
    return time.monotonic() - start, None


def _blocking_result(ref: str, repo_path: str, code: str, message: str,
                     remediation: str, extra_meta: dict | None = None) -> dict:
    meta = {"baseline_ref": ref, "baseline_repo_path": repo_path}
    if extra_meta:
        meta.update(extra_meta)
    meta.update(_cem_health())  # Cycle 3: health fields present even on blocking results
    return {
        "meta": meta,
        "blocking_warnings": [{"code": code, "message": message, "remediation": remediation}],
        "warnings": [],
    }


# ---------------------------------------------------------------------------
# Step executor — Agent 3a
# ---------------------------------------------------------------------------
def baseline_read_executor(step_input: StepInput, **kwargs) -> StepOutput:
    """Pull-on-invocation baseline read. Errors surface as blocking_warnings
    (per the ratified convention) rather than raising, so the parallel branch
    stays observable and the workflow can halt at the HITL gate."""
    ref = _resolve_ref(step_input)
    repo_path = os.environ.get(ENV_REPO_PATH, DEFAULT_BASELINE_REPO_PATH)
    output_dir = os.environ.get(ENV_OUTPUT_DIR, DEFAULT_BASELINE_OUTPUT_DIR)
    clean_url = os.environ.get(ENV_CLEAN_URL, DEFAULT_BASELINE_CLEAN_URL)

    if not os.environ.get(ENV_USERNAME) or not os.environ.get(ENV_TOKEN):
        return StepOutput(
            step_name=STEP_NAME_BASELINE,
            content=_blocking_result(
                ref, repo_path, "BASELINE_AUTH_MISSING",
                f"{ENV_USERNAME}/{ENV_TOKEN} not set; cannot fetch baseline from rmvc01.",
                "Configure the helix-code Deploy Token env vars in Coolify (masked). "
                "Token expiry 2026-11-03.",
            ),
            success=False,
        )

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    duration, err = ensure_baseline_checkout(repo_path, ref, clean_url=clean_url, env=env)
    if err is not None:
        return StepOutput(
            step_name=STEP_NAME_BASELINE,
            content=_blocking_result(
                ref, repo_path, "BASELINE_FETCH_FAILED",
                f"git fetch/checkout of ref '{ref}' failed: {err}",
                "Verify BASELINE_REPO_TOKEN validity (expiry 2026-11-03), rmvc01 "
                "reachability from the container, and that git is installed in the image.",
                extra_meta={"fetch_checkout_seconds": round(duration, 3)},
            ),
            success=False,
        )

    # Cycle 2: consume the CI-built CEM when configured (absolute path wins in `repo / cem_source`);
    # unset -> None -> reader's in-repo default + heuristic (CEM_MISSING loud if truly absent).
    cem_source = os.environ.get(ENV_CEM_PATH) or None
    try:
        inventory = read_baseline(baseline_repo_path=repo_path, ref=ref,
                                  cem_source=cem_source, output_dir=output_dir)
    except BaselineReaderError as e:
        return StepOutput(
            step_name=STEP_NAME_BASELINE,
            content=_blocking_result(
                ref, repo_path, "BASELINE_READ_ERROR", str(e),
                "Inspect baseline layout / resolved paths; see spec:baseline-reader-v1.",
                extra_meta={"fetch_checkout_seconds": round(duration, 3)},
            ),
            success=False,
        )

    inventory.setdefault("meta", {})
    inventory["meta"]["baseline_ref"] = ref
    inventory["meta"]["baseline_repo_path"] = repo_path
    inventory["meta"].update(_cem_health())  # Cycle 3: last_cem_fetch_timestamp + cem_size_bytes
    inventory["meta"]["fetch_checkout_seconds"] = round(duration, 3)
    blocking = inventory.get("blocking_warnings") or []
    return StepOutput(step_name=STEP_NAME_BASELINE, content=inventory, success=not blocking)


# ---------------------------------------------------------------------------
# Step executor — non-adjacent access smoke test (Stage 6 / 3b criterion 7)
# ---------------------------------------------------------------------------
def baseline_access_smoke_test_executor(step_input: StepInput, **kwargs) -> StepOutput:
    """Read 3a's output from Workflow state BY NAME across the Parallel boundary
    (the pattern 3b/3c/3d will use), assert shape, and pass the normalized tokens
    through as the workflow's terminal content so nothing upstream is lost."""
    baseline_out = step_input.get_step_output(STEP_NAME_BASELINE)
    normalize_content = step_input.get_step_content("normalize")

    inv = getattr(baseline_out, "content", None) if baseline_out is not None else None
    is_map = isinstance(inv, dict)
    checks = {
        "baseline_output_reachable": baseline_out is not None,
        "baseline_content_is_mapping": is_map,
        "has_meta": is_map and isinstance(inv.get("meta"), dict),
        "has_tokens_or_components": is_map and ("tokens" in inv or "components" in inv),
        "blocking_warnings_empty": is_map and not (inv.get("blocking_warnings") or []),
    }
    # non-adjacent access itself is the thing being proven; a blocking baseline
    # (e.g. missing auth in a partial env) still proves the access mechanism.
    access_proven = checks["baseline_output_reachable"] and checks["baseline_content_is_mapping"]
    shape_ok = access_proven and checks["has_meta"] and checks["has_tokens_or_components"]

    meta = inv.get("meta", {}) if is_map else {}
    summary = {
        "smoke_test": "baseline non-adjacent access",
        "access_pattern": f"StepInput.get_step_output('{STEP_NAME_BASELINE}') across Parallel boundary",
        "access_proven": access_proven,
        "shape_ok": shape_ok,
        "checks": checks,
        "baseline_ref": meta.get("baseline_ref"),
        "baseline_repo_path": meta.get("baseline_repo_path"),
        "fetch_checkout_seconds": meta.get("fetch_checkout_seconds"),
    }
    return StepOutput(
        step_name=STEP_NAME_SMOKE,
        content={"baseline_access_smoke": summary, "normalized": normalize_content},
        success=access_proven,
    )
