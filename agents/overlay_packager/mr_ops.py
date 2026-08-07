"""Overlay Packager — stacked MR bundle + MR comments (v0.3 Phase 3, FM2).

Opens the 4-MR stack (scaffolding+tokens / atoms / molecules / organisms) in
dependency order against a client-fork project, each declaring its
predecessor branch explicitly in the MR body. Also renders the two ratified
comment formats: flagged design incompatibilities (posted inline, no
pipeline-pause) and FM3 source-shift drift (posted on the organism-hosting
MR). Uses the same write credential as ``fork_ops.py``'s client-fork
functions (``GITLAB_HELIX_CLIENTS_TOKEN``) — never the helix-code read token.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from agents.overlay_packager.lock import SourceShift

_API_TIMEOUT_SECONDS = 20.0

MR_STACK_ORDER = ("scaffolding-and-tokens", "atoms", "molecules", "organisms")


class MrOpsError(Exception):
    """Fail-loud: any GitLab REST call in the MR bundle failed. Carries the
    {code, message, remediation} blocking-warning payload (ratified convention
    — see fork_ops.ForkOpsError)."""

    def __init__(self, code: str, message: str, remediation: str):
        self.code = code
        self.message = message
        self.remediation = remediation
        super().__init__(message)


@dataclass
class GateOutcome:
    name: str
    passed: bool
    duration_seconds: float


@dataclass
class FigmaSource:
    file_key: str
    revision: str


@dataclass
class MergeRequest:
    iid: int
    web_url: str


def branch_name(stage: str, run_id: str) -> str:
    return f"feat/{stage}-run-{run_id}"


def render_mr_description(
    *,
    change_summary: str,
    run_id: str,
    timestamp: str,
    figma_source: FigmaSource,
    predecessor_branch: str | None,
    gates: list[GateOutcome],
) -> str:
    """Ratified format (task Phase 3 step 7). ``predecessor_branch`` is None
    only for MR1 (base of the stack, targets the client-fork's main)."""
    gate_lines = "\n".join(
        f"- `{g.name}`: {'✓' if g.passed else '✗'} ({g.duration_seconds:.0f}s)" for g in gates
    )
    predecessor = predecessor_branch or "(none — base of the stack)"
    return (
        "## Change summary\n"
        f"{change_summary}\n\n"
        "## Provenance\n"
        f"- HELIX run_id: {run_id}\n"
        f"- Timestamp: {timestamp}\n"
        f"- Figma source: {figma_source.file_key}@{figma_source.revision}\n"
        f"- Predecessor MR: {predecessor}\n\n"
        "## Structural gates\n"
        f"{gate_lines}\n"
    )


def render_incompatibility_comment(token_path: str, nearest_neighbor: str) -> str:
    """Ratified format (task Phase 3 step 8). Posted, never pipeline-pausing —
    the workflow completes regardless of how many of these are posted."""
    return (
        f"Token `{token_path}` in customer Figma has no HELIX-compatible mapping; "
        f"using nearest neighbor `{nearest_neighbor}`. Please review."
    )


def render_source_shift_comment(shift: SourceShift) -> str:
    """Ratified format (task Phase 3 step 10, FM3)."""
    return (
        f"{shift.organism} source shifted from `{shift.previous}` to `{shift.current}` "
        "— review substantive changes."
    )


def _post(client: httpx.Client, method: str, url: str, token: str, **kwargs) -> httpx.Response:
    try:
        return client.request(method, url, headers={"PRIVATE-TOKEN": token}, **kwargs)
    except httpx.HTTPError as exc:
        raise MrOpsError(
            "MR_GITLAB_UNREACHABLE",
            f"could not reach GitLab at {url}: {exc}",
            "Verify HELIX_CODE_REPO_URL and network access.",
        ) from exc


def create_branch(
    project_id: int, branch: str, ref: str, api_base: str, token: str, *, client: httpx.Client | None = None
) -> None:
    own = client is None
    c = client or httpx.Client(timeout=_API_TIMEOUT_SECONDS)
    try:
        resp = _post(
            c, "POST", f"{api_base}/api/v4/projects/{project_id}/repository/branches", token,
            params={"branch": branch, "ref": ref},
        )
    finally:
        if own:
            c.close()
    if resp.status_code not in (200, 201):
        raise MrOpsError(
            "MR_BRANCH_CREATE_FAILED",
            f"could not create branch {branch!r} off {ref!r}: {resp.status_code} {resp.text[:500]}",
            "Verify the ref exists and the token has write access.",
        )


def open_merge_request(
    project_id: int,
    source_branch: str,
    target_branch: str,
    title: str,
    description: str,
    api_base: str,
    token: str,
    *,
    client: httpx.Client | None = None,
) -> MergeRequest:
    own = client is None
    c = client or httpx.Client(timeout=_API_TIMEOUT_SECONDS)
    try:
        resp = _post(
            c, "POST", f"{api_base}/api/v4/projects/{project_id}/merge_requests", token,
            json={
                "source_branch": source_branch,
                "target_branch": target_branch,
                "title": title,
                "description": description,
            },
        )
    finally:
        if own:
            c.close()
    if resp.status_code not in (200, 201):
        raise MrOpsError(
            "MR_CREATE_FAILED",
            f"could not open MR {source_branch}->{target_branch}: {resp.status_code} {resp.text[:500]}",
            "Verify both branches exist and the token has write access.",
        )
    body = resp.json()
    return MergeRequest(iid=body["iid"], web_url=body["web_url"])


def post_mr_comment(
    project_id: int, mr_iid: int, body_text: str, api_base: str, token: str, *, client: httpx.Client | None = None
) -> None:
    own = client is None
    c = client or httpx.Client(timeout=_API_TIMEOUT_SECONDS)
    try:
        resp = _post(
            c, "POST", f"{api_base}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/notes", token,
            json={"body": body_text},
        )
    finally:
        if own:
            c.close()
    if resp.status_code not in (200, 201):
        raise MrOpsError(
            "MR_COMMENT_FAILED",
            f"could not post comment on MR !{mr_iid}: {resp.status_code} {resp.text[:500]}",
            "Verify the MR exists and the token has write access.",
        )


def open_stacked_mr_bundle(
    project_id: int,
    run_id: str,
    timestamp: str,
    figma_source: FigmaSource,
    stage_summaries: dict[str, str],
    stage_gates: dict[str, list[GateOutcome]],
    api_base: str,
    token: str,
    *,
    target_branch: str = "main",
    client: httpx.Client | None = None,
) -> dict[str, MergeRequest]:
    """Opens MR1..MR4 in dependency order (FM2): each stage's branch is
    created off the PREVIOUS stage's branch (MR1 off ``target_branch``), and
    each MR targets that same predecessor branch, explicitly named in its
    body. Assumes each stage's commits are already pushed to its branch (the
    packager's 7-step mechanism, upstream of this call) — this function only
    wires branch-create + MR-open + predecessor-declaration."""
    own = client is None
    c = client or httpx.Client(timeout=_API_TIMEOUT_SECONDS)
    mrs: dict[str, MergeRequest] = {}
    try:
        predecessor_branch: str | None = None
        for stage in MR_STACK_ORDER:
            branch = branch_name(stage, run_id)
            base_ref = predecessor_branch or target_branch
            create_branch(project_id, branch, base_ref, api_base, token, client=c)
            description = render_mr_description(
                change_summary=stage_summaries[stage],
                run_id=run_id,
                timestamp=timestamp,
                figma_source=figma_source,
                predecessor_branch=predecessor_branch,
                gates=stage_gates.get(stage, []),
            )
            mrs[stage] = open_merge_request(
                project_id, branch, base_ref,
                f"{stage.replace('-', ' ').title()} — run {run_id}",
                description,
                api_base, token, client=c,
            )
            predecessor_branch = branch
    finally:
        if own:
            c.close()
    return mrs
