# Spike S2 — Lessons Learned: Agno Workflow plumbing (agno 2.6.7)

## Import paths (verified against the repo's own Level-4 example + source)
```python
from agno.workflow import Step, Workflow
from agno.workflow.types import StepInput, StepOutput, StepRequirement
from agno.team import Team
from agno.agent import Agent
from agno.models.openai import OpenAIChat   # NOT OpenAIResponses for tool/structured agents (LiteLLM)
```

## What worked OUT OF THE BOX (native in 2.6.7)
- **Team-inside-Step**: `Step(name=..., team=my_team)` — no executor workaround needed. Broadcast = `Team(delegate_to_all_members=True, output_schema=...)`; the team leader synthesizes the consensus into the typed model.
- **Typed step data**: `Agent(output_schema=PydanticModel)` on an agent step; the next step receives the prior content via `step_input.previous_step_content` (and agent steps auto-receive it as input).
- **HITL pause** is FIRST-CLASS on `Step`: `requires_confirmation` (pre-exec gate), `requires_output_review` (post-exec review gate), `requires_user_input` (+`user_input_schema`), `human_review=HumanReview(...)`, plus `on_reject` (skip/cancel/retry/else), `confirmation_message`/`output_review_message`, `hitl_timeout`/`on_timeout`.
- **session_state**: passed to `workflow.run(session_state=<dict>)`. Agents read it with `add_session_state_to_context=True` + `{var}` templating in instructions.
- **Registration**: `AgentOS(workflows=[wf], db=...)`. REST endpoints auto-exposed: `GET /workflows`, `GET /workflows/{id}`, `POST /workflows/{id}/runs`, `GET /workflows/{id}/runs/{run_id}`, `POST .../continue`, `POST .../resume`, `POST .../cancel`.

## GOTCHAS / undocumented behaviors discovered
1. **HITL resume REQUIRES a persisted session** → the Workflow MUST have `db=`. With no db, the pause works and `run.is_paused` is true, but `continue_run(...)` raises `ValueError: Could not find session with id ...` ("WorkflowSession not found in db"). In AgentOS the workflow uses agentos-db, so resume works over REST; standalone scripts must pass `db=get_postgres_db()`.
2. **`session_state` must be a DICT, not a Pydantic model.** `workflow.run(session_state=...)` is typed `Optional[Dict[str, Any]]`. The task spec's `session_state=ProjectConfig(...)` fails type expectations — pass `ProjectConfig(...).model_dump()`.
3. **`run.steps_requiring_output_review` is a PROPERTY, not a method** (despite reading like `def ...` in source). Access without parentheses; `run.steps_requiring_confirmation` likewise.
4. **Approving an output-review pause**: take the `StepRequirement`s from `run.steps_requiring_output_review`, set `req.confirmed = True` (reject = `confirmed=False` + optional `rejection_feedback`; `edited_output` to modify), then `helix_workflow.continue_run(run_response=run, step_requirements=reqs)`.
5. **Pause shape**: `WorkflowRunOutput` exposes `is_paused`, `status == "PAUSED"`, `paused_step_name`, `paused_step_index`, `pause_kind` ("step" vs "executor"), and `step_requirements`. Executor-level HITL (a tool inside an agent/team step pausing) is a distinct `pause_kind="executor"`.
6. **Function steps** use `def fn(step_input: StepInput) -> StepOutput`; read input via `step_input.input` / `step_input.previous_step_content`; halt the pipeline with `StepOutput(success=False, stop=True)` (it's `stop=True` that stops downstream, not `success=False`).
7. **Latency**: a full LLM pipeline with a 3-member broadcast Team + consensus + 2 more agent steps through the LiteLLM proxy runs **>400s** — the Team broadcast dominates. For plumbing/CI tests, use 2 members, terser instructions, or stub the team with a function executor.

## Pattern recommendations for the real HELIX pipeline
- **Spine = Agno Workflow with explicit `Step`s.** It cleanly carries typed data, Team-in-Step, HITL, and session_state — F3 is GO.
- **Figma extraction (multi-pull consensus)** → `Step(team=Team(delegate_to_all_members=True, output_schema=FigmaExtractionResult))`. For determinism/cost in CI, consider a function-executor that loops a single agent N times and self-judges.
- **Token-normalization review gate** → `Step(requires_output_review=True)`; surface the pause over REST `/continue`; persist with the AgentOS db. Use `on_reject="cancel"` (or `retry` with `rejection_feedback`) for the reject path.
- **Config** (Figma source, repo destinations, CMS/framework) → `session_state` dict; agents read via `add_session_state_to_context=True`. Keep the PAT OUT of session_state in real runs (inject at the MCP/tool layer; never log).
- **Validation/quality gates** → deterministic function steps (zero LLM, no hallucination), per the Level-4 pattern.
- **Registration** → `AgentOS(workflows=[...])`; do NOT dump spike code into `poc-agno-template/agents/`. Real pipeline workflow lives in its own module imported by `app/main.py`.

## Anti-patterns confirmed
- `OpenAIResponses` for tool/structured agents → breaks via LiteLLM (use `OpenAIChat`). Used `OpenAIChat` throughout; structured outputs worked.
- No Docker; bare-metal venv + LiteLLM proxy + bare-metal Postgres.
