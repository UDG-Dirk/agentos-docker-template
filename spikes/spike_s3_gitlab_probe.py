#!/usr/bin/env python3
"""
Spike S3 — GitLab push from an Agno agent (F4 — pipeline output channel).

Validates whether an Agno agent can create a branch + commit files + verify, against
our self-hosted GitLab via REST API + PAT (PRIVATE-TOKEN header). NO git CLI, NO SSH.

Secret handling: GITLAB_PAT loaded from helix-poc-agno/.env via dotenv; NEVER printed.
Model creds from poc-agno-template/.env (LiteLLM). OpenAIChat (not OpenAIResponses).

Modes:
  --direct   run the GitLab REST client directly (no LLM): single-file + multi-file commit + verify
  --agent    run the Agno agent with the GitLab tool (commit has requires_confirmation gate)
  (default: --direct then --agent)

Run:
  cd ~/opencode/workbench/agno-setup/poc-agno-template
  .venv/bin/dotenv run -- .venv/bin/python /home/dirk/opencode/workbench/helix-poc-agno/spike_s3_gitlab_probe.py --direct
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import List

import httpx
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
TEMPLATE_ENV = HERE.parent / "agno-setup" / "poc-agno-template" / ".env"
LOCAL_ENV = HERE / ".env"
OUTDIR = HERE / "spike_s3"
OUTDIR.mkdir(exist_ok=True)

load_dotenv(LOCAL_ENV, override=False)      # GITLAB_PAT
load_dotenv(TEMPLATE_ENV, override=False)   # OPENAI_* model creds

GITLAB_PAT = os.environ.get("GITLAB_PAT")
API = "https://rmvc01.rm.udg.de/api/v4"
PROJECT = "customer-udg-ai%2Fprojects%2Fhelix-poc-agno"
SSL_VERIFY = os.environ.get("GITLAB_SSL_VERIFY", "true").lower() != "false"


# ---------------------------------------------------------------------------
# GitLab REST client (reusable pipeline component). PAT in header; never logged.
# ---------------------------------------------------------------------------
class GitLabClient:
    def __init__(self, pat: str, api: str = API, project: str = PROJECT, verify: bool = SSL_VERIFY):
        self._h = {"PRIVATE-TOKEN": pat}
        self.api, self.project = api, project
        self._c = httpx.Client(verify=verify, timeout=60)

    def _u(self, path: str) -> str:
        return f"{self.api}/projects/{self.project}{path}"

    def get_project(self) -> dict:
        r = self._c.get(self._u(""), headers=self._h)
        return {"status": r.status_code, "json": (r.json() if r.status_code == 200 else r.text[:200])}

    def get_branch(self, branch: str) -> dict:
        from urllib.parse import quote
        r = self._c.get(self._u(f"/repository/branches/{quote(branch, safe='')}"), headers=self._h)
        return {"status": r.status_code, "json": (r.json() if r.status_code == 200 else r.text[:200])}

    def create_branch(self, branch: str, ref: str = "main") -> dict:
        r = self._c.post(self._u("/repository/branches"), headers=self._h,
                         params={"branch": branch, "ref": ref})
        return {"status": r.status_code, "json": (r.json() if r.status_code in (200, 201) else r.text[:300])}

    def commit_files(self, branch: str, files: List[dict], message: str,
                     start_branch: str | None = None) -> dict:
        """files: [{'file_path':..., 'content':...}] -> one commit with create actions."""
        body = {
            "branch": branch,
            "commit_message": message,
            "actions": [{"action": "create", "file_path": f["file_path"], "content": f["content"]} for f in files],
        }
        if start_branch:
            body["start_branch"] = start_branch  # create branch implicitly from this ref
        r = self._c.post(self._u("/repository/commits"), headers=self._h, json=body)
        return {"status": r.status_code, "json": (r.json() if r.status_code in (200, 201) else r.text[:400])}

    def get_file(self, file_path: str, ref: str) -> dict:
        from urllib.parse import quote
        r = self._c.get(self._u(f"/repository/files/{quote(file_path, safe='')}"),
                        headers=self._h, params={"ref": ref})
        return {"status": r.status_code, "exists": r.status_code == 200,
                "json": (r.json() if r.status_code == 200 else r.text[:200])}


def _direct_tests() -> dict:
    """Run the REST client directly (no LLM) — definitive proof the API path works."""
    gl = GitLabClient(GITLAB_PAT)
    proj = gl.get_project()
    out = {"ssl_verify": SSL_VERIFY, "project": proj}
    ts = os.environ.get("S3_TS", "TS")  # timestamp injected by caller

    # Empty-repo aware: if no commits exist yet, the first commit to a branch must omit
    # start_branch (it creates the branch fresh). This also keeps us off main.
    pj = proj.get("json") if isinstance(proj.get("json"), dict) else {}
    empty = bool(pj.get("empty_repo"))
    base = None if empty else (pj.get("default_branch") or "main")
    out["repo_empty"] = empty
    out["base_ref_used"] = base

    # Phase 1/2 — single file
    t0 = time.time()
    single = gl.commit_files(
        branch="spike/s3-test",
        start_branch=base,
        files=[{"file_path": "spike_s3_test.md", "content": f"S3 spike - GitLab push from Agno agent - {ts}"}],
        message="S3 spike: single-file push from Agno pipeline",
    )
    out["single_commit"] = single
    out["single_commit_latency_s"] = round(time.time() - t0, 2)
    out["single_branch"] = gl.get_branch("spike/s3-test")
    out["single_file"] = gl.get_file("spike_s3_test.md", "spike/s3-test")

    # Phase 3 — multi-file
    t0 = time.time()
    multi = gl.commit_files(
        branch="spike/s3-multi-test",
        start_branch=base,
        files=[
            {"file_path": "components/Button.vue", "content": "<template><button><slot/></button></template>"},
            {"file_path": "components/Button.stories.ts", "content": "export default { title: 'Button' };"},
            {"file_path": "tokens/design-tokens.json", "content": json.dumps({"color": {"primary": {"$value": "#3388f0", "$type": "color"}}})},
            {"file_path": "storyblok/bloks/button.json", "content": json.dumps({"name": "button", "schema": {"label": {"type": "text"}}})},
        ],
        message="S3 spike: multi-file pipeline output",
    )
    out["multi_commit"] = multi
    out["multi_commit_latency_s"] = round(time.time() - t0, 2)
    out["multi_files_check"] = {
        p: gl.get_file(p, "spike/s3-multi-test")["exists"]
        for p in ("components/Button.vue", "components/Button.stories.ts", "tokens/design-tokens.json", "storyblok/bloks/button.json")
    }
    return out


def _build_agent():
    """Agno agent with a GitLab tool; the commit op carries an HITL confirmation gate."""
    from agno.agent import Agent
    from agno.models.openai import OpenAIChat
    from agno.tools import tool

    gl = GitLabClient(GITLAB_PAT)

    @tool(requires_confirmation=True)  # HITL gate on the write/push
    def gitlab_commit(branch: str, file_path: str, content: str, commit_message: str) -> str:
        """Create a commit (and the branch if needed) on the HELIX GitLab project.

        Args:
            branch: target branch (use a spike/* branch, never main).
            file_path: path of the file to create.
            content: file content.
            commit_message: commit message.
        """
        res = gl.commit_files(branch=branch, start_branch="main",
                              files=[{"file_path": file_path, "content": content}],
                              message=commit_message)
        return json.dumps(res)

    @tool
    def gitlab_get_branch(branch: str) -> str:
        """Verify a branch exists on the HELIX GitLab project."""
        return json.dumps(gl.get_branch(branch))

    agent = Agent(
        model=OpenAIChat(id=os.environ.get("OPENAI_MODEL_ID", "gpt-5.4"), base_url=os.environ.get("OPENAI_BASE_URL")),
        tools=[gitlab_commit, gitlab_get_branch],
        instructions=[
            "You push HELIX pipeline output to GitLab via the provided tools. Use spike/* branches only; never main.",
            "When asked to commit, call gitlab_commit (it will pause for human confirmation), then verify with gitlab_get_branch.",
        ],
        markdown=False,
    )
    return agent


if __name__ == "__main__":
    if not GITLAB_PAT:
        print(json.dumps({"status": "FAIL", "error": "GITLAB_PAT missing"})); sys.exit(2)
    mode = sys.argv[1] if len(sys.argv) > 1 else "--direct"
    if mode in ("--direct", "--all"):
        res = _direct_tests()
        (OUTDIR / "verification_result.json").write_text(json.dumps(res, indent=2, default=str))
        print("DIRECT:", json.dumps(res, indent=2, default=str)[:1200])
    if mode in ("--agent", "--all"):
        agent = _build_agent()
        run = agent.run("Create branch 'spike/s3-agent-test', commit file 'spike_s3_agent.md' "
                        "with content 'S3 agent push', message 'S3 agent commit', then verify the branch.")
        paused = getattr(run, "is_paused", False)
        info = {"is_paused": paused, "status": str(getattr(run, "status", None))}
        # auto-approve the tool confirmation if paused, then continue
        if paused:
            for t in (getattr(run, "tools_requiring_confirmation", None) or getattr(run, "tools_awaiting_confirmation", None) or []):
                try:
                    t.confirmed = True
                except Exception:
                    pass
            cont = agent.continue_run(run_response=run) if hasattr(agent, "continue_run") else None
            info["after_continue_status"] = str(getattr(cont, "status", None))
            info["final"] = str(getattr(cont, "content", ""))[:600]
        (OUTDIR / "agent_result.json").write_text(json.dumps(info, indent=2, default=str))
        print("AGENT:", json.dumps(info, indent=2, default=str)[:800])
