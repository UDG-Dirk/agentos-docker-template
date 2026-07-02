"""
Spike S2 — HELIX pipeline-plumbing test (Agno Workflow as the orchestration spine).

Tests, with DUMMY data, whether Agno's Workflow primitive (agno 2.6.7) supports the
shapes the real HELIX UC2 pipeline needs:

  Step 1  figma_extraction   — Team-inside-Step (broadcast/multi-pull + consensus) -> FigmaExtractionResult
  Step 2  token_normalization— agent (-> NormalizedTokens) + HITL output-review gate (pause for human)
  Step 3  scaffolding        — agent reading session_state (ProjectConfig) + prev step -> ScaffoldedOutput

Patterns probed (see PLUMBING_REPORT.md): typed step data, Team-in-Step, HITL pause,
session_state access, error handling, AgentOS registration.

Importable: `from spike_s2.workflow_code import helix_workflow` (registered in AgentOS).
Runnable standalone for fast iteration:
    cd ~/opencode/workbench/helix-poc-agno
    ../agno-setup/poc-agno-template/.venv/bin/python -m spike_s2.workflow_code   # (or python spike_s2/workflow_code.py)
"""
from __future__ import annotations

import json
from os import getenv

from agno.agent import Agent
from agno.models.openai import OpenAIChat
from agno.team import Team
from agno.workflow import Step, Workflow
from agno.workflow.types import StepInput, StepOutput

try:  # package-relative when imported by AgentOS; flat when run as a file
    from .models import (
        FigmaExtractionResult,
        NormalizedTokens,
        ProjectConfig,
        ScaffoldedOutput,
    )
except ImportError:  # pragma: no cover
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from models import (  # type: ignore
        FigmaExtractionResult,
        NormalizedTokens,
        ProjectConfig,
        ScaffoldedOutput,
    )


def make_model() -> OpenAIChat:
    # OpenAIChat (NOT OpenAIResponses) — required for tool/structured round-trips via LiteLLM.
    return OpenAIChat(id=getenv("OPENAI_MODEL_ID", "gpt-5.4"), base_url=getenv("OPENAI_BASE_URL", None))


# ---------------------------------------------------------------------------
# STEP 1 — Figma extraction as a TEAM inside a STEP (broadcast multi-pull + consensus)
# ---------------------------------------------------------------------------
def _extractor(name: str, tint: str) -> Agent:
    return Agent(
        name=name,
        model=make_model(),
        instructions=[
            "You simulate one Figma extraction pull for a design system (DUMMY data, no real Figma).",
            "Return a small JSON object with keys: tokens_raw, components_raw, extraction_runs, consensus_confidence.",
            f"Use these dummy values, varying slightly to simulate multi-pull variance ({tint}):",
            'tokens_raw = {"colors": {"primary": "#3388f0"}, "typography": {"heading": "roboto"}};',
            'components_raw = [{"name": "Button", "variants": ["Solid/md/Default", "Solid/md/Hover"]}];',
            "extraction_runs = 1; consensus_confidence = a float 0.8-0.95.",
            "Keep it terse.",
        ],
    )

extraction_team = Team(
    name="figma-extraction-team",
    model=make_model(),
    members=[
        _extractor("puller-a", "lean slightly high confidence"),
        _extractor("puller-b", "lean slightly low confidence"),
        _extractor("puller-c", "middle confidence"),
    ],
    delegate_to_all_members=True,   # broadcast: every member gets the same task
    output_schema=FigmaExtractionResult,
    add_session_state_to_context=True,
    instructions=[
        "You are the consensus synthesizer for a multi-pull Figma extraction.",
        "All members were broadcast the same extraction task. Compare their JSON results.",
        "Synthesize ONE FigmaExtractionResult: merge tokens_raw/components_raw, set extraction_runs to the number of members (3),",
        "and set consensus_confidence to the AVERAGE of the members' confidences.",
    ],
)

figma_extraction_step = Step(
    name="figma_extraction",
    team=extraction_team,                 # <-- Team-inside-Step (native), KEY TEST #2
    description="Multi-pull Figma extraction with consensus (dummy).",
)


# ---------------------------------------------------------------------------
# STEP 2 — token normalization (agent, typed) + HITL output-review gate
# ---------------------------------------------------------------------------
normalizer_agent = Agent(
    name="token-normalizer",
    model=make_model(),
    output_schema=NormalizedTokens,       # typed output, KEY TEST #1
    instructions=[
        "You normalize raw Figma tokens into W3C DTCG shape (DUMMY transform).",
        "You receive a FigmaExtractionResult JSON from the previous step.",
        "Produce NormalizedTokens: dtcg_tokens (nest the raw tokens under a 'color'/'typography' group with a '$value' and '$type'),",
        'typos_detected = ["disbled -> disabled"] (simulate one fix), token_count = number of leaf tokens.',
    ],
)

token_normalization_step = Step(
    name="token_normalization",
    agent=normalizer_agent,
    description="Normalize to DTCG; then PAUSE for human review of the tokens.",
    requires_output_review=True,          # <-- HITL gate AFTER the agent runs, KEY TEST #3
    output_review_message="Review the normalized DTCG tokens. Approve to scaffold, or reject to abort.",
)


# ---------------------------------------------------------------------------
# STEP 3 — scaffolding (agent) reading BOTH previous step output AND session_state
# ---------------------------------------------------------------------------
scaffolder_agent = Agent(
    name="scaffolder",
    model=make_model(),
    output_schema=ScaffoldedOutput,
    add_session_state_to_context=True,    # <-- session_state access, KEY TEST #4
    instructions=[
        "You scaffold component code from NormalizedTokens (DUMMY output).",
        "Target framework = {framework} and CMS = {cms_type} (from session_state ProjectConfig).",
        "Produce ScaffoldedOutput:",
        'vue_components = [{"name": "Button.vue", "code": "<template>..dummy..</template>"}] (name the file per the framework),',
        'storybook_stories = [{"name": "Button.stories.ts", "code": "..dummy.."}],',
        'cms_bloks = [{"name": "button_blok", "schema": {"type": "{cms_type}"}}].',
        "Echo the framework and cms_type you saw so we can confirm session_state was readable.",
    ],
)

scaffolding_step = Step(
    name="scaffolding",
    agent=scaffolder_agent,
    description="Scaffold framework-specific code using session_state config.",
)


# ---------------------------------------------------------------------------
# WORKFLOW — the pipeline spine
# ---------------------------------------------------------------------------
def _workflow_db():
    """HITL resume (continue_run) needs a persisted WorkflowSession -> a db is required.
    Reuse the template's agentos-db. Returns None if unavailable (pause still works; resume won't)."""
    try:
        import sys
        from pathlib import Path

        tmpl = Path(__file__).resolve().parents[2] / "agno-setup" / "poc-agno-template"
        if str(tmpl) not in sys.path:
            sys.path.insert(0, str(tmpl))
        from db import get_postgres_db  # type: ignore

        return get_postgres_db()
    except Exception as e:  # pragma: no cover
        print(f"[warn] no workflow db ({e}); HITL resume will not persist")
        return None


helix_workflow = Workflow(
    id="helix-pipeline-plumbing-test",
    name="helix-pipeline-plumbing-test",
    description="S2 spike: dummy HELIX pipeline to validate Agno Workflow plumbing (typed steps, Team-in-Step, HITL, session_state).",
    db=_workflow_db(),
    steps=[figma_extraction_step, token_normalization_step, scaffolding_step],
)


DUMMY_CONFIG = ProjectConfig(
    figma_file_key="8qPSyetzviLR6eF6bkpL44",
    figma_pat="dummy_pat_not_real",
    storybook_repo_url="https://rmvc01.rm.udg.de/test/helix-storybook",
    cms_repo_url="https://rmvc01.rm.udg.de/test/helix-cms-models",
    cms_type="storyblok",
    framework="vue",
)


def _summarize(run) -> dict:
    steps = []
    for s in (getattr(run, "step_results", None) or getattr(run, "step_responses", None) or []):
        steps.append(getattr(s, "step_name", "?"))
    return {
        "status": str(getattr(getattr(run, "status", None), "value", getattr(run, "status", None))),
        "steps_ran": steps,
        "is_paused": getattr(run, "is_paused", None),
        "run_id": getattr(run, "run_id", None),
        "content_head": str(getattr(run, "content", ""))[:300],
    }


def _full_dump(run) -> dict:
    out = _summarize(run)
    try:
        out["paused_step_name"] = getattr(run, "paused_step_name", None)
        out["pause_kind"] = str(getattr(run, "pause_kind", None))
    except Exception:
        pass
    return out


if __name__ == "__main__":
    import sys
    from pathlib import Path

    OUT = Path("/home/dirk/opencode/workbench/helix-poc-agno/spike_s2/run_result.json")
    findings = {}
    print(f"[config] model={make_model().id!r} base_url={getenv('OPENAI_BASE_URL')!r}")

    run = helix_workflow.run(
        input="Run HELIX pipeline for test project (dummy plumbing).",
        session_state=DUMMY_CONFIG.model_dump(),
    )
    findings["after_run"] = _full_dump(run)
    print("[run]", json.dumps(findings["after_run"], indent=2, default=str))

    # ---- HITL resume: approve the output-review gate, then Step 3 runs ----
    if getattr(run, "is_paused", False):
        reviews = run.steps_requiring_output_review
        reviews = reviews() if callable(reviews) else reviews
        print(f"[hitl] PAUSED at output-review gate; {len(reviews)} requirement(s).")
        # capture what the human would review (the normalized tokens)
        try:
            findings["hitl_review_payload_head"] = str(getattr(reviews[0], "step_output", None))[:400]
        except Exception:
            pass
        for req in reviews:
            req.confirmed = True  # APPROVE (reject = confirmed=False + rejection_feedback / on_reject)
        cont = helix_workflow.continue_run(run_response=run, step_requirements=reviews)
        findings["after_continue"] = _full_dump(cont)
        print("[hitl] continued:", json.dumps(findings["after_continue"], indent=2, default=str))
        # confirm Step 3 read session_state (framework/cms echoed in its output)
        final_content = str(getattr(cont, "content", ""))
        findings["session_state_echo_seen"] = ("vue" in final_content.lower() or "storyblok" in final_content.lower())
        findings["final_content_head"] = final_content[:600]

    OUT.write_text(json.dumps(findings, indent=2, default=str))
    print(f"[done] wrote {OUT}")
