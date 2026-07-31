"""
Dark Factory Knowledge Base
===========================

A small RAG knowledge base of Dark Factory concept documents, built on the
repo's shared ``db.create_knowledge()`` (PgVector hybrid search). Import
``dark_factory_knowledge`` to attach it to an agent (``knowledge=...``), and
call ``ingest()`` once to load the documents.

EMBEDDER CAVEAT (read before running against the LiteLLM proxy)
---------------------------------------------------------------
``db.create_knowledge()`` constructs the embedder as
``OpenAIEmbedder(id="text-embedding-3-small")`` — the BARE model id. Against
the team's LiteLLM proxy this 401s: the virtual key's allow-list only contains
the PREFIXED id ``"openai/text-embedding-3-small"`` (bare id ->
``key_model_access_denied``). So ``ingest()`` will fail to embed unless one of:

  1. ``db/session.py`` ``create_knowledge()`` is patched to use the prefixed id
     ``"openai/text-embedding-3-small"`` (preferred — makes the AgentOS path
     work too), or
  2. the embedder id is made an env var, or
  3. ``OPENAI_BASE_URL`` points at a proxy whose key allows the bare id.

We do NOT edit ``db/session.py`` from here (it is shared infrastructure). This
caveat is documented per the Level 4a investigation, which proved the fix by
constructing the embedder directly with the prefixed id.

Reference: claude-flow shared-results memory key
``first-contact-level-04a-result`` (field ``repo_issue_to_flag`` /
``open_item_for_level5``). Read it with:

    sqlite3 ~/projects/claude-flow-workspace/.swarm/memory.db \
        "SELECT value FROM memory_entries WHERE key='first-contact-level-04a-result';"

The exact fix recorded there: patch ``db/session.py create_knowledge()`` to
``OpenAIEmbedder(id="openai/text-embedding-3-small")`` (or make the id an env
var) so embeddings route through the LiteLLM virtual key without a 401.
"""

from db.session import create_knowledge

# create_knowledge() embeds with OpenAIEmbedder(id="text-embedding-3-small") —
# the BARE id. On the LiteLLM proxy this 401s; the virtual key requires the
# PREFIXED id "openai/text-embedding-3-small". Fix lives in db/session.py (do
# not patch from here). See module docstring + shared-results memory key
# first-contact-level-04a-result.
dark_factory_knowledge = create_knowledge("dark-factory", "dark_factory_kb")


# ---------------------------------------------------------------------------
# Documents (inline text — no URLs, no network fetch). Originally lifted from a
# local hackathon-scratch agent (not in this repo — gitignored; see CUSTOMIZATIONS.md).
# ---------------------------------------------------------------------------
DOCUMENTS = [
    {
        "id": "vsm-overview",
        "content": "The Viable System Model (VSM) is the governance framework for Dark Factory. System 5 (S5) stays explicitly human — strategic direction and identity. Systems 1-4 are progressively automated: S1 is operations (agent execution), S2 is coordination (scheduling, conflict resolution), S3 is control (monitoring, resource allocation), S4 is intelligence (environmental scanning, adaptation). Each capability in the factory is assigned a traffic-light governance tier: Grün (green) for autonomous execution, Gelb (yellow) for human-flagged review, Rot (red) for human-required approval.",
    },
    {
        "id": "finops-principle",
        "content": "Dark Factory operates under a FinOps-honest principle: every AI capability must justify its cost versus the human-direct alternative. The positioning is 'we use AI when AI is the right answer, not when AI is the new shiny.' Cost tracking is centralized through LiteLLM virtual keys, with one key per client project for billing separation. The token budget model (TBM) provides the cost-estimation framework.",
    },
    {
        "id": "assembly-line-pattern",
        "content": "Dark Factory uses an L-shaped architecture: vertical assembly lines crossed by horizontal harnesses. Each assembly line is a domain-specific production pipeline (e.g., HELIX for design-to-code). Horizontal harnesses are cross-cutting concerns: governance, FinOps, CI/CD policy, observability. Lines declare which harnesses they use. The first assembly line is HELIX, which takes design tokens and produces headless CMS frontend components.",
    },
    {
        "id": "agno-runtime",
        "content": "The production runtime for Dark Factory is Agno AgentOS, deployed on Coolify at poc-agno-api.services.plygrnd.tech. AgentOS provides the API server, authentication (JWT with BYO RSA keypair), session management, and agent lifecycle. The runtime layer is treated as boring/stable infrastructure, separated from the orchestration layer which is competitive/swappable. All model calls route through LiteLLM at litellm.services.plygrnd.tech for FinOps tracking.",
    },
]


def ingest() -> None:
    """Load the Dark Factory documents into the PgVector table.

    Idempotent: ``skip_if_exists=True`` means re-runs don't re-embed documents
    already present, so this is safe to call on every startup.

    NOTE: embedding goes through whatever embedder ``db.create_knowledge()``
    configured. See the module docstring's EMBEDDER CAVEAT — against the LiteLLM
    proxy the bare model id 401s until ``db/session.py`` is patched to the
    prefixed id.
    """
    for doc in DOCUMENTS:
        dark_factory_knowledge.insert(
            name=doc["id"],
            text_content=doc["content"],
            metadata={"id": doc["id"]},
            skip_if_exists=True,
        )
