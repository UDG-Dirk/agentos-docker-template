#!/usr/bin/env python3
"""S3 Phase 0 — GitLab REST API sanity check. Loads GITLAB_PAT from helix .env (never printed)."""
import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE.parent / ".env", override=False)

PAT = os.environ.get("GITLAB_PAT")
API = "https://rmvc01.rm.udg.de/api/v4"
PROJ = "customer-udg-ai%2Fprojects%2Fhelix-poc-agno"
OUT = HERE / "phase0_result.json"

res = {"pat_present": bool(PAT)}
if not PAT:
    OUT.write_text(json.dumps({"status": "FAIL", "error": "GITLAB_PAT missing"}, indent=2))
    print("FAIL: GITLAB_PAT missing"); sys.exit(2)

headers = {"PRIVATE-TOKEN": PAT}


def _get(client, path):
    r = client.get(f"{API}{path}", headers=headers)
    return r


# detect SSL behavior: try verify=True first, fall back to verify=False (self-signed)
verify = True
try:
    with httpx.Client(verify=True, timeout=30) as c:
        _get(c, f"/projects/{PROJ}")
except httpx.ConnectError as e:
    if "CERTIFICATE" in str(e).upper() or "SSL" in str(e).upper():
        verify = False
        res["ssl_note"] = f"self-signed/untrusted cert -> using verify=False ({str(e)[:120]})"
    else:
        res["ssl_note"] = f"connect error: {str(e)[:160]}"
except Exception as e:
    res["ssl_note"] = f"probe exception: {type(e).__name__}: {str(e)[:120]}"

res["ssl_verify_used"] = verify

with httpx.Client(verify=verify, timeout=30) as c:
    pr = _get(c, f"/projects/{PROJ}")
    res["project_GET_status"] = pr.status_code
    if pr.status_code == 200:
        pj = pr.json()
        res["project_id"] = pj.get("id")
        res["project_path"] = pj.get("path_with_namespace")
        res["default_branch"] = pj.get("default_branch")
        res["empty_repo"] = pj.get("empty_repo")
    else:
        res["project_body_head"] = pr.text[:200]
    br = _get(c, f"/projects/{PROJ}/repository/branches")
    res["branches_GET_status"] = br.status_code
    if br.status_code == 200:
        res["branches"] = [b.get("name") for b in br.json()][:20]

OUT.write_text(json.dumps(res, indent=2))
print(json.dumps(res, indent=2))
