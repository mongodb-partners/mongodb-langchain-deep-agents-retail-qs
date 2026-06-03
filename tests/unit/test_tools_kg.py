"""knowledge_graph_search tool."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    from deep_agent import config

    config.get_settings.cache_clear()


def test_TC_08_060_kg_tool_delegates_to_chat_response() -> None:
    from deep_agent.tools import knowledge_graph_search as kgs

    class _Reply:
        content = "graph-answer"

    fake = MagicMock()
    fake.chat_response.return_value = _Reply()

    kgs._graph_store.cache_clear()
    with patch(
        "deep_agent.tools.knowledge_graph_search.build_graph_store", return_value=fake
    ):
        out = kgs.knowledge_graph_search.invoke({"query": "who knows whom?"})

    assert out == "graph-answer"
    fake.chat_response.assert_called_once_with("who knows whom?")
    kgs._graph_store.cache_clear()


def test_TC_08_061_kg_tool_returns_sentinel_on_operation_failure() -> None:
    from pymongo.errors import OperationFailure

    from deep_agent.tools import knowledge_graph_search as kgs

    fake = MagicMock()
    fake.chat_response.side_effect = OperationFailure("$in needs an array")

    kgs._graph_store.cache_clear()
    with patch(
        "deep_agent.tools.knowledge_graph_search.build_graph_store", return_value=fake
    ):
        out = kgs.knowledge_graph_search.invoke({"query": "anything"})

    assert "No matching entities" in out
    kgs._graph_store.cache_clear()


def test_TC_08_063_kg_tool_returns_sentinel_on_llm_error() -> None:
    """chat_response makes a live LLM call; a Bedrock
    throttle/timeout (not OperationFailure) must still degrade to the sentinel
    so the parent tool_use gets a tool_result (Bedrock pairing)."""
    from deep_agent.tools import knowledge_graph_search as kgs

    fake = MagicMock()
    fake.chat_response.side_effect = RuntimeError("ThrottlingException: Too many requests")

    kgs._graph_store.cache_clear()
    with patch(
        "deep_agent.tools.knowledge_graph_search.build_graph_store", return_value=fake
    ):
        out = kgs.knowledge_graph_search.invoke({"query": "anything"})

    assert "No matching entities" in out
    kgs._graph_store.cache_clear()


def test_TC_08_062_kg_tool_handles_string_return() -> None:
    from deep_agent.tools import knowledge_graph_search as kgs

    fake = MagicMock()
    fake.chat_response.return_value = "plain"

    kgs._graph_store.cache_clear()
    with patch(
        "deep_agent.tools.knowledge_graph_search.build_graph_store", return_value=fake
    ):
        out = kgs.knowledge_graph_search.invoke({"query": "anything"})

    assert out == "plain"
    kgs._graph_store.cache_clear()


# -------------------- domain-aware graph store --------------------


