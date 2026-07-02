"""
HELIX Figma Extractor Workflow
==============================

Single-step AgentOS Workflow wrapping the Figma Extractor agent (HELIX UC2
pipeline Step 1). The step is gated with ``requires_output_review=True`` so a
human reviews the ``FigmaExtractionResult`` before it is treated as final — the
HITL quality gate between assembly-line stations (Dark Factory pattern).

Registered in ``app/main.py`` via ``AgentOS(workflows=[...])``.
"""

from agno.workflow import Step, Workflow

from agents.figma_extractor.agent import figma_extractor_agent
from db import get_postgres_db

extract_step = Step(
    name="extract",
    agent=figma_extractor_agent,
    requires_output_review=True,  # HITL gate on the extraction result
)

helix_figma_extractor_workflow = Workflow(
    id="helix-figma-extractor",
    name="HELIX Figma Extractor",
    description="Extract design tokens, components, and variant matrices from a Figma file (HITL-reviewed).",
    db=get_postgres_db(),
    steps=[extract_step],
)
