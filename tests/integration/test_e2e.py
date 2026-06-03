"""End-to-end integration tests gated on ``ATLAS_URI``.

- ``TC-INT-010``: checkpoint resume — two invocations on the same
  ``thread_id`` share state.
- ``TC-INT-020``: plan round-trip — a turn that writes a todo list causes
  a document to appear in the ``plans`` collection.
- ``TC-INT-030``: LangSmith trace is emitted when ``LANGSMITH_TRACING=true``.
"""
from __future__ import annotations

import os
import time
import uuid
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

pytestmark = pytest.mark.integration


@pytest.fixture
def thread_id() -> str:
    return f"e2e-{uuid.uuid4()}"


def _reset_settings_for_atlas(uri: str) -> None:
    os.environ["MONGODB_URI"] = uri
    from deep_agent import config

    config.get_settings.cache_clear()


def test_TC_INT_010_checkpoint_resume(atlas_uri: str, thread_id: str) -> None:
    """Two invocations on the same thread_id must share state."""
    _reset_settings_for_atlas(atlas_uri)
    from deep_agent.graph import build_graph
    from deep_agent.persistence.indexes import ensure_indexes
    from deep_agent.persistence.mongo import reset_for_tests

    reset_for_tests()
    ensure_indexes()

    graph = build_graph()
    cfg = {"configurable": {"thread_id": thread_id, "user_id": "e2e-user"}}

    graph.invoke(
        {
            "messages": [HumanMessage(content="Remember: my favorite color is teal.")],
            "user_id": "e2e-user",
        },
        config=cfg,
    )

    state = graph.get_state(cfg).values
    assert any(
        isinstance(m, HumanMessage) and "teal" in m.content
        for m in state.get("messages", [])
    )


def test_TC_INT_020_plan_round_trip(
    atlas_uri: str, atlas_client: Any, thread_id: str
) -> None:
    """A turn that writes a todo list must leave a document in `plans`."""
    _reset_settings_for_atlas(atlas_uri)
    from deep_agent import config
    from deep_agent.graph import build_graph
    from deep_agent.persistence.indexes import ensure_indexes
    from deep_agent.persistence.mongo import reset_for_tests

    reset_for_tests()
    ensure_indexes()

    s = config.get_settings()
    # Clear stale plans for this thread so we're asserting on what THIS turn
    # produced, not a previous run.
    db = atlas_client[s.mongodb_db]
    db["plans"].delete_many({"user_id": "e2e-plan", "thread_id": thread_id})
    # The response cache can short-circuit the LLM; clear for a clean signal.
    db[s.response_cache_collection].delete_many({})

    graph = build_graph()
    cfg = {"configurable": {"thread_id": thread_id, "user_id": "e2e-plan"}}
    graph.invoke(
        {
            "messages": [
                HumanMessage(
                    content=(
                        "Plan and then summarize a short comparison of GridFS "
                        "versus S3 for agent artifacts. Use write_todos first."
                    )
                )
            ],
            "user_id": "e2e-plan",
        },
        config=cfg,
    )

    # Poll up to ~20s — the middleware upserts mid-turn, but Atlas acks may lag.
    deadline = time.time() + 20.0
    doc = None
    while time.time() < deadline:
        doc = db["plans"].find_one(
            {"user_id": "e2e-plan", "thread_id": thread_id}
        )
        if doc is not None:
            break
        time.sleep(1)
    assert doc is not None, "plans document never appeared"
    assert isinstance(doc.get("todos"), list)
    assert len(doc["todos"]) >= 1
    assert "updated_at" in doc


def test_TC_INT_030_langsmith_trace_emitted(
    atlas_uri: str, atlas_client: Any, thread_id: str
) -> None:
    """When LANGSMITH_TRACING=true, graph.invoke produces at least one run."""
    if not os.environ.get("LANGSMITH_API_KEY"):
        pytest.skip("LANGSMITH_API_KEY not set")
    os.environ["LANGSMITH_TRACING"] = "true"
    _reset_settings_for_atlas(atlas_uri)

    from langsmith import Client

    from deep_agent import config
    from deep_agent.graph import build_graph
    from deep_agent.persistence.mongo import reset_for_tests

    reset_for_tests()

    # Clear the response cache so prior runs don't short-circuit the LLM.
    s = config.get_settings()
    db = atlas_client[s.mongodb_db]
    db[s.response_cache_collection].delete_many({})

    graph = build_graph()
    cfg = {"configurable": {"thread_id": thread_id, "user_id": "e2e-trace"}}
    result = graph.invoke(
        {"messages": [HumanMessage(content="What is 2+2?")], "user_id": "e2e-trace"},
        config=cfg,
    )
    assert any(isinstance(m, AIMessage) for m in result.get("messages", []))

    time.sleep(3)
    client = Client()
    runs = list(
        client.list_runs(
            project_name=os.environ.get("LANGSMITH_PROJECT", "agent-cartsmith-retail-demo"),
            limit=5,
        )
    )
    assert len(runs) >= 1
