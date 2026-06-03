"""Sub-phase 01: Bootstrap sanity checks.

These tests describe the shape of a minimally-correct project skeleton:
- pyproject declares the coordinates and pinned deps the rest of the suite assumes
- pytest markers are configured so `pytest -m 'not integration'` works by default
- the top-level package imports cleanly
"""
from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = PROJECT_ROOT / "pyproject.toml"


def _load_pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text())


def test_TC_01_010_pyproject_has_required_coordinates() -> None:
    data = _load_pyproject()
    assert data["project"]["name"] == "mongodb-langchain-deep-agents-retail-qs"
    assert data["project"]["requires-python"].startswith(">=3.11")
    # Console script is named deep-agent (dashes, per PEP 621 convention)
    assert "deep-agent" in data["project"]["scripts"]


@pytest.mark.parametrize(
    "dep_prefix",
    [
        "langchain-core",
        "langchain",
        "langgraph",
        "langchain-mongodb",
        "langgraph-checkpoint-mongodb",
        "langgraph-store-mongodb",
        "langchain-voyageai",
        "langchain-aws",
        "deepagents",
        "langsmith",
        "pymongo",
        "fastapi",
        "sse-starlette",
        "kafka-python",
        "boto3",
        "tavily-python",
    ],
)
def test_TC_01_020_pinned_deps_present(dep_prefix: str) -> None:
    deps = _load_pyproject()["project"]["dependencies"]
    assert any(d.startswith(dep_prefix) for d in deps), f"{dep_prefix} missing from dependencies"


def test_TC_01_030_pytest_markers_and_defaults_configured() -> None:
    data = _load_pyproject()
    opts = data["tool"]["pytest"]["ini_options"]
    markers = opts["markers"]
    assert any(m.startswith("integration") for m in markers)
    assert "not integration" in opts["addopts"]
    assert "--strict-markers" in opts["addopts"]


def test_TC_01_040_package_imports() -> None:
    module = importlib.import_module("deep_agent")
    assert hasattr(module, "__version__")
