"""Overlay Packager — fork operation (v0.3 Phase 2 read-only subset + Phase 3
live write operations).

Phase 2: resolves ``baseline_ref`` to a concrete SHA against helix-code via
``git ls-remote`` — READ-ONLY, same credential-helper pattern as
agents/baseline_reader/step.py (Container Hosting Security Baseline, Rule 5):
the Deploy Token is fed to git via a per-command credential helper reading the
process environment, never appears in argv, and the persisted remote URL stays
credential-free.

Phase 3 adds the WRITE side against the client-forks subgroup
(``msq-turbo/helix-code-clients``), using a distinct credential
(``GITLAB_HELIX_CLIENTS_TOKEN`` / ``HELIX_CLIENTS_GIT_USERNAME``) — never the
helix-code read token — so a leaked client-fork credential can't touch
helix-code, and vice versa (blast-radius isolation, Phase 0 Q0.3). helix-code
itself stays READ-ONLY throughout: seeding clones + checks out a SHA, then
discards that clone's history to build the client-fork's own first commit;
nothing is ever pushed back to helix-code.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx

ENV_USERNAME = "BASELINE_REPO_USERNAME"
ENV_TOKEN = "BASELINE_REPO_TOKEN"
DEFAULT_BASELINE_CLEAN_URL = "https://rmvc01.rm.udg.de/msq-turbo/helix-code.git"
DEFAULT_FORK_ROOT = "/var/lib/helix/forks"

ENV_CLIENTS_USERNAME = "HELIX_CLIENTS_GIT_USERNAME"
ENV_CLIENTS_TOKEN = "GITLAB_HELIX_CLIENTS_TOKEN"
DEFAULT_CLIENTS_REPO_ROOT = "/var/lib/helix/clients"

_GIT_TIMEOUT_SECONDS = 60
_API_TIMEOUT_SECONDS = 20.0


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


def _scrub(text: str | None) -> str | None:
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
    slug: str | None,
    baseline_ref: str,
    *,
    clean_url: str = DEFAULT_BASELINE_CLEAN_URL,
    fork_root: str = DEFAULT_FORK_ROOT,
    env: dict | None = None,
    _git_runner: Callable[..., subprocess.CompletedProcess] | None = None,
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


# =========================================================================== #
# Phase 3 — live write operations against the client-forks subgroup
# =========================================================================== #


@dataclass
class ClientProject:
    id: int
    path_with_namespace: str
    http_url_to_repo: str
    web_url: str


def _clients_credential_helper() -> str:
    return "!f() { echo username=$%s; echo password=$%s; }; f" % (ENV_CLIENTS_USERNAME, ENV_CLIENTS_TOKEN)


def _clients_scrub(text: str | None) -> str | None:
    if not text:
        return text
    out = text
    for var in (ENV_CLIENTS_TOKEN, ENV_CLIENTS_USERNAME):
        val = os.environ.get(var)
        if val:
            out = out.replace(val, "${%s}" % var)
    return out.strip()


def get_subgroup_id(subgroup_path: str, api_base: str, token: str, *, client: httpx.Client | None = None) -> int:
    """Resolve the client-forks subgroup's numeric id (needed as
    ``namespace_id`` on project create — GitLab has no create-by-path form)."""
    own = client is None
    c = client or httpx.Client(timeout=_API_TIMEOUT_SECONDS)
    try:
        resp = c.get(f"{api_base}/api/v4/groups/{quote(subgroup_path, safe='')}", headers={"PRIVATE-TOKEN": token})
    finally:
        if own:
            c.close()
    if resp.status_code != 200:
        raise ForkOpsError(
            "FORK_SUBGROUP_LOOKUP_FAILED",
            f"could not resolve subgroup {subgroup_path!r} id: {resp.status_code} {_clients_scrub(resp.text)}",
            "Verify the client-forks subgroup exists and the token can read it.",
        )
    return resp.json()["id"]


def get_client_project(
    project_path: str, api_base: str, token: str, *, client: httpx.Client | None = None
) -> ClientProject:
    """``GET /api/v4/projects/{project_path}`` — re_run path, once
    ``packaging.detect_run_type`` has already confirmed the project exists."""
    own = client is None
    c = client or httpx.Client(timeout=_API_TIMEOUT_SECONDS)
    try:
        resp = c.get(f"{api_base}/api/v4/projects/{quote(project_path, safe='')}", headers={"PRIVATE-TOKEN": token})
    finally:
        if own:
            c.close()
    if resp.status_code != 200:
        raise ForkOpsError(
            "FORK_PROJECT_LOOKUP_FAILED",
            f"could not fetch existing client project {project_path!r}: {resp.status_code} {_clients_scrub(resp.text)}",
            "Verify the project exists and the token can read it.",
        )
    body = resp.json()
    return ClientProject(
        id=body["id"],
        path_with_namespace=body["path_with_namespace"],
        http_url_to_repo=body["http_url_to_repo"],
        web_url=body["web_url"],
    )


def create_client_project(
    slug: str, subgroup_id: int, api_base: str, token: str, *, client: httpx.Client | None = None
) -> ClientProject:
    """``POST /api/v4/projects`` — first-run only. ``initialize_with_readme``
    is False: the seed commit (see ``seed_client_project``) is the project's
    first commit, not GitLab's auto-generated README."""
    own = client is None
    c = client or httpx.Client(timeout=_API_TIMEOUT_SECONDS)
    try:
        resp = c.post(
            f"{api_base}/api/v4/projects",
            headers={"PRIVATE-TOKEN": token},
            json={
                "path": slug,
                "namespace_id": subgroup_id,
                "initialize_with_readme": False,
                "visibility": "private",
            },
        )
    finally:
        if own:
            c.close()
    if resp.status_code not in (200, 201):
        raise ForkOpsError(
            "FORK_PROJECT_CREATE_FAILED",
            f"GitLab project create for slug {slug!r} failed: {resp.status_code} {_clients_scrub(resp.text)}",
            "Verify the client-forks token has Maintainer+ role and api scope on the subgroup.",
        )
    body = resp.json()
    return ClientProject(
        id=body["id"],
        path_with_namespace=body["path_with_namespace"],
        http_url_to_repo=body["http_url_to_repo"],
        web_url=body["web_url"],
    )


def seed_client_project(
    project: ClientProject,
    clean_url: str,
    resolved_sha: str,
    *,
    scratch_root: str | None = None,
    env: dict | None = None,
    _git_runner: Callable[..., subprocess.CompletedProcess] | None = None,
) -> None:
    """Snapshot helix-code@``resolved_sha`` into a fresh single-commit history
    and push it to the client-fork's ``main`` as its first commit. helix-code
    is only ever cloned + checked out (read); the clone's own ``.git`` is then
    discarded so the client-fork starts its own history rather than inheriting
    helix-code's full commit log. Only the client-fork remote is written to,
    via the write-only credential helper (never the helix-code read token)."""
    runner = _git_runner or _default_git_runner
    run_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}
    read_cred = ["-c", "credential.helper=%s" % _credential_helper()]
    write_cred = ["-c", "credential.helper=%s" % _clients_credential_helper()]

    scratch = Path(scratch_root or tempfile.mkdtemp(prefix="overlay-packager-seed-"))
    clone_proc = runner([*read_cred, "clone", "--no-checkout", clean_url, str(scratch)], env=run_env)
    if clone_proc.returncode != 0:
        raise ForkOpsError(
            "FORK_SEED_CLONE_FAILED",
            f"could not clone {clean_url} for seeding: {_scrub(clone_proc.stderr)}",
            "Verify BASELINE_REPO_TOKEN is valid and helix-code is reachable.",
        )
    checkout_proc = runner(["-C", str(scratch), "checkout", resolved_sha], env=run_env)
    if checkout_proc.returncode != 0:
        raise ForkOpsError(
            "FORK_SEED_CHECKOUT_FAILED",
            f"could not checkout {resolved_sha} in seed clone: {_scrub(checkout_proc.stderr)}",
            "Verify resolved_sha exists on helix-code.",
        )
    shutil.rmtree(scratch / ".git")
    runner(["-C", str(scratch), "init", "-b", "main"], env=run_env)
    runner(["-C", str(scratch), "add", "-A"], env=run_env)
    commit_proc = runner(["-C", str(scratch), "commit", "-m", f"seed: helix-code@{resolved_sha}"], env=run_env)
    if commit_proc.returncode != 0:
        raise ForkOpsError(
            "FORK_SEED_COMMIT_FAILED",
            f"could not create seed commit: {_scrub(commit_proc.stderr)}",
            "Investigate the git failure above.",
        )
    push_proc = runner(
        [*write_cred, "-C", str(scratch), "push", project.http_url_to_repo, "main"],
        env=run_env,
    )
    if push_proc.returncode != 0:
        raise ForkOpsError(
            "FORK_SEED_PUSH_FAILED",
            f"could not push seed commit to {project.path_with_namespace}: {_clients_scrub(push_proc.stderr)}",
            f"Verify {ENV_CLIENTS_TOKEN} has write access to {project.path_with_namespace}.",
        )


def clone_or_fetch_existing_fork(
    project: ClientProject,
    *,
    cache_root: str = DEFAULT_CLIENTS_REPO_ROOT,
    env: dict | None = None,
    _git_runner: Callable[..., subprocess.CompletedProcess] | None = None,
) -> str:
    """re_run path: clone the client-fork into the local cache if absent, else
    ``fetch --depth 1`` + hard-reset to ``origin/main``. The cache is
    disposable (Phase 0: never authoritative — ``helix-lock.json`` is)."""
    runner = _git_runner or _default_git_runner
    run_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}
    write_cred = ["-c", "credential.helper=%s" % _clients_credential_helper()]
    local_path = Path(cache_root) / project.path_with_namespace.split("/")[-1]

    if (local_path / ".git").exists():
        fetch_proc = runner([*write_cred, "-C", str(local_path), "fetch", "--depth", "1", "origin"], env=run_env)
        if fetch_proc.returncode != 0:
            raise ForkOpsError(
                "FORK_REFRESH_FETCH_FAILED",
                f"could not fetch existing client-fork cache at {local_path}: {_clients_scrub(fetch_proc.stderr)}",
                f"Verify {ENV_CLIENTS_TOKEN} still has read access to {project.path_with_namespace}.",
            )
        reset_proc = runner([*write_cred, "-C", str(local_path), "reset", "--hard", "origin/main"], env=run_env)
        if reset_proc.returncode != 0:
            raise ForkOpsError(
                "FORK_REFRESH_RESET_FAILED",
                f"could not reset cache to origin/main: {_clients_scrub(reset_proc.stderr)}",
                "Investigate the git failure above.",
            )
        return str(local_path)

    local_path.parent.mkdir(parents=True, exist_ok=True)
    clone_proc = runner([*write_cred, "clone", project.http_url_to_repo, str(local_path)], env=run_env)
    if clone_proc.returncode != 0:
        raise ForkOpsError(
            "FORK_CLONE_FAILED",
            f"could not clone existing client-fork {project.path_with_namespace}: {_clients_scrub(clone_proc.stderr)}",
            f"Verify {ENV_CLIENTS_TOKEN} has read access to {project.path_with_namespace}.",
        )
    return str(local_path)
