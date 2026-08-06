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
- **Our base commit:** `113864d` (upstream `main` HEAD at fork time). We are **~100+ commits ahead, 0 behind** — re-check with `git rev-list --count 113864d..HEAD` rather than trusting a hardcoded number.
- **agno version pinned:** `agno[os,slack]` → `agno==2.6.7` (`pyproject.toml` / `requirements.txt`).
- **Remotes:**
  - **GitLab `gitlab` — source of truth / Coolify deploys from here** (branch `main`). Canonical clone URL: **GitLab → Clone** (the project has been transferred before — `customer-udg-ai/projects/poc-agno-docker` → `msq-turbo/helix-agents` as of 2026-07-29; prefer the Clone button over a hardcoded path, and no pinned SHA here so it can't go stale).
  - **GitHub `origin` (fork):** `https://github.com/UDG-Dirk/agentos-docker-template` — **NOT kept in sync** (still at `09e1d86`; our agent work was pushed only to GitLab).

## What we changed on top of upstream

We're ~100+ commits ahead, so this is grouped by theme rather than listed commit-by-commit —
run `git log 113864d..HEAD --oneline` for the authoritative list.

- **Template hardening (earliest work).** Dockerfile `CMD` starts uvicorn (was `chill`); MCP server
  endpoint enabled at `/mcp`; added the `fastmcp` dependency.
- **Extra template agents.** Added the Reasoning and Knowledge agents; scaffolded the `tools/` and
  `knowledge/` packages; made the embedder id env-configurable. (Code-defined agents that need a
  redeploy — what this doc calls **Path B** further down.)
- **HELIX design-token pipeline (the bulk of the work).** A deterministic, zero-LLM pipeline that
  reads a client's Figma design system and emits machine-readable inventories of its tokens and
  components. Four workflows are registered in `app/main.py`: `helix-figma-extractor`,
  `helix-client-extractor`, `helix-composition-only-extractor`, and `helix-baseline-reader`. The
  per-stage logic lives in `agents/figma_extractor/`, `agents/token_normalizer/`, and
  `agents/baseline_reader/` (each README carries a plain-language glossary of terms like "lane",
  "Core", "Composition mode"). A fifth stage, the Semantic Matcher (`agents/semantic_matcher/`), is a
  tested library, not yet a running endpoint.
- **CEM delivery.** A GitLab CI job (`build-cem` in `.gitlab-ci.yml`) publishes the compiled
  design-system metadata (the "Custom Elements Manifest") to the GitLab package registry at a stable
  URL; the container fetches it at startup via `scripts/fetch_cem.py`, configured by the
  `HELIX_CODE_CEM_*` environment variables.
- **Self-serve local dev.** `.env.example` + `docker-compose.dev.yml` + `docs/SETUP.md` bring a fresh
  clone up to a `/health` 200 with no tribal knowledge.
- **Enrichment quarantined.** The "enrichment" layer (Figma Variable slash-paths / Code Connect
  metadata) is intentionally inactive in the live pipeline (`enrichment_coverage=0.0`), kept as a
  reserved concept — see `agents/token_normalizer/normalizer.py`.

---

## Core changes (`app/`)

- **`app/main.py`** — registers our agents + workflows and wires startup:
  - `agents=[web_search, code_search, reasoning_agent, knowledge_agent]` (upstream had only `web_search`, `code_search`).
  - `workflows=[...]` — the four HELIX pipeline workflows (`helix-figma-extractor`, `helix-client-extractor`, `helix-composition-only-extractor`, `helix-baseline-reader`); upstream had none.
  - `enable_mcp_server=True` (MCP at `/mcp`).
  - Lifespan startup calls `ingest_dark_factory_kb()` (idempotent; wrapped in try/except so a KB/embedder failure can never block boot).
- **`app/config.yaml`** — added `quick_prompts` for `reasoning-agent` and `knowledge-agent`.
- **`app/settings.py`** — **changed from upstream.** `default_model()` returns `OpenAIResponses(id=OPENAI_MODEL_ID)`, reading `OPENAI_MODEL_ID` from the env (default `gpt-5.4`); a `default_chat_model()` factory was added that returns an `OpenAIChat` model for tool-using agents. LiteLLM routing works because the OpenAI SDK also picks up `OPENAI_BASE_URL` / `OPENAI_API_KEY` from the environment.

## Custom directories (NOT in upstream)

- **`agents/reasoning_agent.py`** *(new)* — strategic advisor using `ReasoningTools` (`think`/`analyze`).
  - **Uses `default_chat_model()` (`OpenAIChat`), deliberately NOT `default_model()`/`OpenAIResponses`.** On the `gpt-5.4 → Claude` LiteLLM route, `ReasoningTools` + `OpenAIResponses` fails with `"sequence item 0: expected str instance, NoneType found"`; `OpenAIChat` works. **Do not "fix" this to `default_model()`.**
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
- **`.env.example`** (the canonical template — see [`docs/SETUP.md`](docs/SETUP.md)) — added a
  `# --- MCP Tool Secrets ---` section (`CONTEXT7_API_KEY`). A second, no-dot-prefix `example.env`
  predated this rename, had drifted (its JWT-setup comment contradicted
  [`docs/AUTH_KEYS.md`](docs/AUTH_KEYS.md)), and was removed 2026-08-06 — `.env.example` is the
  only template now.
- **`.gitignore`** — excludes local scratch **not** part of the deployable template: `.omc/`, `agents/--first-contact/`, `standalone/`; plus **signing material / live credentials** (`*.pem`, `*.key`, `*_token`, `*.jwt`, `.agno-keys/`) so a keypair or minted token can never be committed.
- **Untracked/gitignored scratch (ours, never deployed):** `agents/--first-contact/` (hackathon scripts + generated `output/`), `standalone/` (`hello_agent.py`, helix experiments), `.omc/`.
- **Project additions:** `scripts/mint_token.py` + `scripts/README.md` — mint AgentOS RS256 JWTs (BYO-keypair auth) and wire the `agno-prod` HTTP MCP into Claude Code; `docs/SETUP.md` §9 gained the token/MCP subsection.
- **Also customized:** `README.md` (HELIX header + pipeline overview), `AGENTS.md`/`CLAUDE.md` (HELIX pipeline note), and the primary CI is now **`.gitlab-ci.yml`** (security scanning + the `build-cem` and `mint-token` jobs), not the upstream `.github/` workflows. `docs/` + `scripts/` carry the additions above.
- **Upstream, unchanged:** `evals/`, `compose.yaml`, `.mcp.json`, `LICENSE`.

## Deploy / runtime notes (hard-won)

- **Coolify Git source** must be a **deploy-key (SSH)** source pointing at the GitLab URL — the "Public GitHub" source type mangles the self-hosted GitLab SSH URL.
- **`JWT_VERIFICATION_KEY`** (Coolify env) must be the RSA **public** PEM matching `~/.agno-keys/agno_private.pem`. Single-line is fine for PyJWT; **only literal `\n` escapes break parsing.** If it drifts, every token 401s.
- **DB:** separate pgvector container, reached via `DB_HOST` (Coolify internal hostname). The Postgres role password is set on **first volume init only** — rotating it later requires `ALTER USER agno WITH PASSWORD …` inside the DB container, not just an env change.
- **Embedder:** prod LiteLLM virtual key must allow `openai/text-embedding-3-small` (KB ingest 401s otherwise — wrapped, so it won't block startup, but knowledge retrieval won't work).

## Known upgrade risks

- **agno pip upgrade:** the agent API is stable across 2.6.x, but verified that `2.6.7 → 2.6.19` does **not** change the component-create path **and does not fix** the `ReasoningTools`/`OpenAIResponses` issue (that's a model-class choice, not a version bug). After any bump, run the agents locally (bare-metal `uvicorn` per `docs/SETUP.md`, or the `docker-compose.dev.yml` Postgres) before deploying.
- **Components API boundary:** agno has **TODO stubs** for knowledge/tool serialization (`agno/agent/_storage.py`). So today: **Path A (Components API, no deploy)** = config-only agents (model + instructions + memory + history + db); **tools and knowledge require Path B** (code-defined + redeploy). Re-evaluate if a future agno implements those stubs.
- **Rebase onto a newer upstream:** main conflict points are `agents/web_search.py` (refactored), `app/main.py` (agent registration + lifespan ingest), `db/session.py` (embedder id), `Dockerfile` (CMD), and `requirements.txt`. `tools/` and `knowledge/` are new packages → no conflicts.
