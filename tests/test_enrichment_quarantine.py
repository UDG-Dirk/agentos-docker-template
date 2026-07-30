"""Enrichment-quarantine lock-in tests.

Enrichment (`enrichment_match` / `enrichment_type` on tokens, `enrichment_coverage`) is INACTIVE
in the live deterministic pipeline — see the QUARANTINE note in
`agents/token_normalizer/normalizer.py` and `agents/figma_extractor/README.md`. These tests lock
that contract in place so a future reader (or a silent regression) can't quietly re-activate it:

* the model fields default to their inactive values,
* a normal (no-enrichment) run reports zero enriched tokens and logs NO tripwire warning,
* the enrichment READ-paths are preserved (a token that DOES carry enrichment is still honoured),
* and when enrichment unexpectedly arrives, the quarantine tripwire WARNING fires.

Task: code-tasks:quarantine-vestigial-enrichment-deterministic-path-2026-07-30 (Option A).
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

# --- paths (mirror tests/test_token_normalizer.py) ---------------------------
HELIX_ROOT = Path(__file__).resolve().parents[1]
NORMALIZER_DIR = HELIX_ROOT / "agents" / "token_normalizer"
EXTRACTOR_DIR = HELIX_ROOT / "agents" / "figma_extractor"
for _p in (str(NORMALIZER_DIR), str(EXTRACTOR_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from _normalizer_helpers import build_extraction, make_token  # noqa: E402


@pytest.fixture(scope="module")
def normalize():
    from normalizer import normalize_tokens  # type: ignore

    return normalize_tokens


# --- model defaults are inactive ---------------------------------------------

def test_model_enrichment_defaults_inactive():
    """TokenEntry / FigmaExtractionResult default enrichment fields to their inactive values."""
    from normalizer import FigmaExtractionResult, TokenEntry  # type: ignore  # re-exported from figma_extractor/models

    tok = TokenEntry(name="--color-primary-500", value="#3388F0")
    assert tok.enrichment_match is None
    assert tok.enrichment_type is None

    res = FigmaExtractionResult(file_key="X")
    assert res.enrichment_coverage == 0.0


# --- the normal (no-enrichment) path: zero enriched, no tripwire -------------

def test_no_enrichment_run_reports_zero_and_is_silent(normalize, caplog):
    """A normal run (the only shape the live pipeline produces) reports zero enriched tokens
    and does NOT fire the quarantine tripwire warning."""
    extraction = build_extraction([
        make_token("--color-primary-500", "#3388F0", category="color"),
        make_token("--spacing-md", "16px", category="spacing"),
    ])
    with caplog.at_level(logging.WARNING, logger="token_normalizer"):
        result = normalize(extraction)

    assert result.normalization_report.tokens_with_enrichment == 0
    assert result.normalization_report.tokens_without_enrichment == 2
    assert not any(
        "enrichment is unexpectedly ACTIVE" in r.getMessage() for r in caplog.records
    ), "tripwire must stay silent when no token carries enrichment"


# --- read-paths preserved + tripwire fires when enrichment DOES arrive -------

def test_enrichment_readpaths_preserved_and_tripwire_fires(normalize, caplog):
    """If a token DOES carry enrichment (not expected in today's pipeline), the read-paths still
    honour it as authoritative AND the quarantine tripwire warns. This proves we preserved the
    logic (per task constraint) while flagging the unexpected state."""
    extraction = build_extraction([
        make_token(
            "--color-brand",
            "#FF0000",
            category="color",
            enrichment_match="colors/brand/primary",
            enrichment_type="color",
        ),
    ])
    with caplog.at_level(logging.WARNING, logger="token_normalizer"):
        result = normalize(extraction)

    rep = result.normalization_report
    # read-path preserved: enrichment counted + used as the authoritative type/path signal
    assert rep.tokens_with_enrichment == 1
    entry = rep.all_entries[0]
    assert entry.type_source == "enrichment"
    assert entry.confidence == "authoritative"
    assert entry.dtcg_type == "color"
    # tripwire fired
    assert any(
        "enrichment is unexpectedly ACTIVE" in r.getMessage() for r in caplog.records
    ), "tripwire must warn when enrichment unexpectedly arrives"


# --- behavior preservation: no-enrichment output unchanged by the quarantine -

def test_no_enrichment_output_still_infers_type_and_path(normalize):
    """Behaviour for the live (no-enrichment) shape is unchanged: type/path come from
    value/name inference, exactly as before the quarantine comments were added."""
    extraction = build_extraction([make_token("--color-primary-500", "#3388F0", category="color")])
    result = normalize(extraction)
    entry = result.normalization_report.all_entries[0]
    assert entry.dtcg_type == "color"
    assert entry.type_source in {"value-pattern", "name-convention"}  # inference, not "enrichment"
    assert entry.confidence in {"high", "medium"}
