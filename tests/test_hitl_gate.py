"""HITL output-review gate reachability (Finding 51 remediation).

Finding 51 (shared-results:hitl-pause-behavior-verification-result-2026-07-27): Agno only
evaluates `requires_output_review` on TOP-LEVEL Step/Router — a review step nested inside
Steps(...) inside Parallel(...) is a silent no-op. This MR moves normalize_step top-level.

These tests prove the fix deterministically with tiny stand-in workflows (no PAT/LLM/git/
Postgres — db omitted; the pause is computed in the step loop), plus a structural assertion
on the real workflow.
"""
from __future__ import annotations

import asyncio

from agno.workflow import Parallel, Step, Steps, Workflow
from agno.workflow.types import StepInput, StepOutput

import app.workflows.helix_figma_extractor as w


def _ex(si: StepInput, **k):
    return StepOutput(content={"tokens": [1]})


def _base(si: StepInput, **k):
    return StepOutput(content={"meta": {}})


def _review(si: StepInput, **k):
    return StepOutput(content={"report": "non-empty"})


def _done(si: StepInput, **k):
    return StepOutput(content="ok")


def _review_step():
    return Step(name="review", executor=_review, requires_output_review=True,
                output_review_message="review me")


def test_top_level_review_gate_pauses():
    """FIXED shape: Parallel(extract, baseline) → review(top-level) → done ⇒ PAUSES."""
    wf = Workflow(id="hitl-top", name="t", steps=[
        Parallel(Step(name="extract", executor=_ex), Step(name="baseline-read", executor=_base),
                 name="par"),
        _review_step(),
        Step(name="done", executor=_done),
    ])
    out = asyncio.run(wf.arun(input="go"))
    assert out.is_paused is True
    assert out.paused_step_name == "review"


def test_nested_review_gate_does_not_pause():
    """BROKEN shape (Finding 51): review nested in Steps in Parallel ⇒ NO pause (no-op gate).
    Negative control — reproduces the original bug this MR fixes."""
    wf = Workflow(id="hitl-nested", name="t", steps=[
        Parallel(Steps(name="cb", steps=[Step(name="extract", executor=_ex), _review_step()]),
                 Step(name="baseline-read", executor=_base), name="par"),
        Step(name="done", executor=_done),
    ])
    out = asyncio.run(wf.arun(input="go"))
    assert out.is_paused is False


def test_real_workflow_normalize_is_top_level():
    """Structural: normalize_step sits at the TOP LEVEL of the real workflow (not nested in
    the Parallel), and carries requires_output_review — so its HITL gate is reachable."""
    steps = w.helix_figma_extractor_workflow.steps
    # normalize_step is a direct top-level member
    assert w.normalize_step in steps, "normalize_step must be a top-level workflow step"
    # ...and it is NOT a member of the Parallel (which now holds only extract + baseline)
    parallel = steps[0]
    parallel_members = getattr(parallel, "steps", None) or getattr(parallel, "components", None) or []
    assert w.normalize_step not in parallel_members, "normalize_step must not be nested in the Parallel"
    assert w.normalize_step.requires_output_review is True
    # order: Parallel first, normalize before smoke
    assert steps.index(w.normalize_step) < steps.index(w.smoke_test_step)
