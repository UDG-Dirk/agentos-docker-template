# HELIX UC2 PoC — Environment Setup

Setup from scratch for any HELIX agent developer. Cold-start reading order: **SETUP.md → ARCHITECTURE.md → agents/\<agent\>/README.md**.

---

## 1. Prerequisites

- **Node.js** — required at runtime so `npx` can fetch the Framelink Figma MCP
- **Python 3.11+** — pipeline targets 3.12; agno 2.6.7
- **LiteLLM proxy access** — prod: `https://litellm.services.plygrnd.tech`, or local: `http://localhost:4000`
- **Git**

> **No Docker on local dev.** The stack is bare-metal (venv + uvicorn). Do not look for or create Dockerfiles or compose files locally.

---

## 2. Repository Layout

```
~/opencode/workbench/
├── helix-poc-agno/          # This repo — HELIX pipeline PoC (agents, tests, docs)
└── agno-setup/
    └── poc-agno-template/   # Deployable AgentOS app (sibling dir)
                             # HELIX reuses its venv and model credentials
```

HELIX agents live in `helix-poc-agno/` for the duration of the PoC. Promotion to `poc-agno-template/` is a deliberate productionize step (see `decision:agent-repo-location`).

---

## 3. Python venv — Reuse the Template's

Do **not** create a local venv. Reuse the one in `poc-agno-template/`:

```bash
source ~/opencode/workbench/agno-setup/poc-agno-template/.venv/bin/activate
```

Or invoke directly without activating:

```bash
~/opencode/workbench/agno-setup/poc-agno-template/.venv/bin/python ...
```

**Why:** agno and all dependencies already live there; a second venv drifts. pytest is installed there too.

---

## 4. .env Files — Two Files, Two Locations (Dual-Dotenv)

| File | Keys | Purpose |
|---|---|---|
| `agno-setup/poc-agno-template/.env` | `OPENAI_MODEL_ID` (gpt-5.4), `OPENAI_BASE_URL`, `OPENAI_API_KEY` | LiteLLM proxy / model credentials |
| `helix-poc-agno/.env` | `FIGMA_PAT`, `GITLAB_PAT` | Figma + GitLab tokens |

### Generating tokens

**Figma PAT:** figma.com → Settings → Security → Personal access tokens → generate. Read access to file content is sufficient. Used by the Framelink MCP.

**GitLab PAT** (self-hosted `rmvc01.rm.udg.de`): create a token with the **`api`** scope.

> **CRITICAL:** On this instance, `read_repository`/`write_repository` authorize git-over-HTTPS only. The repository REST API (branches, commits, files) returns `403 insufficient_scope` without `api`. Use `read_api` for read-only access. See `lesson:gitlab-pat-scopes-self-hosted`.

### Security rules

- Never commit `.env` — `helix-poc-agno/.gitignore` excludes it.
- Never copy the Figma or GitLab PAT into `poc-agno-template/.env`.
- Never put any PAT in workflow `session_state`.

---

## 5. Dual Dotenv Loading

Agent scripts call `python-dotenv` twice with `override=False`:

1. Load `helix-poc-agno/.env` first — picks up `FIGMA_PAT` and `GITLAB_PAT`
2. Load `poc-agno-template/.env` second — picks up model credentials

`override=False` ensures neither file clobbers the other. See `agents/figma_extractor/agent.py` for the reference implementation.

---

## 6. Framelink Figma MCP

No pre-install needed. On first run, `npx` fetches the package automatically:

```bash
npx -y figma-developer-mcp --figma-api-key=$FIGMA_PAT --stdio
```

Requires Node.js. The PAT is injected at the MCP layer via `StdioServerParameters(env=...)`; it is never logged. The server writes downloaded assets relative to its working directory (`cwd`), so the agent sets `cwd` to the agent directory.

---

## 7. First-Run Verification

Run the extractor against the test Figma file:

```bash
cd ~/opencode/workbench/agno-setup/poc-agno-template

.venv/bin/python ~/opencode/workbench/helix-poc-agno/agents/figma_extractor/agent.py \
  --pattern sequential \
  --out ~/opencode/workbench/helix-poc-agno/agents/figma_extractor/runs/run_verify.json
```

**Expected result** (test file `8qPSyetzviLR6eF6bkpL44`):

- ~25 pages, ~100 tokens, 6 components
- ~20 SVG assets written to `runs/assets/`
- `status: completed`
- ~230 s total

Then run the test harness:

```bash
cd ~/opencode/workbench/helix-poc-agno

~/opencode/workbench/agno-setup/poc-agno-template/.venv/bin/python \
  -m pytest tests/test_figma_extractor.py \
  --output=agents/figma_extractor/runs/run_verify.json \
  -q
```

---

## 8. Production Secrets (Coolify)

On prod, secrets go in **Coolify environment variables** — not in `.env` files.

AgentOS deploys to the `projects-01` Coolify server (`poc-agno-api.services.plygrnd.tech`). Review the prod launch checklist (`bestpractice:helix-workflow-prod-launch-checklist`) before deploying.

> Coolify is playground/experimentation only — no SLA. Operational workloads migrate to IT-managed infrastructure later.

### AgentOS access token (MCP / API)

With `RUNTIME_ENV=prd`, every request needs a **Bearer RS256 JWT**, verified against
`JWT_VERIFICATION_KEY` (the RSA **public** key; must match the private key below). Mint client
tokens with **[`scripts/mint_token.py`](../scripts/mint_token.py)**:

```bash
python3 scripts/mint_token.py --user <handle> --days 30 > ~/.agno-keys/agno_mcp_token
```

- Private key lives at `~/.agno-keys/agno_private.pem` (chmod 600), delivered **out-of-band** —
  never committed (`.gitignore` blocks `*.pem` / `*_token` / `*.jwt`).
- To reach the deployed AgentOS from **Claude Code** (the `agno-prod` HTTP MCP) or any API client,
  see the full clone → mint → wire walkthrough in **[`scripts/README.md`](../scripts/README.md)**.
- Squad self-serve minting (CI job, no key handling) + key custody / rotation / revocation:
  **[`AUTH_KEYS.md`](AUTH_KEYS.md)**.
