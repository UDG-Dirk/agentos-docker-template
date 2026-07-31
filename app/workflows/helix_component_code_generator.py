"""HELIX Component Code Generator — Agent 3d workflow endpoint (v0.1, Phase 1).

Generates a customer Lit + TypeScript component-library package from 3c's ThemeGeneratorOutput
(source-agnostic; UC1/UC2 today). Two steps:
  1. ``ccg-input-gathering`` — gather + validate the 3c element descriptors + token layer (SP-6).
  2. ``ccg-generate`` — Phase-1 DETERMINISTIC generation: route each element, and for baseline-matched
     ones fork the baseline Lit source from helix-code (READ-ONLY) into the customer namespace
     (re-tag / re-class / re-tokenise). No-baseline elements are recorded DEFERRED (Path B = Phase 2).

Phase 2+ add the agentic from-spec path + structural validation gate. The envelope
(agents/component_code_generator/models.py) is the full v0.1 shape already. helix-code is READ-ONLY
(Q3 α); config is env-only (SP-9); no `seed` on LLM calls (Anthropic route — 3c hardening finding).

Invoke: ``POST /workflows/helix-component-code-generator/runs`` with inputs in additional_data:
  {customer_slug, scope, elements:[{slot, baseline_ref, derivation, confidence}], tokens_json?,
   helix_code_root?, output_dir?}
"""
from __future__ import annotations

from agno.workflow import Step, Workflow

from agents.component_code_generator.step import (
    STEP_NAME_GENERATE,
    STEP_NAME_INPUT,
    generate_executor,
    input_gathering_executor,
)
from db import get_postgres_db

ccg_input_step = Step(name=STEP_NAME_INPUT, executor=input_gathering_executor)
ccg_generate_step = Step(name=STEP_NAME_GENERATE, executor=generate_executor)

helix_component_code_generator_workflow = Workflow(
    id="helix-component-code-generator",
    name="HELIX Component Code Generator",
    description=(
        "Agent 3d (v0.1, Phase 1 — deterministic): generates a customer Lit + TypeScript component "
        "library from 3c's output. Step 1 gathers the 3c element descriptors + token layer; Step 2 "
        "routes each element and forks the matched baseline Lit source from helix-code (READ-ONLY) "
        "into the customer namespace (re-tag/re-class/re-tokenise) — byte-identical, no LLM. "
        "No-baseline elements are DEFERRED to the Phase-2 agentic from-spec path (with a structural "
        "validation gate). Fail-loud (SP-6); helix-code READ-ONLY; inputs via additional_data."
    ),
    db=get_postgres_db(),
    steps=[ccg_input_step, ccg_generate_step],
)
