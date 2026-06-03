"""Tavily web_search tool."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    from deep_agent import config

    config.get_settings.cache_clear()


def test_TC_09_010_web_search_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    from deep_agent import config as cfg

    cfg.get_settings.cache_clear()
    from deep_agent.tools import web_search as ws

    client = MagicMock()
    client.search.return_value = {
        "results": [
            {"url": "https://a.example", "title": "A", "content": "body-a", "score": 0.9},
            {"url": "https://b.example", "title": "B", "content": "body-b", "score": 0.5},
        ]
    }
    with patch("deep_agent.tools.web_search._tavily_client", return_value=client):
        out = ws.web_search.invoke({"query": "q", "max_results": 2})

    # web_search now returns a JSON string (not a list) to survive the Bedrock
    # adapter's empty-list-drop behavior.
    assert isinstance(out, str)
    parsed = json.loads(out)
    assert parsed == [
        {"url": "https://a.example", "title": "A", "content": "body-a", "score": 0.9},
        {"url": "https://b.example", "title": "B", "content": "body-b", "score": 0.5},
    ]
    client.search.assert_called_once_with("q", max_results=2)


def test_TC_09_011_web_search_disabled_without_key_returns_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When TAVILY_API_KEY is missing, web_search must NOT raise — a raise
    leaves the parent agent's ``tool_use`` block without a ``tool_result``
    and Bedrock rejects the next turn. Return a sentinel string instead."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    from deep_agent import config as cfg

    cfg.get_settings.cache_clear()
    from deep_agent.tools import web_search as ws

    out = ws.web_search.invoke({"query": "anything"})
    assert isinstance(out, str)
    assert "disabled" in out or "no web results" in out


def test_TC_09_012_web_search_sentinel_on_tavily_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any Tavily client failure (rate limit, network error, bad response)
    must also degrade gracefully — same reason as TC_09_011."""
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    from deep_agent import config as cfg

    cfg.get_settings.cache_clear()
    from deep_agent.tools import web_search as ws

    boom = MagicMock()
    boom.search.side_effect = RuntimeError("429 rate limit")
    with patch("deep_agent.tools.web_search._tavily_client", return_value=boom):
        out = ws.web_search.invoke({"query": "anything"})
    assert isinstance(out, str)
    assert "failed" in out or "rate limit" in out
