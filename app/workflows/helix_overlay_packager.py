"""HELIX Overlay Packager — standalone workflow endpoint (v0.3 Phase 3).

Assembles the HELIX pipeline's outputs (3c token values, Path-A/Path-B
component code, Figma provenance) into a client-fork of helix-code under
``msq-turbo/helix-code-clients`` as a stacked 4-MR bundle (scaffolding+tokens
/ atoms / molecules / organisms) for FE-DEV review. Standalone Agno Workflow
(Option B, Phase 0-ratified — matches baseline_reader/3c/3d: native
HarnessTelemetry conformance, credential isolation, independent
VT-testability, clean FinOps attribution).

Credentials are DISTINCT from helix-code's read-only Deploy Token
(``BASELINE_REPO_*``): ``GITLAB_HELIX_CLIENTS_TOKEN`` / ``HELIX_CLIENTS_GIT_USERNAME``
write only to the client-forks subgroup — blast-radius isolation (Phase 0
Q0.3). helix-code itself stays READ-ONLY throughout (see agents/overlay_packager/
fork_ops.py's module docstring).

Config is env-only (SP-9): ``GITLAB_HELIX_CLIENTS_TOKEN``, ``HELIX_CODE_REPO_URL``,
``HELIX_CLIENTS_SUBGROUP_URL``, ``HELIX_CLIENTS_GIT_USERNAME``. Per-run input via
``additional_data`` — see ``agents.overlay_packager.step.overlay_packager_executor``'s
docstring for the full contract (customer_slug required, fail-loud if blank).

Invoke: ``POST /workflows/helix-overlay-packager/runs``. Blocking conditions
surface as ``blocking_warning``, never raise.
"""
from __future__ import annotations

from agno.workflow import Step, Workflow

from agents.overlay_packager.step import STEP_NAME_PACKAGER, overlay_packager_executor
from db import get_postgres_db

overlay_packager_step = Step(name=STEP_NAME_PACKAGER, executor=overlay_packager_executor)

helix_overlay_packager_workflow = Workflow(
    id="helix-overlay-packager",
    name="HELIX Overlay Packager",
    description=(
        "Standalone v0.3 Phase 3: fork-then-overlay customer packaging. Applies 3c token "
        "values + Path-A/Path-B component code into a client-fork of helix-code, opens a "
        "stacked 4-MR bundle (scaffolding+tokens / atoms / molecules / organisms), and "
        "writes helix-lock.json (FM3 source-shift tracking). Fail-loud blocking_warning on "
        "blank customer_slug, missing credentials, or an unresolvable client-forks subgroup. "
        "Env-configured (SP-9): GITLAB_HELIX_CLIENTS_TOKEN / HELIX_CODE_REPO_URL / "
        "HELIX_CLIENTS_SUBGROUP_URL / HELIX_CLIENTS_GIT_USERNAME."
    ),
    db=get_postgres_db(),
    steps=[overlay_packager_step],
)
