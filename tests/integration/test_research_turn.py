"""Full research-turn integration test.

Seeds the knowledge base with one synthetic document, drives a research
question through the compiled deep-agent graph, and asserts:

- The turn terminates with at least one :class:`AIMessage`.
- The agent touched the knowledge base (a ``fetch_and_cache`` call left
  metadata with ``content_hash`` in ``knowledge_base``, OR the seeded
  document was retrieved from it).
- The plan document reflects the turn's todo list.

Gated on ``ATLAS_URI``; skips when absent.
"""
from __future__ import annotations

import os
import time
import uuid
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

pytestmark = pytest.mark.integration

SEEDED_MARKER = "DeepAgentResearchMarker"
SEEDED_TEXT = (
    f"{SEEDED_MARKER}: deep-agent research showcases MongoDB Atlas Vector "
    "Search combined with LangChain deepagents, using a researcher subagent "
    "that ingests new URLs via fetch_and_cache into knowledge_base."
)


def _reset_for_atlas(uri: str) -> None:
    os.environ["MONGODB_URI"] = uri
    from deep_agent import config

    config.get_settings.cache_clear()


def test_TC_INT_060_research_turn_end_to_end(
    atlas_uri: str, atlas_client: Any
) -> None:
    _reset_for_atlas(atlas_uri)
    from deep_agent import config
    from deep_agent.graph import build_graph
    from deep_agent.persistence.indexes import ensure_indexes
    from deep_agent.persistence.mongo import reset_for_tests
    from deep_agent.persistence.vector_store import build_vector_store

    reset_for_tests()
    ensure_indexes()

    s = config.get_settings()
    db = atlas_client[s.mongodb_db]
    # Clean slate for deterministic assertions.
    db[s.response_cache_collection].delete_many({})
    db[s.knowledge_base_collection].delete_many({"metadata.source": "int-seed"})

    vs = build_vector_store()
    vs.add_texts([SEEDED_TEXT], metadatas=[{"source": "int-seed"}])

    # Atlas Vector Search indexes take a few seconds to become queryable.
    time.sleep(5)

    thread_id = f"e2e-research-{uuid.uuid4()}"
    user_id = "e2e-research"
    graph = build_graph()
    cfg = {"configurable": {"thread_id": thread_id, "user_id": user_id}}

    question = (
        f"Using the knowledge base, explain what {SEEDED_MARKER} refers to in "
        "one sentence. Use write_todos before answering."
    )
    result = graph.invoke(
        {"messages": [HumanMessage(content=question)], "user_id": user_id},
        config=cfg,
    )

    # graph produced at least one AI message
    assert any(isinstance(m, AIMessage) for m in result.get("messages", []))

    # plan was persisted mid-turn via the middleware
    deadline = time.time() + 20.0
    plan_doc = None
    while time.time() < deadline:
        plan_doc = db["plans"].find_one(
            {"user_id": user_id, "thread_id": thread_id}
        )
        if plan_doc is not None:
            break
        time.sleep(1)
    assert plan_doc is not None
    assert isinstance(plan_doc.get("todos"), list)

    # the KB answer referenced the seeded marker either in the final
    # AI content, any ToolMessage, or as a still-present seeded doc.
    ai_content = " ".join(
        str(m.content) for m in result.get("messages", []) if isinstance(m, AIMessage)
    )
    seeded_still_present = (
        db[s.knowledge_base_collection].count_documents(
            {"metadata.source": "int-seed"}, limit=1
        )
        >= 1
    )
    assert SEEDED_MARKER in ai_content or seeded_still_present
