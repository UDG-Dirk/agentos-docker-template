#!/usr/bin/env python3
"""S3 Phase 0b — granular scope probe: which repository endpoints does the PAT actually allow?
Tests the endpoints the pipeline NEEDS (tree/file read, branch+commit write). spike/* only. PAT never printed."""
import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE.parent / ".env", override=False)
PAT = os.environ["GITLAB_PAT"]
API = "https://rmvc01.rm.udg.de/api/v4"
PROJ = "customer-udg-ai%2Fprojects%2Fhelix-poc-agno"
H = {"PRIVATE-TOKEN": PAT}
OUT = HERE / "phase0b_result.json"
res = {}

with httpx.Client(verify=True, timeout=30) as c:
    def probe(label, method, path, **kw):
        r = c.request(method, f"{API}{path}", headers=H, **kw)
        body = r.text[:160]
        res[label] = {"status": r.status_code, "body": body}
        print(label, r.status_code, body[:120])
        return r

    probe("tree", "GET", f"/projects/{PROJ}/repository/tree")
    probe("branches_list", "GET", f"/projects/{PROJ}/repository/branches")
    # try to create a probe branch from common default names
    made = None
    for ref in ("main", "master"):
        r = probe(f"create_branch_from_{ref}", "POST",
                  f"/projects/{PROJ}/repository/branches",
                  params={"branch": "spike/s3-scope-probe", "ref": ref})
        if r.status_code in (201, 400):  # 400 often = already exists
            made = ref
            break
    # try a commit (creates a file on a fresh branch; start_branch from default if exists)
    commit_body = {
        "branch": "spike/s3-scope-probe2",
        "commit_message": "S3 scope probe commit",
        "actions": [{"action": "create", "file_path": "spike_s3_scope_probe.md", "content": "scope probe"}],
    }
    if made:
        commit_body["start_branch"] = made
    probe("create_commit", "POST", f"/projects/{PROJ}/repository/commits", json=commit_body)

OUT.write_text(json.dumps(res, indent=2))
print("\nWROTE", OUT)
