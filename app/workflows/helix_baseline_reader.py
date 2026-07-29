"""HELIX Baseline Reader — standalone Agent 3a workflow endpoint (Cycle 2).

Wires the deterministic Baseline Reader (spec:baseline-reader-v1) into PROD as its OWN workflow
(decision (a) = A1 standalone), NOT embedded in another workflow — downstream callers (Semantic
Matcher 3b, Theme Generator 3c, …) compose the pipeline themselves.

On invocation it pull-checks-out the helix-code baseline (shallow clone via the `BASELINE_REPO_*`
deploy-token envs) and reads a machine-readable inventory (tokens, components, storybook). The
**Custom Elements Manifest** is NOT committed to helix-code, so per decision (b) = 1b it is built by
a SEPARATE CI job (`build-cem`, `pnpm … analyze:flat`) and delivered to the container; point the
reader at it via env `HELIX_CODE_CEM_PATH`. If the CEM is absent, the reader emits a `CEM_MISSING`
blocking_warning (fail-loud) — the endpoint still returns, with `success=False`.

Config is env-only (SP-9): `BASELINE_REPO_URL` / `BASELINE_REPO_USERNAME` / `BASELINE_REPO_TOKEN`
(deploy token), `BASELINE_REPO_PATH` (checkout dir, Coolify volume), `BASELINE_OUTPUT_DIR`,
`HELIX_CODE_CEM_PATH` (CI-built CEM). Per-run override: `additional_data={"baseline_ref": "<ref>"}`
(default `master`). Deterministic, zero-LLM, read-only on the baseline.

Invoke: `POST /workflows/helix-baseline-reader/runs` (use `background=true` + `get_session_run`;
the checkout can take a few seconds). Blocking conditions surface as `blocking_warnings`, never raise.
"""
from __future__ import annotations

from agno.workflow import Step, Workflow

from agents.baseline_reader.step import STEP_NAME_BASELINE, baseline_read_executor
from db import get_postgres_db

baseline_read_step = Step(name=STEP_NAME_BASELINE, executor=baseline_read_executor)

helix_baseline_reader_workflow = Workflow(
    id="helix-baseline-reader",
    name="HELIX Baseline Reader",
    description=(
        "Standalone Agent 3a: pull-on-invocation read of the helix-code baseline (tokens, components "
        "from the CI-built CEM via HELIX_CODE_CEM_PATH, storybook) → deterministic JSON inventory. "
        "Fail-loud blocking_warnings (BASELINE_AUTH_MISSING / BASELINE_FETCH_FAILED / CEM_MISSING); "
        "zero-LLM; read-only. Env-configured (SP-9). additional_data={'baseline_ref': '<ref>'}."
    ),
    db=get_postgres_db(),
    steps=[baseline_read_step],
)
