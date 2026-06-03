"""VFS / SSRF / embeddings / persistence hardening tests.

Self-contained behavior tests for the VFS / SSRF / embeddings / persistence
hardening. Server (CORS, /health) and graph (_agent_log singleton) cases are
covered in test_server.py / test_graph.py.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import mongomock
import pytest

# --- VFS: S3 key segment validation -------------------------


@pytest.mark.parametrize("bad", ["u1/evil", "..", "a/../b", "lead ", " trail", "ab\x00c"])
def test_TC_520_s3_thread_id_rejects_unsafe_segment(bad: str) -> None:
    from deep_agent.vfs.base import VfsError
    from deep_agent.vfs.s3_backend import S3Backend

    be = S3Backend(bucket="b", prefix="deep-agent", client=MagicMock())
    with pytest.raises(VfsError):
        be._key(bad, "notes.md")


def test_TC_520_s3_thread_id_accepts_composite() -> None:
    from deep_agent.vfs.s3_backend import S3Backend

    be = S3Backend(bucket="b", prefix="deep-agent", client=MagicMock())
    # The real f"{user_id}:{sub}" shape (contains ':') is a valid single segment.
    assert be._key("alice:abc123", "/workspace/x.md") == "deep-agent/alice:abc123/workspace/x.md"


# --- VFS: atomic metadata upsert ----------------------------


def test_TC_520_metadata_upsert_atomic_preserves_created_at() -> None:
    from deep_agent.vfs.metadata import VfsMetadataStore

    coll = mongomock.MongoClient()["t"]["vfs_files"]
    store = VfsMetadataStore(coll)
    first = store.upsert(
        thread_id="u1:t1", path="/workspace/a.md", size=10,
        content_type="text/markdown", backend="s3", locator="k1",
    )
    second = store.upsert(
        thread_id="u1:t1", path="/workspace/a.md", size=20,
        content_type="text/markdown", backend="s3", locator="k1",
    )
    # Single doc (atomic upsert, no duplicate), created_at stable, size updated.
    assert coll.count_documents({}) == 1
    assert second.created_at == first.created_at
    assert second.size == 20


# --- SSRF: CGNAT block + no redirect following ---------------


def test_TC_520_ssrf_blocks_cgnat() -> None:
    from deep_agent.tools.fetch_and_cache import _is_safe_url

    # 100.64.0.0/10 (RFC 6598) reports is_global=True but must be refused.
    with patch(
        "deep_agent.tools.fetch_and_cache.socket.getaddrinfo",
        return_value=[(2, 1, 6, "", ("100.64.1.1", 0))],
    ):
        ok, reason = _is_safe_url("http://internal.cgnat.example")
    assert ok is False
    assert "non-public" in reason


def test_TC_520_ssrf_still_allows_public() -> None:
    from deep_agent.tools.fetch_and_cache import _is_safe_url

    with patch(
        "deep_agent.tools.fetch_and_cache.socket.getaddrinfo",
        return_value=[(2, 1, 6, "", ("93.184.216.34", 0))],  # example.com
    ):
        ok, _ = _is_safe_url("http://example.com")
    assert ok is True


# --- Embeddings: output_dimension wired ---------------------


def test_TC_520_embeddings_pass_output_dimension(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOYAGE_DIMENSIONS", "512")
    from deep_agent import config, models

    config.get_settings.cache_clear()
    models.get_embeddings.cache_clear()
    captured: list[dict[str, Any]] = []

    def _fake_voyage(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return MagicMock()

    with patch("deep_agent.models.VoyageAIEmbeddings", side_effect=_fake_voyage):
        models.get_embeddings()
    models.get_embeddings.cache_clear()
    assert captured, "VoyageAIEmbeddings was not constructed"
    assert all(c.get("output_dimension") == 512 for c in captured)


# --- Persistence: KB vector index has no stray `mappings` key -


def test_TC_520_kb_vector_index_no_mappings_key() -> None:
    from deep_agent import config
    from deep_agent.persistence import indexes as idx

    config.get_settings.cache_clear()
    fake_db = mongomock.MongoClient()["deep_agent_test"]
    captured: list[dict[str, Any]] = []

    def _capture(coll: Any, definition: dict[str, Any], *, name: str, **kw: Any) -> None:
        captured.append({"name": name, "definition": definition})

    with (
        patch("deep_agent.persistence.indexes.get_db", return_value=fake_db),
        patch("deep_agent.persistence.indexes._safe_create_search_index", side_effect=_capture),
    ):
        idx.ensure_indexes()

    s = config.get_settings()
    kb_vec = next(c for c in captured if c["name"] == s.knowledge_base_vector_index)
    assert "mappings" not in kb_vec["definition"], "vectorSearch index must not carry a mappings key"
    assert "fields" in kb_vec["definition"]
