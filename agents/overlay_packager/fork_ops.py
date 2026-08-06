"""Overlay Packager — fork operation (v0.3 Phase 2, offline-testable subset).

Resolves ``baseline_ref`` to a concrete SHA against helix-code via
``git ls-remote`` — READ-ONLY, same credential-helper pattern as
agents/baseline_reader/step.py (Container Hosting Security Baseline, Rule 5):
the Deploy Token is fed to git via a per-command credential helper reading the
process environment, never appears in argv, and the persisted remote URL stays
credential-free. Actual fork creation (new GitLab project, push seed) is
Phase 3 live execution — out of scope here.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional

ENV_USERNAME = "BASELINE_REPO_USERNAME"
ENV_TOKEN = "BASELINE_REPO_TOKEN"
DEFAULT_BASELINE_CLEAN_URL = "https://rmvc01.rm.udg.de/msq-turbo/helix-code.git"
DEFAULT_FORK_ROOT = "/var/lib/helix/forks"

_GIT_TIMEOUT_SECONDS = 60


class ForkOpsError(Exception):
    """Fail-loud (Phase 2 constraint: never silent on a blank slug or an
    unresolved baseline_ref). Carries a blocking-warning-shaped payload
    ({code, message, remediation}) per the ratified convention, even though
    this path raises rather than returning one."""

    def __init__(self, code: str, message: str, remediation: str):
        self.code = code
        self.message = message
        self.remediation = remediation
        super().__init__(message)


@dataclass
class ForkResult:
    slug: str
    baseline_ref: str
    resolved_sha: str
    fork_path: str


def _credential_helper() -> str:
    """Reads the token from the ENVIRONMENT; contains only var NAMES, never a
    secret. Passed per-command via ``git -c``, never written to .git/config."""
    return "!f() { echo username=$%s; echo password=$%s; }; f" % (ENV_USERNAME, ENV_TOKEN)


def _scrub(text: Optional[str]) -> Optional[str]:
    if not text:
        return text
    out = text
    for var in (ENV_TOKEN, ENV_USERNAME):
        val = os.environ.get(var)
        if val:
            out = out.replace(val, "${%s}" % var)
    return out.strip()


def _default_git_runner(args: list[str], *, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    )


def create_or_fetch_client_fork(
    slug: Optional[str],
    baseline_ref: str,
    *,
    clean_url: str = DEFAULT_BASELINE_CLEAN_URL,
    fork_root: str = DEFAULT_FORK_ROOT,
    env: Optional[dict] = None,
    _git_runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
) -> ForkResult:
    """Phase 2 scope: resolve ``baseline_ref`` -> SHA, return the addressing a
    fork needs. Blank/None ``slug`` fails loud — never defaulted (customer
    identity must be explicit)."""
    if not slug or not slug.strip():
        raise ForkOpsError(
            "FORK_SLUG_BLANK",
            "customer slug is blank/None; refusing to default it.",
            "Pass a concrete customer slug (e.g. from the 3b engagement record).",
        )
    slug = slug.strip()

    runner = _git_runner or _default_git_runner
    run_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}
    cred = ["-c", "credential.helper=%s" % _credential_helper()]
    proc = runner([*cred, "ls-remote", clean_url, baseline_ref], env=run_env)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise ForkOpsError(
            "FORK_BASELINE_REF_UNRESOLVED",
            f"could not resolve baseline_ref '{baseline_ref}' against {clean_url}: "
            f"{_scrub(proc.stderr) or 'no matching ref'}",
            "Verify the ref exists on helix-code and BASELINE_REPO_TOKEN is valid.",
        )
    resolved_sha = proc.stdout.split()[0]
    return ForkResult(
        slug=slug,
        baseline_ref=baseline_ref,
        resolved_sha=resolved_sha,
        fork_path=f"{fork_root}/{slug}",
    )
