"""Shared pytest fixtures for the Figma Extractor harness.

Tests run against REAL run output (no MCP mocking — per step3 task). Point at a
run with --output; defaults to the sequential run.
"""
import json
import sys
from pathlib import Path

import pytest
from dotenv import load_dotenv

HELIX_ROOT = Path(__file__).resolve().parents[1]            # helix-poc-agno/
AGENT_DIR = HELIX_ROOT / "agents" / "figma_extractor"
sys.path.insert(0, str(AGENT_DIR))

# load FIGMA_PAT (helix .env) for the PAT-leak behavioral test
load_dotenv(HELIX_ROOT / ".env", override=False)

DEFAULT_RUN = AGENT_DIR / "runs" / "run_sequential_001.json"


def pytest_addoption(parser):
    parser.addoption(
        "--output",
        action="store",
        default=str(DEFAULT_RUN),
        help="path to a Figma Extractor run JSON ({result, _meta} wrapper)",
    )


@pytest.fixture(scope="session")
def run_path(pytestconfig) -> Path:
    return Path(pytestconfig.getoption("--output"))


@pytest.fixture(scope="session")
def run_payload(run_path) -> dict:
    if not run_path.exists():
        pytest.skip(f"run output not found: {run_path} (run agent.py first)")
    return json.loads(run_path.read_text())


@pytest.fixture(scope="session")
def result_dict(run_payload) -> dict:
    return run_payload["result"]


@pytest.fixture(scope="session")
def meta(run_payload) -> dict:
    return run_payload.get("_meta", {})


@pytest.fixture(scope="session")
def extraction_result(result_dict):
    from models import FigmaExtractionResult  # type: ignore
    return FigmaExtractionResult(**result_dict)


@pytest.fixture(scope="session")
def asset_base() -> Path:
    return AGENT_DIR
