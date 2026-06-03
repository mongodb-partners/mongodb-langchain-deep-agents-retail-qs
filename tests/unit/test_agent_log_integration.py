"""Integration coverage for the
``langchain-mongodb-agent-log`` adoption.

These tests verify that the deep-agent graph wires the package's
``AgentLog`` engine, ``AgentLogMiddleware`` adapter, and
``search_past_conversations`` tool correctly. They do NOT re-test the
package's internals (worker thread, projection, embedding gate,
PyMongoError handling) — the package owns its own 67-test suite for
that. Here we only assert the wiring is right.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import mongomock
import pytest


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    from deep_agent import config, graph

    config.get_settings.cache_clear()
    graph._agent_log.cache_clear()


def test_TC_510_002_single_agent_log_instance() -> None:
    """``_agent_log`` is lru-cached so a single engine
    is reused across all callers in the same process. The package's
    daemon worker is process-lifetime; multiple engines would compete
    for ordering."""
    fake_db = mongomock.MongoClient()["deep_agent_test"]
    with patch("deep_agent.graph.get_db", return_value=fake_db):
        from deep_agent.graph import _agent_log

        a = _agent_log()
        b = _agent_log()
        assert a is b


def test_TC_510_003_AgentLogMiddleware_in_chain() -> None:
    """The middleware chain registers
    ``AgentLogMiddleware`` from the package, not the deleted
    ``CheckpointMirrorMiddleware``."""
    from langchain_mongodb_agent_log import AgentLogMiddleware

    fake_db = mongomock.MongoClient()["deep_agent_test"]
    with patch("deep_agent.graph.get_db", return_value=fake_db):
        from deep_agent.graph import _middleware_chain

        chain = _middleware_chain()
    assert any(
        isinstance(m, AgentLogMiddleware) for m in chain
    ), f"AgentLogMiddleware missing from chain: {chain}"


def test_TC_510_004a_search_tool_present_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``ENABLE_AGENT_LOG_SEARCH`` is true, the
    package's prebuilt ``search_past_conversations`` tool is added to
    the agent's tool list."""
    monkeypatch.setenv("ENABLE_AGENT_LOG_SEARCH", "true")
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-test")
    from deep_agent import config

    config.get_settings.cache_clear()

    fake_db = mongomock.MongoClient()["deep_agent_test"]
    fake_embeddings = MagicMock()
    fake_embeddings.embed_query = MagicMock(return_value=[0.0] * 8)

    # ``get_data_tools`` opens a real MongoDB connection on first call;
    # stub it out — we're testing search-tool wiring, not the data
    # toolkit.
    with (
        patch("deep_agent.graph.get_db", return_value=fake_db),
        patch("deep_agent.graph.get_data_tools", return_value=[]),
        patch("deep_agent.models.get_embeddings", return_value=fake_embeddings),
    ):
        from deep_agent.graph import _main_agent_tools

        tools = _main_agent_tools()

    names = [getattr(t, "name", None) for t in tools]
    assert "search_past_conversations" in names


def test_TC_510_004b_search_tool_absent_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``ENABLE_AGENT_LOG_SEARCH`` is false, the
    tool is NOT registered (avoids users seeing a tool that would
    always return empty against a missing index)."""
    monkeypatch.setenv("ENABLE_AGENT_LOG_SEARCH", "false")
    from deep_agent import config

    config.get_settings.cache_clear()

    fake_db = mongomock.MongoClient()["deep_agent_test"]
    with (
        patch("deep_agent.graph.get_db", return_value=fake_db),
        patch("deep_agent.graph.get_data_tools", return_value=[]),
    ):
        from deep_agent.graph import _main_agent_tools

        tools = _main_agent_tools()

    names = [getattr(t, "name", None) for t in tools]
    assert "search_past_conversations" not in names


def test_TC_510_009a_record_lands_doc_in_agent_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An end-to-end ``record(...)`` call
    via the engine lands one doc in ``db[agent_log_collection]`` with
    the right thread/user/agent_name."""
    monkeypatch.setenv("ENABLE_AGENT_LOG_SEARCH", "false")
    from deep_agent import config

    config.get_settings.cache_clear()

    fake_db = mongomock.MongoClient()["deep_agent_test"]
    with patch("deep_agent.graph.get_db", return_value=fake_db):
        from deep_agent.graph import _agent_log

        log = _agent_log()
        msg = MagicMock()
        msg.type = "human"
        msg.content = "hi"
        msg.tool_calls = []
        msg.tool_call_id = None
        msg.usage_metadata = None
        msg.additional_kwargs = {}
        log.record(thread_id="t1", user_id="u1", messages=[msg])
        log.flush_for_tests()

    coll = fake_db["agent_log"]
    doc = coll.find_one({})
    assert doc is not None
    assert doc["thread_id"] == "t1"
    assert doc["user_id"] == "u1"
    assert doc["agent_name"] == "main"


def test_TC_510_009c_pymongo_error_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PyMongoError on insert does not
    propagate. The package owns the swallow logic; this just verifies
    the wiring doesn't accidentally re-raise."""
    from pymongo.errors import PyMongoError

    monkeypatch.setenv("ENABLE_AGENT_LOG_SEARCH", "false")
    from deep_agent import config

    config.get_settings.cache_clear()

    fake_db = mongomock.MongoClient()["deep_agent_test"]
    coll = fake_db["agent_log"]
    with patch.object(coll, "insert_one", side_effect=PyMongoError("down")), patch(
        "deep_agent.graph.get_db", return_value=fake_db
    ):
        from deep_agent.graph import _agent_log

        log = _agent_log()
        msg = MagicMock()
        msg.type = "human"
        msg.content = "x"
        msg.tool_calls = []
        msg.tool_call_id = None
        msg.usage_metadata = None
        msg.additional_kwargs = {}
        # Must not raise — package logs and continues
        log.record(thread_id="t1", user_id="u1", messages=[msg])
        log.flush_for_tests()


def _make_runtime(thread_id: str, user_id: str, **extra: Any) -> Any:
    rt = MagicMock()
    rt.config = {
        "configurable": {"thread_id": thread_id, "user_id": user_id, **extra}
    }
    return rt


def test_TC_510_INV_001_one_doc_per_after_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every ``after_model`` super-step appends exactly
    one doc. Verified by invoking the middleware twice and counting."""
    monkeypatch.setenv("ENABLE_AGENT_LOG_SEARCH", "false")
    from deep_agent import config

    config.get_settings.cache_clear()

    fake_db = mongomock.MongoClient()["deep_agent_test"]
    with patch("deep_agent.graph.get_db", return_value=fake_db):
        from deep_agent.graph import _agent_log

        log = _agent_log()
        from langchain_mongodb_agent_log import AgentLogMiddleware

        mw = AgentLogMiddleware(log)
        msg = MagicMock()
        msg.type = "human"
        msg.content = "hi"
        msg.tool_calls = []
        msg.tool_call_id = None
        msg.usage_metadata = None
        msg.additional_kwargs = {}
        rt = _make_runtime("t1", "u1")
        mw.after_model({"messages": [msg]}, rt)
        mw.after_model({"messages": [msg]}, rt)
        log.flush_for_tests()

    assert fake_db["agent_log"].count_documents({}) == 2


def test_TC_510_subagent_attribution() -> None:
    """The researcher & writer
    subagents attach an AgentLogMiddleware with their own agent_name so their
    activity is attributed in agent_log instead of all landing as 'main'."""
    from langchain_mongodb_agent_log import AgentLogMiddleware

    fake_db = mongomock.MongoClient()["deep_agent_test"]
    with patch("deep_agent.graph.get_db", return_value=fake_db):
        from deep_agent.agents.subagents import researcher_subagent, writer_subagent

        researcher = researcher_subagent()
        writer = writer_subagent()

    def _agent_log_name(spec: Any) -> Any:
        for m in spec["middleware"]:
            if isinstance(m, AgentLogMiddleware):
                return getattr(m, "_agent_name", None)
        return None

    assert _agent_log_name(researcher) == "researcher"
    assert _agent_log_name(writer) == "writer"


def test_TC_510_index_names_threaded(monkeypatch: pytest.MonkeyPatch) -> None:
    """AGENT_LOG_VECTOR_INDEX / AGENT_LOG_SEARCH_INDEX
    / AGENT_LOG_SEARCH_TOP_K reach build_tool — they are no longer dead config."""
    monkeypatch.setenv("ENABLE_AGENT_LOG_SEARCH", "true")
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-test")
    monkeypatch.setenv("AGENT_LOG_VECTOR_INDEX", "custom_vec")
    monkeypatch.setenv("AGENT_LOG_SEARCH_INDEX", "custom_srch")
    monkeypatch.setenv("AGENT_LOG_SEARCH_TOP_K", "7")
    from deep_agent import config

    config.get_settings.cache_clear()

    fake_db = mongomock.MongoClient()["deep_agent_test"]
    fake_emb = MagicMock()
    fake_emb.embed_query = MagicMock(return_value=[0.0] * 8)
    captured: dict[str, Any] = {}

    def fake_build_tool(collection: Any, embeddings: Any, **kw: Any) -> Any:
        captured.update(kw)
        t = MagicMock()
        t.name = "search_past_conversations"
        return t

    with (
        patch("deep_agent.graph.get_db", return_value=fake_db),
        patch("deep_agent.graph.get_data_tools", return_value=[]),
        patch("deep_agent.models.get_embeddings", return_value=fake_emb),
        patch(
            "langchain_mongodb_agent_log.retrieval.tool.build_tool",
            side_effect=fake_build_tool,
        ),
    ):
        from deep_agent.graph import _main_agent_tools

        _main_agent_tools()

    assert captured.get("search_index") == "custom_srch"
    assert captured.get("vector_index") == "custom_vec"
    assert captured.get("top_k") == 7
