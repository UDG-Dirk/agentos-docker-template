"""HELIX 3d Component Code Generator — generates a customer Lit component library from 3c output.

Public surface (Phase 1, deterministic):
  from agents.component_code_generator import generate_component_code, ComponentCodeGeneratorOutput
"""
from agents.component_code_generator.models import (
    BlockingWarning,
    ComponentCodeGeneratorOutput,
    ComponentGenerationSummary,
    CostSummary,
    ElementGenerationResult,
    ProvenanceExtension,
    StructuralGateResult,
)
from agents.component_code_generator.scaffolding import (
    find_baseline_source,
    fork_component,
    get_configured_fork_branches,
    is_valid_lit_source,
    render_cem,
    route_element,
    slugify,
)
from agents.component_code_generator.step import (
    STEP_NAME_GENERATE,
    STEP_NAME_INPUT,
    generate_component_code,
)

__all__ = [
    "ComponentCodeGeneratorOutput",
    "ComponentGenerationSummary",
    "ElementGenerationResult",
    "ProvenanceExtension",
    "StructuralGateResult",
    "CostSummary",
    "BlockingWarning",
    "fork_component",
    "route_element",
    "render_cem",
    "slugify",
    "generate_component_code",
    "STEP_NAME_INPUT",
    "STEP_NAME_GENERATE",
]
