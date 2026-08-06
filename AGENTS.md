# Dark Factory — AgentOS (poc-agno-template)

Source of truth for any coding agent (Claude Code, Codex, others) working in this
repo. `CLAUDE.md` is a symlink to this file — edit one, both update.

> Stack constraints, gotchas, and MCP usage patterns are documented in `docs/AGNO_DEV_GUIDE.md`.
> Read that before extending this repo. (The `~/.claude/skills/agno-dev/` directory exists only
> on one developer's machine and is not part of this repo.)

## Project Overview

A unified agent platform built on [Agno](https://docs.agno.com) / AgentOS. Agents
persist sessions, memory, and knowledge in Postgres (with the pgvector vector-search
extension). Runs **bare metal** in local dev (venv + uvicorn, no containers) and on
**Coolify** in production.

## Architecture

```
AgentOS  (app/main.py)
├── web-search       (agents/web_search.py)      — direct tools (Parallel SDK / keyless MCP)
├── code-search      (agents/code_search.py)     — context provider (WorkspaceContextProvider)
├── reasoning-agent  (agents/reasoning_agent.py) — ReasoningTools (think/analyze) · OpenAIChat
└── knowledge-agent  (agents/knowledge_agent.py) — Dark Factory KB · context-injection RAG (retrieval-augmented generation)
```

Agents may also be created at runtime via the **Components API** (config-only:
model + instructions + memory + history + db, no redeploy). Those are REST-only and
do **not** carry tools/knowledge — see the skill's GOTCHAS.

The repo also hosts the **HELIX design-token pipeline** — Agno workflows registered in
`app/main.py` and defined in `app/workflows/`: `helix-figma-extractor`, `helix-client-extractor`,
`helix-composition-only-extractor`, `helix-baseline-reader`, `helix-theme-generator` (forks the
baseline into a customer package), and `helix-component-code-generator` (produces the customer
Lit + TypeScript component source; see [`agents/component_code_generator/README.md`](agents/component_code_generator/README.md)
for how customer branding is applied). The first four are deterministic, zero-LLM; the last two
combine a deterministic fork path with an AI-assisted path for genuinely new components. Their
per-stage logic lives in the `agents/figma_extractor/`, `agents/token_normalizer/`,
`agents/baseline_reader/`, `agents/theme_generator/`, and `agents/component_code_generator/`
subpackages.

Shared:
- PostgreSQL + pgvector for sessions, memory, knowledge.
- Models route through a **LiteLLM proxy** (`OPENAI_BASE_URL` / `OPENAI_API_KEY`).
- `app.settings.default_model()` → `OpenAIResponses(id="gpt-5.4")`. **Agents with
  `tools=[...]` must use `OpenAIChat` instead** (see skill GOTCHAS).
- Scheduler on by default (`scheduler=True`); Slack interface auto-enables when
  `SLACK_BOT_TOKEN` + `SLACK_SIGNING_SECRET` are set.
- Auth is **production-only**: enabled when `RUNTIME_ENV == "prd"`. Local dev has none.

### MCP tool surface

The `agno-prod` MCP server (mounted at `https://poc-agno-api.services.plygrnd.tech/mcp`) exposes:

| Tool | Signature | Description |
|------|-----------|--------------|
| `get_agentos_config` | `get_agentos_config()` | Full AgentOS config — registered agents/teams/workflows, interfaces, DB ids. Connectivity smoke test. |
| `run_agent` | `run_agent(agent_id, message)` | Run an agent with a message. |
| `run_team` | `run_team(team_id, message)` | Run a team with a message. |
| `run_workflow` | `run_workflow(workflow_id, message)` | Run a workflow. No `additional_data`, no `background` param — REST-only for those. |
| `get_sessions` / `get_session` / `create_session` / `get_session_runs` / `get_session_run` / `rename_session` / `update_session` / `delete_session` / `delete_sessions` | session CRUD | Manage agent/team/workflow sessions. |
| `create_memory` / `get_memory` / `get_memories` / `update_memory` / `delete_memory` / `delete_memories` | memory CRUD | Manage user memories. |

Full auth/wiring instructions: `scripts/README.md`. Full usage guide: `docs/AGNO_DEV_GUIDE.md`.

## Key Files

| File | Purpose |
|------|---------|
| [`app/main.py`](app/main.py) | AgentOS entrypoint — agent + workflow registry (the six HELIX workflows in `app/workflows/`), lifespan (KB ingest), Slack, MCP server, auth gate. |
| [`app/settings.py`](app/settings.py) | `default_model()` factory. |
| [`app/config.yaml`](app/config.yaml) | Quick prompts per agent (keyed by agent `id`). |
| [`agents/`](agents/) | Single-file for the generic agents (see Architecture); multi-file subpackages for the HELIX pipeline (`figma_extractor/`, `token_normalizer/`, `baseline_reader/`, `semantic_matcher/`, `theme_generator/`, `component_code_generator/`). |
| [`tools/`](tools/) | Shared tool factories (e.g. `parallel_search.py`, `context7.py`). |
| [`knowledge/`](knowledge/) | Knowledge-base definitions + ingestion (e.g. `dark_factory_kb.py`). |
| [`db/session.py`](db/session.py) | `get_postgres_db()` (id `agentos-db`), `create_knowledge()`, embedder id. |
| [`db/url.py`](db/url.py) | Builds the DB URL from `DB_*` env vars. |
| [`evals/cases.py`](evals/cases.py) · [`evals/__main__.py`](evals/__main__.py) | Eval suite — `python -m evals` (judge + reliability). |
| [`.mcp.json`](.mcp.json) | `agno-docs` MCP (live Agno API docs). |

## Dev quickstart (bare metal — no containers)

```bash
# from the repo root:
source .venv/bin/activate
dotenv run -- uvicorn app.main:app --reload --port 8000     # http://localhost:8000
curl -sSf http://localhost:8000/health                      # 200 = up (no auth in dev)
```

Postgres runs bare metal on `localhost:5432`. Format / validate: `./scripts/format.sh`,
`./scripts/validate.sh` (need the venv). **Full agent lifecycle flows, restart/log
recipes, and gotchas are in the `agno-dev` skill.**

## Production

Deployed on **Coolify** at `poc-agno-api.services.plygrnd.tech`. Deploy = `git push`
to `main` (Coolify auto-builds the prod image). `RUNTIME_ENV=prd` turns on JWT
(JSON Web Token, RS256-signed) auth; the public verification key goes in
`JWT_VERIFICATION_KEY`. Git: `dev`
for local work, `main` for prod — never push to `main` without review.

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `OPENAI_API_KEY` | yes | — | Key for the LiteLLM proxy (models + embeddings). |
| `OPENAI_BASE_URL` | dev/prod | — | LiteLLM proxy URL (`http://localhost:4000` local / `https://litellm.services.plygrnd.tech` prod). |
| `OPENAI_EMBEDDER_ID` | no | `openai/text-embedding-3-small` | Embedder id — keep the `openai/` prefix on LiteLLM (bare id 401s). |
| `RUNTIME_ENV` | no | `prd` | `dev` enables hot-reload and disables auth. |
| `JWT_VERIFICATION_KEY` | prd | — | RSA public PEM; required when `RUNTIME_ENV=prd`. |
| `AGENTOS_URL` | no | `http://127.0.0.1:8000` | Scheduler base URL (set to the public domain in prod). |
| `PARALLEL_API_KEY` | no | — | Raises the WebSearch agent's Parallel rate ceiling. |
| `SLACK_BOT_TOKEN` / `SLACK_SIGNING_SECRET` | no | — | Both set → Slack interface loads. |
| `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASS` / `DB_DATABASE` | no | `localhost`/`5432`/`ai`/`ai`/`ai` | Postgres connection (id `agentos-db`). |
| `DB_DRIVER` | no | `postgresql+psycopg` | SQLAlchemy driver. |
| `AGNO_DEBUG` | no | `False` | `True` → agno logs each tool call (`Running: <tool>(`). Set in `.env` for dev. |

## More

- **Agno dev (everything):** `docs/AGNO_DEV_GUIDE.md` — stack, gotchas, MCP usage patterns,
  and the agent lifecycle (`~/.claude/skills/agno-dev/` no longer exists in this repo).
- **Live Agno API docs:** `agno-docs` MCP (`.mcp.json`) · <https://docs.agno.com/llms-full.txt>.
