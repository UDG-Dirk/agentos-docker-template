"""
Database Session
----------------

PostgreSQL database connection for AgentOS.
"""

from os import getenv

from agno.db.postgres import PostgresDb
from agno.knowledge import Knowledge
from agno.knowledge.embedder.openai import OpenAIEmbedder
from agno.vectordb.pgvector import PgVector, SearchType

from db.url import db_url

DB_ID = "agentos-db"

# Embedder model id. Defaults to the LiteLLM-PREFIXED id: the proxy's virtual key
# allow-list uses "openai/text-embedding-3-small"; the BARE id 401s
# (key_model_access_denied). Override via OPENAI_EMBEDDER_ID when running against
# real OpenAI (set it to "text-embedding-3-small"). See shared-results
# first-contact-level-04a-result.
EMBEDDER_ID = getenv("OPENAI_EMBEDDER_ID", "openai/text-embedding-3-small")


def get_postgres_db(contents_table: str | None = None) -> PostgresDb:
    """Create a PostgresDb instance.

    Args:
        contents_table: Optional table name for storing knowledge contents.

    Returns:
        Configured PostgresDb instance.
    """
    if contents_table is not None:
        return PostgresDb(id=DB_ID, db_url=db_url, knowledge_table=contents_table)
    return PostgresDb(id=DB_ID, db_url=db_url)


def create_knowledge(name: str, table_name: str) -> Knowledge:
    """Create a Knowledge instance with PgVector hybrid search.

    Args:
        name: Display name for the knowledge base.
        table_name: PostgreSQL table name for vector storage.

    Returns:
        Configured Knowledge instance.
    """
    return Knowledge(
        name=name,
        vector_db=PgVector(
            db_url=db_url,
            table_name=table_name,
            search_type=SearchType.hybrid,
            embedder=OpenAIEmbedder(id=EMBEDDER_ID),
        ),
        contents_db=get_postgres_db(contents_table=f"{table_name}_contents"),
    )
