# Spike S2 — Agno Workflow Plumbing Report (F3)

**Date:** 2026-06-29 · **Agno:** 2.6.7 · **Stack:** poc-agno-template (bare-metal venv, LiteLLM proxy, bare-metal Postgres, AgentOS dev)
**Question:** Can Agno's Workflow primitive carry the HELIX UC2 pipeline shape — typed inter-step data, a Team inside a Step, an HITL gate, and session_state config?

## VERDICT: **GO.** All required patterns work; HITL resume has one hard requirement (a persisted `db=`).

Built a dummy 3-step pipeline (`helix-pipeline-plumbing-test`) with typed Pydantic models and ran it end-to-end (standalone + registered in AgentOS via REST). Files: `models.py`, `workflow_code.py`, `agentos_register_probe.py`, `run_result.json`, `agentos_register_result.json`.

```
input + session_state(ProjectConfig)
   └▶ Step 1  figma_extraction    Team(3 members, broadcast) + consensus  -> FigmaExtractionResult
   └▶ Step 2  token_normalization Agent(output_schema)  ──HITL output-review gate (PAUSE)──▶ approve
   └▶ Step 3  scaffolding         Agent reads session_state + prev step   -> ScaffoldedOutput
```

## Verification matrix

| # | Pattern | Result | Evidence / How |
|---|---|---|---|
| 1 | **Typed step data** | ✅ WORKS | `Agent(output_schema=NormalizedTokens)`; step content is a typed model instance; next step reads `step_input.previous_step_content`. Standalone run flowed FigmaExtractionResult → NormalizedTokens → ScaffoldedOutput; HITL payload showed a real `NormalizedTokens(...)` instance. |
| 2 | **Team-inside-Step** | ✅ WORKS (native) | `Step(team=Team(delegate_to_all_members=True, output_schema=FigmaExtractionResult))`. Step 1 executed; broadcast to 3 members + leader synthesis. No executor workaround needed. |
| 3 | **HITL pause** | ✅ WORKS (native) | `Step(requires_output_review=True, output_review_message=...)` → run `status=PAUSED`, `is_paused=True`, `pause_kind=STEP`, `paused_step_name='token_normalization'`. API: `run.steps_requiring_output_review` (property) → `StepRequirement` carrying `step_output` for the human to review. **Resume**: set `req.confirmed=True`, `continue_run(run_response=run, step_requirements=reqs)` → `status=COMPLETED`, Step 3 ran. REST: `POST /workflows/{id}/runs/{run_id}/continue` (and `/resume`). ⚠️ **Requires `db=`** (see gotcha). Reject path = `confirmed=False` + `on_reject` (skip/cancel/retry/else) + optional `rejection_feedback`/`edited_output`. |
| 4 | **session_state** | ✅ WORKS | Passed as a **dict** to `workflow.run(session_state=ProjectConfig(...).model_dump())`. Step 3 agent (`add_session_state_to_context=True`, `{framework}`/`{cms_type}` templating) read it — `session_state_echo_seen=true` (vue/storyblok surfaced in output). Mutable variants exist (`enable_agentic_state`) but default usage is read-as-context. |
| 5 | **Error handling** | ✅ WORKS | `Step(max_retries=3, on_error=skip|cancel, skip_on_failure=...)`; function steps halt via `StepOutput(success=False, stop=True)` (`stop` is what halts downstream). Errors are typed (StepError events). |
| 6 | **Workflow registration + API** | ✅ WORKS | `AgentOS(workflows=[helix_workflow], db=...)`; in-process TestClient: `GET /workflows` → 200, `workflow_registered=true`, id `helix-pipeline-plumbing-test`. REST run via `POST /workflows/{id}/runs` (form field is **`message`**, not `input`). Auto-exposed routes: `/workflows`, `/workflows/{id}`, `/runs`, `/runs/{run_id}`, `/runs/{run_id}/continue`, `/resume`, `/cancel`. |

## Key gotchas (full list in LESSONS_LEARNED.md)
1. **HITL resume requires a persisted `db=`.** Pause works without it, but `continue_run` raises `ValueError: Could not find session`. AgentOS supplies agentos-db; standalone needs `db=get_postgres_db()`.
2. `session_state` must be a **dict**, not a Pydantic model (`.model_dump()`).
3. `run.steps_requiring_output_review` / `steps_requiring_confirmation` are **properties**, not methods.
4. Full LLM pipeline latency **>400s** (the broadcast Team dominates) — use 2 members / function-stub for CI.
5. `OpenAIChat` (not `OpenAIResponses`) for structured/tool agents via LiteLLM.

## Recommendation for the real HELIX pipeline
- **Adopt Agno Workflow as the spine** — no pivot to n8n/custom needed for these requirements.
- Figma extraction → `Step(team=...)` broadcast + consensus (or function-loop for determinism/cost).
- Token-normalization review → `Step(requires_output_review=True)`, resumed over REST `/continue`, persisted on agentos-db. Keep the **PAT out of session_state** (inject at MCP/tool layer; never log).
- Deterministic validation/quality gates → function steps (zero-LLM), per the Level-4 pattern.
- Register the real workflow in its own module imported by `app/main.py` via `AgentOS(workflows=[...])` — don't dump spike code into `agents/`.
