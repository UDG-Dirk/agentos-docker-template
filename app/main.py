"""
AgentOS Entrypoint
==================
"""

from contextlib import asynccontextmanager
from os import getenv
from pathlib import Path

from agno.os import AgentOS
from agno.utils.log import log_info

from agents.code_search import code_search
from agents.knowledge_agent import knowledge_agent
from agents.reasoning_agent import reasoning_agent
from agents.web_search import web_search
from app.workflows.helix_baseline_reader import helix_baseline_reader_workflow
from app.workflows.helix_client_extractor import helix_client_extractor_workflow
from app.workflows.helix_composition_only_extractor import helix_composition_only_extractor_workflow
from app.workflows.helix_figma_extractor import helix_figma_extractor_workflow
from db import get_postgres_db
from knowledge.dark_factory_kb import ingest as ingest_dark_factory_kb

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
runtime_env = getenv("RUNTIME_ENV", "prd")
scheduler_base_url = getenv("AGENTOS_URL", "http://127.0.0.1:8000")

# ---------------------------------------------------------------------------
# Interfaces
# - The CodeSearch agent becomes available on Slack when both env vars are set
# ---------------------------------------------------------------------------
SLACK_BOT_TOKEN = getenv("SLACK_BOT_TOKEN", "")
SLACK_SIGNING_SECRET = getenv("SLACK_SIGNING_SECRET", "")

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


# ---------------------------------------------------------------------------
# Lifespan — extension hook for app-level startup / teardown.
#
# AgentOS handles the MCP lifecycle (connect on startup, close on shutdown).
# Keep this hook in place so you can plug in your own setup as needed.
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app):  # type: ignore[no-untyped-def]
    log_info("AgentOS lifespan: startup")
    try:
        ingest_dark_factory_kb()  # idempotent (skip_if_exists)
        log_info("Dark Factory knowledge base ingested.")
    except Exception as e:  # never let KB ingest block startup
        log_info(f"Dark Factory KB ingest skipped/failed: {e}")
    try:
        yield
    finally:
        log_info("AgentOS lifespan: shutdown")


# ---------------------------------------------------------------------------
# Create AgentOS
# ---------------------------------------------------------------------------
agent_os = AgentOS(
    name="AgentOS",
    tracing=True,
    scheduler=True,
    scheduler_base_url=scheduler_base_url,
    authorization=runtime_env == "prd",
    lifespan=lifespan,
    db=get_postgres_db(),
    agents=[web_search, code_search, reasoning_agent, knowledge_agent],
    workflows=[helix_figma_extractor_workflow, helix_client_extractor_workflow,
               helix_composition_only_extractor_workflow, helix_baseline_reader_workflow],
    interfaces=interfaces,
    config=str(Path(__file__).parent / "config.yaml"),
    enable_mcp_server=True,
)
app = agent_os.get_app()


if __name__ == "__main__":
    agent_os.serve(app="app.main:app", reload=runtime_env == "dev")
