"""Query-keyed semantic response cache.

Tests the dedicated turn-level cache that embeds ONLY the user query and stores
the final response, scoped by (user_id, model). This replaces the unsafe
legacy prompt-level cache (which embedded the whole prompt and collided).
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest


class _FakeEmbeddings:
    """Records which method was called; returns a fixed query vector."""

    def __init__(self) -> None:
        self.embed_query_calls: list[str] = []
        self.embed_documents_calls: list[list[str]] = []

    def embed_query(self, text: str) -> list[float]:
        self.embed_query_calls.append(text)
        return [0.1, 0.2, 0.3]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.embed_documents_calls.append(texts)
        return [[0.1, 0.2, 0.3] for _ in texts]


def _make_cache(emb: Any, coll: Any, threshold: float = 0.9):
    from deep_agent.persistence.response_cache import ResponseCache

    return ResponseCache(
        collection=coll,
        embeddings=emb,
        index_name="response_cache_semantic_index",
        threshold=threshold,
    )


# ── query-only, symmetric embedding ──────────────────────────


def test_TC_520_101_embed_query_used_for_lookup_and_save() -> None:
    emb = _FakeEmbeddings()
    coll = MagicMock()
    coll.aggregate.return_value = iter([])
    cache = _make_cache(emb, coll)

    cache.lookup("find a chicken recipe", "u1", "m1")
    cache.save("find a chicken recipe", "u1", "m1", "here you go")

    # Both store and lookup go through embed_query (symmetric self-match);
    # embed_documents (the asymmetric doc model) is never used.
    assert emb.embed_query_calls == ["find a chicken recipe", "find a chicken recipe"]
    assert emb.embed_documents_calls == []


# ── similarity lookup with threshold ─────────────────────────


def test_TC_520_102_lookup_hit_at_or_above_threshold_returns_response() -> None:
    emb = _FakeEmbeddings()
    coll = MagicMock()
    coll.aggregate.return_value = iter([{"response": "cached answer", "score": 0.95}])
    cache = _make_cache(emb, coll, threshold=0.9)

    assert cache.lookup("q", "u1", "m1") == "cached answer"


def test_TC_520_103_lookup_below_threshold_returns_none() -> None:
    emb = _FakeEmbeddings()
    coll = MagicMock()
    coll.aggregate.return_value = iter([{"response": "too far", "score": 0.80}])
    cache = _make_cache(emb, coll, threshold=0.9)

    assert cache.lookup("q", "u1", "m1") is None


def test_TC_520_103b_lookup_no_results_returns_none() -> None:
    emb = _FakeEmbeddings()
    coll = MagicMock()
    coll.aggregate.return_value = iter([])
    cache = _make_cache(emb, coll)

    assert cache.lookup("q", "u1", "m1") is None


# ── scope by user_id AND model ───────────────────────────────


def test_TC_520_104_lookup_filters_by_user_id_and_model() -> None:
    emb = _FakeEmbeddings()
    coll = MagicMock()
    coll.aggregate.return_value = iter([])
    cache = _make_cache(emb, coll)

    cache.lookup("q", "alice", "claude-haiku")

    (pipeline,), _ = coll.aggregate.call_args
    vs = pipeline[0]["$vectorSearch"]
    assert vs["index"] == "response_cache_semantic_index"
    assert vs["path"] == "query_embedding"
    assert vs["filter"] == {
        "user_id": {"$eq": "alice"},
        "model": {"$eq": "claude-haiku"},
    }


# ── save shape ───────────────────────────────────────────────


def test_TC_520_105_save_inserts_full_doc() -> None:
    emb = _FakeEmbeddings()
    coll = MagicMock()
    cache = _make_cache(emb, coll)

    cache.save("my query", "bob", "claude-haiku", "the answer")

    (doc,), _ = coll.insert_one.call_args
    assert doc["query"] == "my query"
    assert doc["response"] == "the answer"
    assert doc["user_id"] == "bob"
    assert doc["model"] == "claude-haiku"
    assert doc["query_embedding"] == [0.1, 0.2, 0.3]
    assert "created_at" in doc


# ── build_response_cache singleton ──────────────────────────────────────────


def test_TC_520_106_build_returns_none_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_RESPONSE_CACHE", "false")
    from deep_agent import config
    from deep_agent.persistence import response_cache as rc

    config.get_settings.cache_clear()
    rc.build_response_cache.cache_clear()
    assert rc.build_response_cache() is None


def test_TC_520_107_build_returns_instance_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_RESPONSE_CACHE", "true")
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-test")
    from deep_agent import config
    from deep_agent.persistence import response_cache as rc

    config.get_settings.cache_clear()
    rc.build_response_cache.cache_clear()

    with patch("deep_agent.persistence.response_cache.get_db", return_value=MagicMock()), patch(
        "deep_agent.persistence.response_cache.get_embeddings", return_value=MagicMock()
    ):
        out = rc.build_response_cache()
    assert out is not None
    assert isinstance(out, rc.ResponseCache)
