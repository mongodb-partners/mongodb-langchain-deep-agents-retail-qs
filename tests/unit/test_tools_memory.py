"""Unit tests for the long-term-memory tools (remember_fact / recall_memories)."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest


class _FakeStore:
    """Minimal MongoDBStore-like double."""

    def __init__(self, items: list[Any] | None = None) -> None:
        self.puts: list[tuple[tuple[str, ...], str, dict[str, Any]]] = []
        self._items = items or []

    def put(self, namespace: tuple[str, ...], key: str, value: dict[str, Any]) -> None:
        self.puts.append((namespace, key, value))

    def search(
        self, namespace: tuple[str, ...], *, query: str, limit: int = 5
    ) -> list[Any]:
        return self._items[:limit]


class _SearchItem:
    def __init__(self, text: str) -> None:
        self.value = {"text": text}


def test_TC_21_010_remember_fact_writes_to_user_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deep_agent.tools import memory

    store = _FakeStore()
    monkeypatch.setattr(
        "deep_agent.tools.memory.get_config",
        lambda: {"configurable": {"user_id": "alice"}},
    )
    monkeypatch.setattr("deep_agent.tools.memory.get_store", lambda: store)

    out = memory.remember_fact.invoke({"fact": "alice prefers markdown reports"})

    assert out.startswith("remembered")
    assert len(store.puts) == 1
    namespace, key, value = store.puts[0]
    assert namespace == ("user", "alice", "memories")
    assert value["text"] == "alice prefers markdown reports"
    assert len(key) == 32  # uuid4 hex


def test_TC_21_011_remember_refuses_empty_fact(monkeypatch: pytest.MonkeyPatch) -> None:
    from deep_agent.tools import memory

    monkeypatch.setattr(
        "deep_agent.tools.memory.get_config",
        lambda: {"configurable": {"user_id": "alice"}},
    )
    monkeypatch.setattr("deep_agent.tools.memory.get_store", lambda: _FakeStore())
    assert memory.remember_fact.invoke({"fact": "   "}).startswith("refused")


def test_TC_21_012_memory_without_user_id_returns_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing user_id in the runtime config must not raise - return a sentinel
    the LLM can read and reroute."""
    from deep_agent.tools import memory

    monkeypatch.setattr(
        "deep_agent.tools.memory.get_config", lambda: {"configurable": {}}
    )
    monkeypatch.setattr("deep_agent.tools.memory.get_store", lambda: _FakeStore())
    out = memory.remember_fact.invoke({"fact": "x"})
    assert "memory unavailable" in out


def test_TC_21_013_memory_outside_runtime_returns_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare Python (no LangGraph runtime) must surface 'memory unavailable'
    rather than crash the tool call."""
    from deep_agent.tools import memory

    def _raise() -> dict[str, Any]:
        raise RuntimeError("Called outside a LangGraph runtime")

    monkeypatch.setattr("deep_agent.tools.memory.get_config", _raise)
    monkeypatch.setattr("deep_agent.tools.memory.get_store", lambda: _FakeStore())
    out = memory.recall_memories.invoke({"query": "anything"})
    assert "memory unavailable" in out


def test_TC_21_020_recall_memories_returns_top_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deep_agent.tools import memory

    store = _FakeStore(items=[_SearchItem("alice prefers concise answers"),
                              _SearchItem("alice is a data scientist")])
    monkeypatch.setattr(
        "deep_agent.tools.memory.get_config",
        lambda: {"configurable": {"user_id": "alice"}},
    )
    monkeypatch.setattr("deep_agent.tools.memory.get_store", lambda: store)

    out = memory.recall_memories.invoke({"query": "what do we know about alice?"})
    assert "alice prefers concise answers" in out
    assert "alice is a data scientist" in out


def test_TC_21_021_recall_empty_returns_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deep_agent.tools import memory

    monkeypatch.setattr(
        "deep_agent.tools.memory.get_config",
        lambda: {"configurable": {"user_id": "alice"}},
    )
    monkeypatch.setattr("deep_agent.tools.memory.get_store", lambda: _FakeStore(items=[]))

    out = memory.recall_memories.invoke({"query": "anything"})
    assert out == "no matching memories"


def test_TC_21_030_memory_tools_in_main_agent_toolbelt() -> None:
    """build_graph must bind remember_fact + recall_memories on the main agent."""
    from unittest.mock import patch

    from deep_agent import graph as graph_mod

    with patch("deep_agent.graph.create_deep_agent") as cda, patch(
        "deep_agent.graph.get_llm", return_value=MagicMock()
    ), patch("deep_agent.graph.build_checkpointer", return_value=object()), patch(
        "deep_agent.graph.build_store", return_value=object()
    ), patch(
        "deep_agent.graph.get_data_tools", return_value=[]):
        graph_mod.build_graph()

    _, kwargs = cda.call_args
    tool_names = {getattr(t, "name", "") for t in kwargs["tools"]}
    assert {"remember_fact", "recall_memories"} <= tool_names
