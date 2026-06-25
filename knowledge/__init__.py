"""
Knowledge Package
=================

Shared knowledge bases (RAG). Each module builds a ``Knowledge`` instance via
``db.create_knowledge()`` plus an idempotent ``ingest()`` to load its documents.

Re-exports:
- ``dark_factory_knowledge`` — Dark Factory concept docs (PgVector hybrid).
- ``ingest``                 — load the docs into the vector table (idempotent).
"""

from knowledge.dark_factory_kb import dark_factory_knowledge, ingest

__all__ = [
    "dark_factory_knowledge",
    "ingest",
]
