"""HELIX 3c Theme Generator — customer-specific fork of the baseline library.

Public surface (Phase 1, deterministic):
  from agents.theme_generator import generate_theme_phase1, ThemeGeneratorOutput
"""
from agents.theme_generator.models import (
    BlockingWarning,
    ComponentDerivation,
    ConfidenceSummary,
    CostSummary,
    ProvenanceSummary,
    ThemeGeneratorOutput,
    UnmappedComponent,
)
from agents.theme_generator.scaffolding import (
    build_deterministic_package,
    classify_component,
    substitute_tokens,
    write_package,
)
from agents.theme_generator.reconciliation import (
    CircuitBreaker,
    CohesionVerdict,
    MockCohesionReviewer,
    MockReconciler,
    ReconciliationResult,
)
from agents.theme_generator.step import (
    STEP_NAME_GENERATE,
    STEP_NAME_INPUT,
    STEP_NAME_TRANSFORM,
    generate_theme,
    generate_theme_phase1,
)

__all__ = [
    "ThemeGeneratorOutput",
    "ProvenanceSummary",
    "ConfidenceSummary",
    "ComponentDerivation",
    "UnmappedComponent",
    "BlockingWarning",
    "CostSummary",
    "build_deterministic_package",
    "classify_component",
    "substitute_tokens",
    "write_package",
    "generate_theme_phase1",
    "generate_theme",
    "ReconciliationResult",
    "CohesionVerdict",
    "MockReconciler",
    "MockCohesionReviewer",
    "CircuitBreaker",
    "STEP_NAME_INPUT",
    "STEP_NAME_TRANSFORM",
    "STEP_NAME_GENERATE",
]
