# helix-agents — Local Dev Setup

Get a fresh clone of `msq-turbo/helix-agents` from nothing to a **running server answering
`/health` with `200`**, on your own machine. Written for UDG + HELIX-product-team colleagues who
have never run this stack. Cold-start reading order: **SETUP.md → ARCHITECTURE.md →
agents/\<agent\>/README.md**.

> **The app runs bare-metal** (a Python virtualenv + `uvicorn`). The **only** thing that runs in a
> container is the Postgres database, and even that is optional (you can use a bare-metal Postgres
> instead). There is no Dockerfile to build for local dev.

---

## 0. The one-minute picture

You need three things running/available:

1. **The app** — this repo, in a Python virtualenv, started with `uvicorn`.
2. **A database** — Postgres with the `pgvector` extension (sessions, memory, knowledge).
3. **A model backend** — the app talks the plain OpenAI API and points at whatever
   `OPENAI_BASE_URL` says. For our team that's the **shared LiteLLM proxy** (a gateway that holds the
   model keys centrally). It is a convenience, not a hard requirement — any OpenAI-compatible
   endpoint works — but the shared proxy is the supported path here.

`/health` returns `200` as soon as (1) and (2) are up — it does **not** need the model backend, so you
can confirm the server boots before you have a proxy key.

---

## 1. Prerequisites

> **Platforms:** Linux, **macOS** (incl. Apple Silicon), or **Windows via WSL2** — the setup scripts
> assume a POSIX shell, so on Windows run everything inside WSL2, not native PowerShell/CMD. On macOS
> and Windows, use **Docker Desktop** for the database (§5 Option A) — the `pgvector/pgvector` image is
> multi-arch (amd64 + arm64), and `psycopg-binary` ships wheels for all three platforms, so no
> compiler is needed.

- **Python 3.12** (3.11+ works; the pipeline targets 3.12).
- **Node.js** — only if you run the Figma-extraction workflows (`npx` fetches the Framelink Figma
  MCP at runtime). Not needed just to boot the server.
- **Git**.
- **A database**, one of:
  - **Docker** (easiest — use the provided `docker-compose.dev.yml`), or
  - a **bare-metal Postgres 14+** with `pgvector` installable.
- **LiteLLM proxy** — your proxy URL + API token, set as `OPENAI_BASE_URL` + `OPENAI_API_KEY`. (You
  can boot the server and hit `/health` without it; you only need it for anything that calls a model.)

---

## 2. Clone

```bash
git clone git@rmvc01.rm.udg.de:msq-turbo/helix-agents.git
cd helix-agents
```

Everything below runs from the repo root.

---

## 3. Python virtualenv

Create the venv and install dependencies (the helper script does both):

```bash
./scripts/venv_setup.sh
source .venv/bin/activate
```

Prefer to do it by hand? That's all the script does:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 4. Configure `.env`

Copy the template and fill in the placeholders:

```bash
cp .env.example .env
```

Then edit `.env`:

- **Models (LiteLLM proxy):** set `OPENAI_BASE_URL` + `OPENAI_API_KEY` to your LiteLLM proxy URL and
  token. `OPENAI_MODEL_ID` and `OPENAI_EMBEDDER_ID` are pre-filled with working defaults. Keep the
  `openai/` prefix on `OPENAI_EMBEDDER_ID` — a bare id is rejected by the proxy.
- **Database:** the defaults (`ai` / `ai` / `ai` on `localhost:5432`) match the Docker option in the
  next step. Change them only if you use a different Postgres.
- **Figma:** set `FIGMA_PAT` only if you'll run the Figma-extraction workflows (figma.com → Settings →
  Personal access tokens; read access to file content is enough).

`.env` is gitignored — never commit real keys. Full variable reference: [`ENV.md`](ENV.md).

---

## 5. Database — Postgres + pgvector

### Option A — Docker (recommended, one command)

```bash
docker compose -f docker-compose.dev.yml up -d
```

This starts Postgres 16 with `pgvector` preinstalled, database/user/password all `ai`, on
`localhost:5432`. Stop it with `docker compose -f docker-compose.dev.yml down` (add `-v` to wipe the
data). The app enables the `vector` extension itself on first connect.

### Option B — Bare-metal Postgres (Linux; adapt for macOS/Windows)

If you already run Postgres locally, create the database + user and enable pgvector. The commands
below are Linux-flavored (`sudo -u postgres psql`); on macOS use your Homebrew Postgres (`psql
postgres`), and on Windows/WSL adjust to your install. **If in doubt, use Option A (Docker)** — it's
identical on every platform:

```bash
# as a Postgres superuser (e.g. `sudo -u postgres psql`):
CREATE USER ai WITH PASSWORD 'ai';
CREATE DATABASE ai OWNER ai;
\c ai
CREATE EXTENSION IF NOT EXISTS vector;   -- requires the pgvector package installed on the server
```

`pgvector` install varies by platform (e.g. `apt install postgresql-16-pgvector`, or `brew install
pgvector`). See <https://github.com/pgvector/pgvector#installation>.

---

## 6. Run it → `/health` 200

```bash
dotenv run -- uvicorn app.main:app --reload --port 8000
```

In another terminal:

```bash
curl -sf http://localhost:8000/health && echo "  ← server is up"
```

A `200` means the app booted and reached the database. In `dev` (`RUNTIME_ENV=dev`) there is **no
auth**, so no token is needed locally. On startup the app also tries to ingest its knowledge base;
if your model key isn't set yet that step logs a soft warning and is skipped — it does **not** block
startup or `/health`.

You now have a working local AgentOS. The interactive API surface is at
`http://localhost:8000` (AgentOS routes + the deployed workflows).

---

## 7. Framelink Figma MCP (only for the extraction workflows)

No pre-install needed — on first run `npx` fetches the pinned package:

```bash
npx -y figma-developer-mcp@0.13.2 --figma-api-key=$FIGMA_PAT --stdio --format json
```

Requires Node.js. The PAT is injected at the MCP layer via `StdioServerParameters(env=...)`; it is
never logged. (Why the version pin + `--format json` are load-bearing:
[`agents/figma_extractor/README.md`](../agents/figma_extractor/README.md).)

---

## 8. Run the tests

```bash
source .venv/bin/activate
python -m pytest -q                                  # whole suite
python -m pytest tests/test_deterministic_extractor.py -q   # Figma extractor (live smokes gated on FIGMA_PAT)
python -m pytest tests/test_token_normalizer.py -q          # token normalizer
python -m pytest tests/baseline_reader/ -q                  # baseline reader
```

Tests that hit the live Figma API are skipped unless `FIGMA_PAT` is set.

---

## 9. Production secrets (Coolify)

On prod, secrets are **Coolify environment variables**, not `.env` files. AgentOS deploys to Coolify
at `poc-agno-api.services.plygrnd.tech`; deploy = push to `main` (Coolify auto-builds).

With `RUNTIME_ENV=prd`, every request needs a **Bearer RS256 JWT**, verified against
`JWT_VERIFICATION_KEY` (the RSA **public** key). Mint client tokens with
[`scripts/mint_token.py`](../scripts/mint_token.py):

```bash
python3 scripts/mint_token.py --user <handle> --days 30 > ~/.agno-keys/agno_mcp_token
```

- The private key lives at `~/.agno-keys/agno_private.pem` (chmod 600), delivered **out-of-band** —
  never committed (`.gitignore` blocks `*.pem` / `*_token` / `*.jwt`).
- To reach the deployed AgentOS from Claude Code (the `agno-prod` HTTP MCP) or any API client, see the
  clone → mint → wire walkthrough in [`scripts/README.md`](../scripts/README.md).
- Team self-serve minting (CI job, no key handling) + key custody / rotation:
  [`AUTH_KEYS.md`](AUTH_KEYS.md).
