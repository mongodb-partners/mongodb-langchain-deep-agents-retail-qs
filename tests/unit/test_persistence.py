"""Sub-phase 04: persistence surface factories."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    from deep_agent import config, models
    from deep_agent.persistence import mongo

    config.get_settings.cache_clear()
    models.get_llm.cache_clear()
    models.get_embeddings.cache_clear()
    models.get_reranker.cache_clear()
    mongo.reset_for_tests()


def test_TC_04_010_build_checkpointer() -> None:
    from deep_agent.persistence import checkpointer

    fake_client = MagicMock()
    with patch("deep_agent.persistence.checkpointer.MongoDBSaver") as saver, patch(
        "deep_agent.persistence.checkpointer.get_client", return_value=fake_client
    ):
        checkpointer.build_checkpointer()
    _, kwargs = saver.call_args
    assert kwargs["client"] is fake_client
    assert kwargs["db_name"] == "deep_agent_test"
    assert kwargs["checkpoint_collection_name"] == "checkpoints"
    assert kwargs["writes_collection_name"] == "checkpoint_writes"


def test_TC_04_020_build_store() -> None:
    from deep_agent.persistence import store

    fake_embedder = MagicMock()
    fake_db = MagicMock()
    with patch("deep_agent.persistence.store.MongoDBStore") as s_cls, patch(
        "deep_agent.persistence.store.get_db", return_value=fake_db
    ), patch("deep_agent.persistence.store.get_embeddings", return_value=fake_embedder), patch(
        "deep_agent.persistence.store.create_vector_index_config"
    ) as mk_cfg:
        mk_cfg.return_value = {"cfg": True}
        store.build_store()
    _, kwargs = s_cls.call_args
    # collection was the configured long-term memory collection
    fake_db.__getitem__.assert_called_once_with("long_term_memory")
    assert kwargs["index_config"] == {"cfg": True}
    assert kwargs["auto_index_timeout"] == 0
    # index config received dims + embedder + configured index name
    _, cfg_kwargs = mk_cfg.call_args
    assert cfg_kwargs["dims"] == 1024
    assert cfg_kwargs["embed"] is fake_embedder
    assert cfg_kwargs["name"] == "memory_semantic_index"


def test_TC_04_021_build_namespace() -> None:
    from deep_agent.persistence.store import build_namespace

    assert build_namespace("alice") == ("user", "alice", "memories")


def test_TC_04_021b_build_namespace_sanitizes_periods() -> None:
    # LangGraph forbids periods in namespace labels; email-based user_ids
    # must be escaped or store.put() raises InvalidNamespaceError.
    from deep_agent.persistence.store import build_namespace

    assert build_namespace("first.last@mongodb.com") == (
        "user",
        "first_last@mongodb_com",
        "memories",
    )


def test_TC_04_050_build_vector_store() -> None:
    from deep_agent.persistence import vector_store

    fake_db = MagicMock()
    fake_embedder = MagicMock()
    with patch("deep_agent.persistence.vector_store.MongoDBAtlasVectorSearch") as cls, patch(
        "deep_agent.persistence.vector_store.get_db", return_value=fake_db
    ), patch("deep_agent.persistence.vector_store.get_embeddings", return_value=fake_embedder):
        vector_store.build_vector_store()
    _, kwargs = cls.call_args
    fake_db.__getitem__.assert_called_once_with("knowledge_base")
    assert kwargs["index_name"] == "vector_index"
    assert kwargs["text_key"] == "text"
    assert kwargs["embedding_key"] == "embedding"
    assert kwargs["relevance_score_fn"] == "cosine"
    assert kwargs["dimensions"] == 1024
    assert kwargs["auto_create_index"] is False
    assert kwargs["embedding"] is fake_embedder


def test_TC_04_060_build_graph_store() -> None:
    from deep_agent.persistence import graph_store

    fake_db = MagicMock()
    fake_llm = MagicMock()
    with patch("deep_agent.persistence.graph_store.MongoDBGraphStore") as cls, patch(
        "deep_agent.persistence.graph_store.get_db", return_value=fake_db
    ), patch("deep_agent.persistence.graph_store.get_llm", return_value=fake_llm):
        graph_store.build_graph_store()
    _, kwargs = cls.call_args
    fake_db.__getitem__.assert_called_once_with("knowledge_graph")
    assert kwargs["entity_extraction_model"] is fake_llm


# -------------------- db_name plumbing --------------------




# The ``build_llm_cache`` builder tests were removed with the
# cache itself. The response-cache builder is covered in
# tests/unit/test_response_cache.py.
