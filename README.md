# HELIX Agents (AgentOS)

> **This repo = `helix-agents`** (HELIX PoC AgentOS; hosted on GitLab under `msq-turbo/helix-agents`,
> deployed to Coolify at `poc-agno-api.services.plygrnd.tech`). It began as the upstream
> **AgentOS Docker Template** (below, kept largely intact) — what we changed is tracked in
> [`CUSTOMIZATIONS.md`](CUSTOMIZATIONS.md).
>
> **New here?** Clone via **GitLab → Clone** (the project has been transferred before — don't trust a
> hardcoded path). Then: local dev setup → [`docs/SETUP.md`](docs/SETUP.md); AgentOS access token +
> Claude Code MCP → [`scripts/README.md`](scripts/README.md); the Figma extractor →
> [`agents/figma_extractor/README.md`](agents/figma_extractor/README.md).
>
> *(All the links above are repo-relative on purpose, so they survive the next rename/transfer.)*

---

An agent platform you build, improve, and run using coding agents.

The app itself runs **bare-metal** locally (a Python virtualenv + `uvicorn`); the only container is its
Postgres database. It runs behind your auth, with all your data in your own database. Because trace
data, agent code, system logs, and the iteration tools all live in one place, coding agents like Claude
Code can read, update, and improve the platform end-to-end.

## Built for coding agents

This codebase is designed primarily for coding agents. It comes with five prompts that cover the full agent development lifecycle:

1. **Create.** Claude asks a few questions, scaffolds the agent file, registers it in `app/main.py`, adds quick prompts to `app/config.yaml`, restarts the container, and smoke-tests via cURL. Usually 5-10 minutes for a simple agent.
2. **Improve.** Hardens and fine-tunes your agent based on its existing spec. Claude derives probes from the agent's `INSTRUCTIONS`, runs them against the live container, judges the responses, and edits until they pass. No input from you.
3. **Extend.** Add a new feature to an agent. You direct, Claude executes. Add tools, refine prompts, fix bugs. The Agno docs MCP is loaded so toolkit research is grounded in the real API.
4. **Hill Climb.** Claude runs the eval suite, diagnoses failures, and fixes what's in scope. Stops when all cases pass.
5. **Review.** Claude sweeps the repo for drift between docs, code, and config. Auto-fixes mechanical drift like stale paths and missing env vars; flags anything bigger.

3 of 5 run autonomously with no input needed from you.

## What's Included

| Agent | Pattern | Description |
|-------|---------|-------------|
| WebSearch | Direct tools | Search the web using Parallel SDK or keyless MCP. |
| CodeSearch | Context provider | Answer questions about this codebase. |
| Reasoning | Direct tools | Strategic advisor using `ReasoningTools` (`think`/`analyze`). |
| Knowledge | Context provider | Answers grounded in the Dark Factory knowledge base (tool-free RAG). |

> See [`CUSTOMIZATIONS.md`](CUSTOMIZATIONS.md) for how this repo diverges from the upstream agno-agi template (added agents, env-configurable embedder, deploy notes).

## The HELIX pipeline — what this project actually does

New to the project (or to Agno)? Start here. **HELIX turns a client's Figma design file into a
usable, standards-compliant design system** — the colours, spacing, fonts, and components a
front-end team can build with. It does that as a short assembly line of steps.

Two kinds of step, and the difference matters:

- **Deterministic workflow** — plain Python, **no AI/LLM involved**. Same input always gives the same
  output. Most of the pipeline is this, on purpose: reading a structured design file is a job for
  code, not a language model (an earlier LLM version invented components that weren't in the file —
  exactly what we don't want).
- **AI-assisted step** — uses a language model for judgement calls, and asks a human when it's unsure.
  Only one step needs this.

| Step | What it does, in plain terms | Kind | Status |
|---|---|---|---|
| **Figma Extractor** | Reads a Figma file and pulls its building blocks — colours, spacing, fonts, components, and its design-token catalog — into one structured inventory. | Deterministic workflow (no AI) | **Live** |
| **Client Extractor** | Same idea, for a client file: also follows the components a client borrows from shared libraries and resolves them. | Deterministic workflow (no AI) | **Live** |
| **Composition-Only Extractor** | Extractor for a standalone / unpublished file whose components live as page frames rather than a published library. | Deterministic workflow (no AI) | **Live** |
| **Token Normalizer** | Cleans the extracted tokens into a standards-compliant token tree (W3C Design Tokens), then **pauses for a person to review** before moving on. | Deterministic step (no AI) + human review | **Live** |
| **Baseline Reader** | Reads the team's existing baseline design system into an inventory, so later steps can compare a client against it. | Deterministic workflow (no AI) | **Live** |
| **Semantic Matcher** | Matches each client token/component to its closest counterpart in the baseline, with a confidence score — and asks a human when it isn't sure. | AI-assisted (the one LLM step) | Tested library; live endpoint planned |
| **Theme Generator** | Turns the matches into the CSS theme overrides for that client. | Deterministic workflow (no AI) | Planned |
| **New-Component Builder** | Scaffolds only the genuinely new components a client needs, following the baseline's conventions. | Deterministic workflow (no AI) | Planned |

```mermaid
flowchart TD
    F["Client Figma file"] --> EX["Figma Extractor<br/>(deterministic)"]
    EX --> TN["Token Normalizer<br/>(deterministic + human review)"]
    B["Baseline design system"] --> BR["Baseline Reader<br/>(deterministic)"]
    TN --> SM["Semantic Matcher<br/>(AI-assisted · endpoint planned)"]
    BR --> SM
    SM --> TG["Theme Generator<br/>(planned)"]
    SM --> NC["New-Component Builder<br/>(planned)"]
    TG --> OUT["Themed design system<br/>(CSS + components)"]
    NC --> OUT
```

Each live step is an Agno **workflow** you can call over HTTP; the deep-dive for each lives in its own
README under [`agents/`](agents/) (each opens with a plain-language glossary). Architecture overview:
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Get Started

### Step 1: Run locally

The full, verified walkthrough (prerequisites, database, model access, troubleshooting) is in
[`docs/SETUP.md`](docs/SETUP.md) — it takes a clean clone to a running server. The short version:

```sh
# Clone via GitLab → Clone (the project has been transferred before — don't trust a hardcoded path)
cd helix-agents

./scripts/venv_setup.sh && source .venv/bin/activate   # Python venv + deps
cp .env.example .env                                    # then fill in the placeholders

docker compose -f docker-compose.dev.yml up -d          # Postgres + pgvector (the only container)
dotenv run -- uvicorn app.main:app --reload --port 8000 # the app, bare-metal
```

Confirm it's up: `curl -sf http://localhost:8000/health` returns `200` (no auth in local dev). The API
docs are at [http://localhost:8000/docs](http://localhost:8000/docs).

### Step 2: Connect to the Web UI

1. Open [os.agno.com](https://os.agno.com) and login
2. Add OS → Local → `http://localhost:8000`
3. Click "Connect"

### Step 3: Stop the application

Stop the server with `Ctrl-C` in its terminal. To stop the database too:

```sh
docker compose -f docker-compose.dev.yml down     # add -v to also wipe the data
```

## Extending the Platform

### Multi-agent teams and workflows

For most things one agent is enough. When it isn't:

- **[Multi-agent teams](https://docs.agno.com/teams/overview).** Coordinate (a leader plans and synthesizes), route (a router picks the right specialist), or broadcast (run everyone in parallel). Use when the right specialist isn't known up front.
- **[Agentic workflows](https://docs.agno.com/workflows/overview).** Deterministic step-by-step pipelines. Use when a process needs to run the same way every time.

Rule of thumb: agents for open questions, teams for routing, workflows for processes.

### Scheduled tasks

`scheduler=True` is on in [`app/main.py`](app/main.py). Schedule any agent or workflow on a cron:

- **Maintenance.** Purge sessions older than 90 days. Vacuum tables.
- **Proactive runs.** Every weekday morning, summarize overnight news for your portfolio and send to Slack.
- **Periodic re-evaluation.** Wrap the eval suite as a scheduled workflow to catch behavior drift before users do.

See [Agno scheduler docs](https://docs.agno.com/agent-os/scheduler) for the cron API.

### Interfaces

Agents should live where your users are. Slack, Discord, Telegram, custom UIs in your product.

**Slack** is pre-wired. Set `SLACK_BOT_TOKEN` and `SLACK_SIGNING_SECRET` in your `.env` and the interface lights up automatically. See [`app/main.py`](app/main.py):

```python
interfaces: list = []
if SLACK_BOT_TOKEN and SLACK_SIGNING_SECRET:
    from agno.os.interfaces.slack import Slack

    interfaces.append(
        Slack(
            agent=code_search,
            streaming=True,
            token=SLACK_BOT_TOKEN,
            signing_secret=SLACK_SIGNING_SECRET,
            resolve_user_identity=True,
        )
    )
```

Swap the `agent=` arg to route Slack to a different agent. For the Slack-side app setup, see the [Agno Slack docs](https://docs.agno.com/agent-os/interfaces/slack/introduction).

For Discord, Telegram, WhatsApp, or a custom UI, mirror the same conditional with the relevant interface from Agno. See the [Agno interfaces guide](https://docs.agno.com/agent-os/interfaces/overview).

### Tools and MCP servers

The WebSearch agent in [`agents/web_search.py`](agents/web_search.py) shows the MCPTools pattern (URL plus transport). Copy it to wire any MCP server.

For built-in toolkits, Agno ships 100+. A typical wire-up is three lines:

```python
from agno.tools.linear import LinearTools

linear_agent = Agent(
    id="linear",
    model=default_model(),
    tools=[LinearTools()],
    instructions="You triage issues in Linear.",
    db=get_postgres_db(),
)
```

See [Agno tools](https://docs.agno.com/tools/toolkits) for the full catalog.

## Common Tasks

### Add your own agent

1. **Hand it to Claude Code** — paste `Run docs/create-new-agent.md` into a Claude Code session. Claude asks what the agent should do, generates the file, registers it, smoke-tests it.

2. **Do it manually** — create `agents/my_agent.py`:

```python
from agno.agent import Agent

from app.settings import default_model
from db import get_postgres_db

my_agent = Agent(
    id="my-agent",
    name="My Agent",
    model=default_model(),
    db=get_postgres_db(),
    instructions="You are a helpful assistant.",
    enable_agentic_memory=True,
    add_datetime_to_context=True,
    add_history_to_context=True,
    num_history_runs=5,
    markdown=True,
)
```

Register in `app/main.py`. With `uvicorn --reload` running, saving the file reloads it automatically; otherwise restart the `uvicorn` process.

### Add tools to an agent

Agno includes 100+ tool integrations. See the [full list](https://docs.agno.com/tools/toolkits).

```python
from agno.tools.slack import SlackTools
from agno.tools.google_calendar import GoogleCalendarTools

my_agent = Agent(
    ...
    tools=[
        SlackTools(),
        GoogleCalendarTools(),
    ],
)
```

### Add dependencies

```sh
# 1. Edit pyproject.toml
# 2. Regenerate requirements
./scripts/generate_requirements.sh upgrade
# 3. Reinstall into the venv, then restart uvicorn
pip install -r requirements.txt
```

### Use a different model provider

1. Add your API key to `.env` (e.g., `ANTHROPIC_API_KEY`)
2. Update `app/settings.py`:

```python
from agno.models.anthropic import Claude

def default_model():
    return Claude(id="claude-sonnet-4-5")
```

3. Add dependency to `pyproject.toml`

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `OPENAI_API_KEY` | Yes | - | OpenAI API key |
| `RUNTIME_ENV` | No | `prd` | `dev` enables hot-reload and disables JWT |
| `JWT_VERIFICATION_KEY` | Prd | - | RSA **public** PEM that verifies minted access tokens (see `docs/AUTH_KEYS.md`) |
| `OPENAI_BASE_URL` | Dev/Prd | - | LiteLLM proxy URL |
| `OPENAI_EMBEDDER_ID` | No | `openai/text-embedding-3-small` | Keep the `openai/` prefix on LiteLLM |
| `AGENTOS_URL` | No | `http://127.0.0.1:8000` | Scheduler base URL |
| `PARALLEL_API_KEY` | No | - | Parallel SDK key (optional for WebSearch) |
| `SLACK_BOT_TOKEN` | No | - | Enable Slack interface |
| `SLACK_SIGNING_SECRET` | No | - | Enable Slack interface |
| `DB_HOST` | No | `localhost` | Database host |
| `DB_PORT` | No | `5432` | Database port |
| `DB_USER` | No | `ai` | Database user |
| `DB_PASS` | No | `ai` | Database password |
| `DB_DATABASE` | No | `ai` | Database name |

## Learn More

- [Agno Documentation](https://docs.agno.com)
- [AgentOS Documentation](https://docs.agno.com/agent-os/introduction)
- [Agno Discord](https://agno.com/discord)
