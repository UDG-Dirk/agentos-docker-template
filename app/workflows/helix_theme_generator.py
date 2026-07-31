"""HELIX Theme Generator — Agent 3c workflow endpoint (v0.1, Phase 1).

Wires the 3c Theme Generator into PROD as its own workflow (spec §9.1). 3c forks the
helix-code baseline into a customer-specific component-library package, combining:
  * 3a Baseline Reader output   (reference components/tokens/structure),
  * 3b Semantic Matcher scoring (client→baseline per-component mapping), and
  * the HELIX extractor's client FigmaExtractionResult.

**Phase 1 (this shape) is DETERMINISTIC only** — two steps:
  1. ``theme-input-gathering``        — gather + validate the three inputs (SP-6 fail-loud).
  2. ``theme-deterministic-transform`` — package scaffolding + token substitution +
     high-confidence component forking + passthrough handling (spec §5.1).

Steps 3-6 (agentic reconciliation, coarse cohesion review, provenance, assembly — spec
§9.2) are Phase 2+. The output envelope (agents/theme_generator/models.py) is already the
full v0.1 shape, so later phases add behaviour without changing the contract.

**Inputs are passed via run ``additional_data``** (Phase 1 — the pipeline caller composes
3a/3b/extractor themselves per the 3a workflow's own note):
  additional_data = {
    "customer_slug": "<slug>",           # optional; falls back to client file_key
    "scope": "<npm-scope>",              # optional; default 'msq-dx'
    "baseline": {<3a BaselineReaderOutput>},
    "client_extraction": {<FigmaExtractionResult>},
    "scoring_by_slot": {"<component>": <3b ScoringResult>, ...},
    "output_dir": "<path>",              # optional; materialise the package to disk
  }

Config is env-only (SP-9); Phase 1 invokes no model (deterministic), so model routing is
recorded as n/a. When Phase 2 lands, agent steps use ``default_chat_model()`` from
app.settings (resolves OPENAI_MODEL_ID; NOT a hardcoded model). Determinism labelled per
Decision #5: the envelope always carries ``non_deterministic: true``.

Invoke: ``POST /workflows/helix-theme-generator/runs``. Blocking conditions (missing
baseline / client extraction) surface as ``blocking_warnings`` with status=failure — never raise.
"""
from __future__ import annotations

from agno.workflow import Step, Workflow

from agents.theme_generator.step import (
    STEP_NAME_INPUT,
    STEP_NAME_TRANSFORM,
    deterministic_transform_executor,
    input_gathering_executor,
)
from db import get_postgres_db

theme_input_step = Step(name=STEP_NAME_INPUT, executor=input_gathering_executor)
theme_transform_step = Step(name=STEP_NAME_TRANSFORM, executor=deterministic_transform_executor)

helix_theme_generator_workflow = Workflow(
    id="helix-theme-generator",
    name="HELIX Theme Generator",
    description=(
        "Agent 3c (v0.1, Phase 1 — deterministic): forks the helix-code baseline into a "
        "customer-specific component-library package. Step 1 gathers + validates the 3a baseline, "
        "the client FigmaExtractionResult, and 3b per-component scoring; Step 2 deterministically "
        "scaffolds the package, substitutes tokens, forks high-confidence components, and passes "
        "through baseline-less client components. Agentic reconciliation + cohesion review are "
        "Phase 2. Fail-loud blocking_warnings (SP-6); non_deterministic:true always (Decision #5); "
        "inputs via additional_data."
    ),
    db=get_postgres_db(),
    steps=[theme_input_step, theme_transform_step],
)
