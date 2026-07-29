# CUSTOMIZATIONS.md

Living record of how this repo diverges from the upstream **agno-agi** template.
Update it whenever you change the template, so a future rebase/audit knows what's
ours vs. upstream.

> **Method:** every claim below was verified with `git diff <upstream>..HEAD`, not
> assumed. The authoritative command is:
> ```bash
> git diff 113864d..HEAD            # full divergence vs upstream main
> git log  113864d..HEAD --oneline  # our commits on top of upstream
> ```

---

## Upstream source

- **Original:** `https://github.com/agno-agi/agentos-docker-template`
- **Our base commit:** `113864d` (upstream `main` HEAD at fork time). We are **4 commits ahead, 0 behind**.
- **agno version pinned:** `agno[os,slack]` → `agno==2.6.7` (`pyproject.toml` / `requirements.txt`).
- **Remotes:**
  - **GitLab `gitlab` — source of truth / Coolify deploys from here:** `git@rmvc01.rm.udg.de:customer-udg-ai/projects/poc-agno-docker.git` (branch `main`, currently `df9efbf`).
  - **GitHub `origin` (fork):** `https://github.com/UDG-Dirk/agentos-docker-template` — **NOT kept in sync** (still at `09e1d86`; our agent work was pushed only to GitLab).

## Our commits on top of upstream

| Commit | What |
|---|---|
| `b37f1bc` | Dockerfile `CMD` → start uvicorn (was `chill`) |
| `348301f` | enable the MCP server endpoint at `/mcp` |
| `09e1d86` | add `fastmcp` dependency (needed by the MCP server) |
| `df9efbf` | **add Reasoning + Knowledge agents (Path B); scaffold `tools/` + `knowledge/`; env-configurable embedder** |

---

## Core changes (`app/`)

- **`app/main.py`** — registers our agents and wires startup:
  - `agents=[web_search, code_search, reasoning_agent, knowledge_agent]` (upstream had only `web_search`, `code_search`).
  - `enable_mcp_server=True` (MCP at `/mcp`).
  - Lifespan startup calls `ingest_dark_factory_kb()` (idempotent; wrapped in try/except so a KB/embedder failure can never block boot).
- **`app/config.yaml`** — added `quick_prompts` for `reasoning-agent` and `knowledge-agent`.
- **`app/settings.py`** — **UNCHANGED from upstream.** `default_model()` returns `OpenAIResponses(id="gpt-5.4")` (no env reads). The LiteLLM routing works because the OpenAI SDK picks up `OPENAI_BASE_URL` / `OPENAI_API_KEY` from the environment automatically. *(Note: earlier docs claimed settings.py reads `OPENAI_MODEL_ID`/`OPENAI_BASE_URL` — that is not the current code.)*

## Custom directories (NOT in upstream)

- **`agents/reasoning_agent.py`** *(new)* — strategic advisor using `ReasoningTools` (`think`/`analyze`).
  - **Uses `OpenAIChat`, deliberately NOT `default_model()`/`OpenAIResponses`.** On the `gpt-5.4 → Claude` LiteLLM route, `ReasoningTools` + `OpenAIResponses` fails with `"sequence item 0: expected str instance, NoneType found"`; `OpenAIChat` works. **Do not "fix" this to `default_model()`.**
- **`agents/knowledge_agent.py`** *(new)* — Dark Factory RAG. **Tool-free retrieval**: `add_knowledge_to_context=True`, `search_knowledge=False` (injects KB docs into the prompt instead of using the `search_knowledge_base` tool — avoids the Anthropic tool route).
- **`tools/`** *(new package)* — shared tool factories so agent files stay declarative:
  - `parallel_search.py` — `get_web_search_tools()` (Parallel SDK if `PARALLEL_API_KEY` else keyless MCP). Refactored out of `web_search.py`.
  - `context7.py` — example external-MCP wrapper (`get_context7_tools()`, `CONTEXT7_API_KEY` via client-side `Authorization: Bearer` header on `MCPTools`/`StreamableHTTPClientParams`).
- **`knowledge/`** *(new package)* — KB definitions + ingestion:
  - `dark_factory_kb.py` — `dark_factory_knowledge = create_knowledge("dark-factory", "dark_factory_kb")`, 4 Dark Factory concept docs, idempotent `ingest()`.

## Modified upstream files

- **`agents/web_search.py`** — refactored to `from tools.parallel_search import get_web_search_tools()`; the `web_search` Agent (id/params) is otherwise unchanged.
- **`agents/code_search.py`** — **UNCHANGED (upstream).**
- **`db/session.py`** — only change is an **env-configurable embedder id**: `EMBEDDER_ID = getenv("OPENAI_EMBEDDER_ID", "openai/text-embedding-3-small")`. The default is the **LiteLLM-prefixed** id — the bare `text-embedding-3-small` 401s against the proxy's virtual key. `get_postgres_db()` / `create_knowledge()` themselves are upstream.
- **`db/url.py`** — **UNCHANGED (upstream).** Builds `db_url` from `DB_HOST/PORT/USER/PASS/DATABASE` env (the DB is a separate pgvector container).

## Infrastructure

- **`Dockerfile`** — `CMD` runs uvicorn (`uvicorn app.main:app --host 0.0.0.0 --port 8000`); upstream shipped `["chill"]`. `EXPOSE 8000`, entrypoint `scripts/entrypoint.sh` (waits for DB, then execs). **Build Pack = Dockerfile; container port = 8000** (Coolify "Ports Exposes" must be 8000, not the 3000 default, or Traefik 502s).
- **`requirements.txt` / `pyproject.toml`** — added `fastmcp` (+ `agno[os,slack]`); pinned `agno==2.6.7`.
- **`example.env`** — added a `# --- MCP Tool Secrets ---` section (`CONTEXT7_API_KEY`).
- **`.gitignore`** — excludes local scratch **not** part of the deployable template: `.omc/`, `agents/--first-contact/`, `standalone/`; plus **signing material / live credentials** (`*.pem`, `*.key`, `*_token`, `*.jwt`, `.agno-keys/`) so a keypair or minted token can never be committed.
- **Untracked/gitignored scratch (ours, never deployed):** `agents/--first-contact/` (hackathon scripts + generated `output/`), `standalone/` (`hello_agent.py`, helix experiments), `.omc/`.
- **Project additions:** `scripts/mint_token.py` + `scripts/README.md` — mint AgentOS RS256 JWTs (BYO-keypair auth) and wire the `agno-prod` HTTP MCP into Claude Code; `docs/SETUP.md` §8 gained the token/MCP subsection.
- **Upstream, unchanged:** `evals/`, `compose.yaml`, `AGENTS.md`, `CLAUDE.md`, `.github/`, `.mcp.json`, `README.md`, `LICENSE`. (`docs/` + `scripts/` now carry the additions above.)

## Deploy / runtime notes (hard-won)

- **Coolify Git source** must be a **deploy-key (SSH)** source pointing at the GitLab URL — the "Public GitHub" source type mangles the self-hosted GitLab SSH URL.
- **`JWT_VERIFICATION_KEY`** (Coolify env) must be the RSA **public** PEM matching `~/.agno-keys/agno_private.pem`. Single-line is fine for PyJWT; **only literal `\n` escapes break parsing.** If it drifts, every token 401s.
- **DB:** separate pgvector container, reached via `DB_HOST` (Coolify internal hostname). The Postgres role password is set on **first volume init only** — rotating it later requires `ALTER USER agno WITH PASSWORD …` inside the DB container, not just an env change.
- **Embedder:** prod LiteLLM virtual key must allow `openai/text-embedding-3-small` (KB ingest 401s otherwise — wrapped, so it won't block startup, but knowledge retrieval won't work).

## Known upgrade risks

- **agno pip upgrade:** the agent API is stable across 2.6.x, but verified that `2.6.7 → 2.6.19` does **not** change the component-create path **and does not fix** the `ReasoningTools`/`OpenAIResponses` issue (that's a model-class choice, not a version bug). After any bump, run the agents on the **local dev harness** (`agno-setup/dev-up.sh`) before deploying.
- **Components API boundary:** agno has **TODO stubs** for knowledge/tool serialization (`agno/agent/_storage.py`). So today: **Path A (Components API, no deploy)** = config-only agents (model + instructions + memory + history + db); **tools and knowledge require Path B** (code-defined + redeploy). Re-evaluate if a future agno implements those stubs.
- **Rebase onto a newer upstream:** main conflict points are `agents/web_search.py` (refactored), `app/main.py` (agent registration + lifespan ingest), `db/session.py` (embedder id), `Dockerfile` (CMD), and `requirements.txt`. `tools/` and `knowledge/` are new packages → no conflicts.
