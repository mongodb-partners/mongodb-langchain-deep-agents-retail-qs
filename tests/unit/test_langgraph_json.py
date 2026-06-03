"""Sub-phase 13: langgraph.json manifest."""
from __future__ import annotations

import json
from pathlib import Path


def test_TC_13_050_langgraph_json_points_at_build_graph() -> None:
    path = Path(__file__).resolve().parents[2] / "langgraph.json"
    assert path.exists(), f"missing {path}"
    cfg = json.loads(path.read_text())
    assert cfg["dependencies"] == ["."]
    assert "graphs" in cfg
    assert any(
        ("deep_agent/graph" in v) or ("deep_agent.graph" in v)
        for v in cfg["graphs"].values()
    )
    assert cfg["env"] == ".env"
