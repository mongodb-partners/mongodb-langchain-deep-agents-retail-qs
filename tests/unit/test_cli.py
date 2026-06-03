"""Sub-phase 13: deep-agent CLI."""
from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    from deep_agent import config

    config.get_settings.cache_clear()


def test_TC_13_011_cli_seed(capsys: pytest.CaptureFixture[str]) -> None:
    # `seed` is imported lazily inside the CLI handler. The test patches the
    # import path that the handler uses.
    with patch("deep_agent.ingestion.seed.seed_all", return_value={"knowledge_base": 3}):
        from deep_agent.cli import main

        rc = main(["seed"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "knowledge_base" in out


