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
> assume a POSIX shell, so on Windows run everything inside WSL2, not native PowerShell/CMD.
> **This team runs bare-metal Postgres, no Docker, in local dev** (§5 Option A below). If you have
> no local Postgres at all and just want something running fast, `docker-compose.dev.yml` (§5
> Option B) is available as a fallback — it is not the supported path here.

- **Python 3.12** (3.11+ works; the pipeline targets 3.12).
- **[uv](https://docs.astral.sh/uv/)** — required by `./scripts/venv_setup.sh` (the recommended venv
  setup path in §3). Install it before running the script, or set up the venv by hand instead.
- **Node.js** — only if you run the Figma-extraction workflows (`npx` fetches the Framelink Figma
  MCP at runtime). Not needed just to boot the server.
- **Git**.
- **A bare-metal Postgres 14+** with `pgvector` installable (§5 Option A). Docker (§5 Option B) is a
  fallback only, not used by this team.
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

This needs [`uv`](https://docs.astral.sh/uv/) installed (the script exits with an install link if
it isn't). It removes any existing `.venv`, creates a fresh Python 3.12 venv with `uv`, installs
`requirements.txt`, and installs the project itself in editable mode with dev dependencies
(`uv pip install -e .[dev]`).

Prefer to do it by hand, without `uv`?

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .[dev]
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
- **Database:** the defaults (`ai` / `ai` / `ai` on `localhost:5432`) match §5's setup below.
  Change them only if you use a different Postgres.
- **Figma:** set `FIGMA_PAT` only if you'll run the Figma-extraction workflows. Get one at
  **figma.com → your avatar → Settings → Security tab → Personal access tokens → "Generate new
  token"**. A name + **read** access to *file content* is enough (the extractor only reads). Copy the
  token immediately — Figma shows it **once** — and paste it into `.env` as `FIGMA_PAT`.

`.env` is gitignored — never commit real keys. Full variable reference: [`ENV.md`](ENV.md).

---

## 5. Database — Postgres + pgvector

### Option A — Bare-metal Postgres (this team's setup; Linux commands, adapt for macOS/Windows)

If you already run Postgres locally, create the database + user and enable pgvector. The commands
below are Linux-flavored (`sudo -u postgres psql`); on macOS use your Homebrew Postgres (`psql
postgres`), and on Windows/WSL adjust to your install:

```bash
# as a Postgres superuser (e.g. `sudo -u postgres psql`):
CREATE USER ai WITH PASSWORD 'ai';
CREATE DATABASE ai OWNER ai;
\c ai
CREATE EXTENSION IF NOT EXISTS vector;   -- requires the pgvector package installed on the server
```

`pgvector` install varies by platform (e.g. `apt install postgresql-16-pgvector`, or `brew install
pgvector`). See <https://github.com/pgvector/pgvector#installation>. On a Postgres version/package
mismatch (the most common friction point on a fresh WSL Ubuntu box), match the `postgresql-<ver>-pgvector`
package to the Postgres major version you installed.

### Option B — Docker (fallback only, not used by this team)

If you have no local Postgres at all and just want something running fast for a one-off check:

```bash
docker compose -f docker-compose.dev.yml up -d
```

This starts Postgres 16 with `pgvector` preinstalled, database/user/password all `ai`, on
`localhost:5432`. Stop it with `docker compose -f docker-compose.dev.yml down` (add `-v` to wipe the
data). The app enables the `vector` extension itself on first connect. This is the **only** container
this repo uses in local dev — there is no app container, and this option is not the supported path
on this team's machines.

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
`JWT_VERIFICATION_KEY` (the RSA **public** key). You do **not** need this for local dev
(`RUNTIME_ENV=dev` has no auth). To call the deployed prod AgentOS, mint yourself a token.

**Full key custody, rotation, and the CI-variable setup live in [`AUTH_KEYS.md`](AUTH_KEYS.md) — read
that for anything beyond minting your own token.**

### Preferred — the `mint-token` CI job (you never touch the private key)

The signing key stays server-side as a **protected group CI variable** (`AGNO_PRIVATE_KEY`); the
pipeline signs for you:

1. GitLab → **Build → Pipelines → Run pipeline** on `main` (the protected default branch).
2. Add a variable **`MINT_USER`** = your handle. Optional: `MINT_DAYS` (default `30`), `MINT_SCOPES`.
3. Run it → open the `mint-token` job → **download the `agno_mcp_token` artifact** (it expires in
   1 day; the token itself lasts `MINT_DAYS`). Never commit it.

The job only runs on a web/manual pipeline on the protected `main` branch (that's what exposes the
protected key). Setup of the `AGNO_PRIVATE_KEY` CI variable + the whole key lifecycle:
[`AUTH_KEYS.md`](AUTH_KEYS.md).

### Local — `scripts/mint_token.py` (only if you hold the private key)

If the private key was handed to you out-of-band:

```bash
python3 scripts/mint_token.py --user <handle> --days 30 > ~/.agno-keys/agno_mcp_token
```

The private key lives at `~/.agno-keys/agno_private.pem` (chmod 600), delivered out-of-band — never
committed (`.gitignore` blocks `*.pem` / `*_token` / `*.jwt`).

To wire a minted token into Claude Code (the `agno-prod` HTTP MCP) or any API client, see the
clone → mint → wire walkthrough in [`scripts/README.md`](../scripts/README.md).
