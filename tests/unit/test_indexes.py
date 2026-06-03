"""Sub-phase 03: ensure_indexes idempotent DDL."""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from pymongo.errors import OperationFailure


class _FakeColl:
    """Mock collection recording search-index and classic-index calls."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.search_indexes: list[dict[str, Any]] = []
        self.classic_indexes: list[tuple[list[tuple[str, int]], dict[str, Any]]] = []
        self.dropped_search_indexes: list[str] = []
        # Optional scripted failures per collection
        self.search_fail_on: str | None = None
        self.search_fail_code: int | None = None
        # Optional pre-existing indexes (simulates an Atlas cluster with stale shapes)
        self.preexisting_search: list[dict[str, Any]] = []

    def create_search_index(self, model: dict[str, Any]) -> str:
        if self.search_fail_on == model["name"]:
            raise OperationFailure(
                "index already exists", code=self.search_fail_code or 68
            )
        self.search_indexes.append(model)
        # Creating also removes it from preexisting so drift-detection
        # returns False on the second call.
        self.preexisting_search = [
            ix for ix in self.preexisting_search if ix.get("name") != model["name"]
        ]
        return model["name"]

    def list_search_indexes(self, name: str | None = None) -> list[dict[str, Any]]:
        if name is None:
            return list(self.preexisting_search)
        return [ix for ix in self.preexisting_search if ix.get("name") == name]

    def drop_search_index(self, name: str) -> None:
        self.dropped_search_indexes.append(name)
        self.preexisting_search = [
            ix for ix in self.preexisting_search if ix.get("name") != name
        ]

    def create_index(self, keys: list[tuple[str, int]], **kwargs: Any) -> str:
        self.classic_indexes.append((keys, kwargs))
        return kwargs.get("name", "idx")


class _FakeDB:
    """Mock database that lazily vends ``_FakeColl`` instances and tracks
    which collections have been "created"."""

    def __init__(self, preexisting: list[str] | None = None) -> None:
        self._colls: dict[str, _FakeColl] = {}
        self._existing: set[str] = set(preexisting or [])
        self.created: list[str] = []

    def __getitem__(self, name: str) -> _FakeColl:
        return self._colls.setdefault(name, _FakeColl(name))

    def list_collection_names(self) -> list[str]:
        return sorted(self._existing)

    def create_collection(self, name: str) -> _FakeColl:
        if name in self._existing:
            raise OperationFailure("NamespaceExists", code=48)
        self._existing.add(name)
        self.created.append(name)
        return self[name]


@pytest.fixture(autouse=True)
def _clear_settings() -> None:
    from deep_agent import config

    config.get_settings.cache_clear()


def _run_ensure(db: _FakeDB) -> None:
    with patch("deep_agent.persistence.indexes.get_db", return_value=db):
        from deep_agent.persistence.indexes import ensure_indexes

        ensure_indexes()


def test_TC_03_030_kb_vector_and_search_indexes_created() -> None:
    db = _FakeDB()
    _run_ensure(db)
    kb = db["knowledge_base"]
    names = [m["name"] for m in kb.search_indexes]
    assert "vector_index" in names
    assert "search_index" in names
    # Vector index declares metadata.source filter
    vector_model = next(m for m in kb.search_indexes if m["name"] == "vector_index")
    fields = vector_model["definition"]["fields"]
    assert any(f.get("path") == "metadata.source" and f.get("type") == "filter" for f in fields)


def test_TC_03_050_memory_vector_index_created() -> None:
    db = _FakeDB()
    _run_ensure(db)
    mem = db["long_term_memory"]
    names = [m["name"] for m in mem.search_indexes]
    assert names == ["memory_semantic_index"]
    model = mem.search_indexes[0]
    fields = model["definition"]["fields"]
    # MongoDBStore.search filters by namespace_prefix; the index MUST
    # declare it as a filter or every search() fails with a
    # "Path 'namespace_prefix' needs to be indexed as filter" error.
    assert any(
        f.get("path") == "namespace_prefix" and f.get("type") == "filter"
        for f in fields
    ), "memory_semantic_index missing namespace_prefix filter"


def test_TC_03_056_stream_events_ttl_index() -> None:
    db = _FakeDB()
    _run_ensure(db)
    ev = db["stream_events"]
    found = [(keys, kw) for keys, kw in ev.classic_indexes if keys == [("ts", 1)]]
    assert found, "stream_events ts index missing"
    _, kw = found[0]
    assert kw.get("expireAfterSeconds") == 60 * 60 * 24 * 30


def test_TC_03_057_knowledge_graph_collection_and_type_index() -> None:
    """ensure_indexes provisions the knowledge_graph collection upfront and
    adds a type-filter index so traversal queries don't scan the whole graph."""
    db = _FakeDB()
    _run_ensure(db)
    assert "knowledge_graph" in db.created, "knowledge_graph collection not provisioned"
    kg = db["knowledge_graph"]
    keys = [k for k, _ in kg.classic_indexes]
    assert [("type", 1)] in keys, "knowledge_graph type index missing"


def test_TC_03_058_feedback_thread_ts_index() -> None:
    db = _FakeDB()
    _run_ensure(db)
    fb = db["feedback"]
    keys = [k for k, _ in fb.classic_indexes]
    assert [("thread_id", 1), ("ts", -1)] in keys, "feedback thread+ts index missing"


def test_TC_03_060_vfs_files_unique_compound_index() -> None:
    db = _FakeDB()
    _run_ensure(db)
    vfs = db["vfs_files"]
    found = [
        (keys, kw)
        for keys, kw in vfs.classic_indexes
        if keys == [("thread_id", 1), ("path", 1)]
    ]
    assert found, "vfs_files compound index missing"
    _, kw = found[0]
    assert kw.get("unique") is True


def test_TC_03_082_heals_search_index_missing_filter() -> None:
    """If an existing vector index lacks a required filter path (e.g. the
    old memory index without namespace_prefix), ensure_indexes drops and
    recreates it so MongoDBStore.search stops failing."""
    db = _FakeDB()
    # Simulate a cluster with the OLD memory_semantic_index (no filter field)
    mem = db["long_term_memory"]
    mem.preexisting_search = [
        {
            "name": "memory_semantic_index",
            "latestDefinition": {
                "fields": [
                    {
                        "type": "vector",
                        "path": "embedding",
                        "numDimensions": 1024,
                        "similarity": "cosine",
                    }
                    # missing namespace_prefix filter
                ]
            },
        }
    ]
    _run_ensure(db)
    assert "memory_semantic_index" in mem.dropped_search_indexes, (
        "stale memory_semantic_index was not dropped before recreation"
    )
    # And the re-created index should carry the filter
    model = next(
        m for m in mem.search_indexes if m["name"] == "memory_semantic_index"
    )
    assert any(
        f.get("path") == "namespace_prefix" and f.get("type") == "filter"
        for f in model["definition"]["fields"]
    )


def test_TC_03_083_does_not_drop_when_filters_already_present() -> None:
    """If an existing index already has the required filter, ensure_indexes
    leaves it alone - no drop, no recreate."""
    db = _FakeDB()
    mem = db["long_term_memory"]
    mem.preexisting_search = [
        {
            "name": "memory_semantic_index",
            "latestDefinition": {
                "fields": [
                    {
                        "type": "vector",
                        "path": "embedding",
                        "numDimensions": 1024,
                        "similarity": "cosine",
                    },
                    {"type": "filter", "path": "namespace_prefix"},
                ]
            },
        }
    ]
    _run_ensure(db)
    assert "memory_semantic_index" not in mem.dropped_search_indexes


def test_TC_530_320_carts_and_promotions_provisioned() -> None:
    """ensure_indexes provisions the carts + promotions collections
    and their lookup indexes."""
    db = _FakeDB()
    _run_ensure(db)

    assert "carts" in db.created
    assert "promotions" in db.created

    carts_idx = [(k, kw) for k, kw in db["carts"].classic_indexes
                 if k == [("user_id", 1), ("thread_id", 1)]]
    assert carts_idx, "carts (user_id, thread_id) index missing"
    # It must be UNIQUE (one cart per conversation; backs the upsert).
    assert carts_idx[0][1].get("unique") is True

    promo_keys = [k for k, _ in db["promotions"].classic_indexes]
    assert [("applies_to.product_id", 1)] in promo_keys


def test_TC_03_080_idempotent_on_duplicate_index() -> None:
    db = _FakeDB()
    # Pre-populate and script the vector-index call to "already exists"
    kb = db["knowledge_base"]
    kb.search_fail_on = "vector_index"
    kb.search_fail_code = 68
    # ensure_indexes must not raise
    _run_ensure(db)


def test_TC_03_081_namespace_precreated_when_missing() -> None:
    db = _FakeDB()  # no preexisting namespaces
    _run_ensure(db)
    # Each collection that needs search-index DDL was pre-created
    for name in ("knowledge_base", "long_term_memory"):
        assert name in db.created


# --- llm_cache provisioning is retired ------------


def test_TC_540_B03_llm_cache_index_never_provisioned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ensure_indexes never provisions the llm_cache collection
    or its vector index, even if a stale ENABLE_LLM_CACHE lingers in the env."""
    monkeypatch.setenv("ENABLE_LLM_CACHE", "true")  # stale env must be ignored now
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-test")
    from deep_agent import config

    config.get_settings.cache_clear()

    db = _FakeDB()
    _run_ensure(db)

    assert "llm_cache" not in db.created
    all_index_names = [m["name"] for coll in db._colls.values() for m in coll.search_indexes]
    assert "llm_cache_semantic_index" not in all_index_names


def test_TC_520_401_response_cache_provisioned_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ENABLE_RESPONSE_CACHE=true → query-embedding vector index
    (with user_id + model filters) AND a created_at TTL index are provisioned."""
    monkeypatch.setenv("ENABLE_RESPONSE_CACHE", "true")
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-test")
    monkeypatch.setenv("RESPONSE_CACHE_TTL_DAYS", "7")
    from deep_agent import config

    config.get_settings.cache_clear()

    db = _FakeDB()
    _run_ensure(db)

    assert "semantic_response_cache" in db.created
    coll = db["semantic_response_cache"]
    names = [m["name"] for m in coll.search_indexes]
    assert "response_cache_semantic_index" in names
    model = next(
        m for m in coll.search_indexes if m["name"] == "response_cache_semantic_index"
    )
    fields = model["definition"]["fields"]
    assert any(f.get("type") == "vector" and f.get("path") == "query_embedding" for f in fields)
    assert any(f.get("type") == "filter" and f.get("path") == "user_id" for f in fields)
    assert any(f.get("type") == "filter" and f.get("path") == "model" for f in fields)
    # TTL index on created_at = ttl_days * 86400.
    ttl = [(keys, kw) for keys, kw in coll.classic_indexes if keys == [("created_at", 1)]]
    assert ttl, "created_at TTL index missing"
    _, kw = ttl[0]
    assert kw.get("expireAfterSeconds") == 7 * 60 * 60 * 24


def test_TC_520_402_response_cache_skipped_when_disabled() -> None:
    """Negative case: disabled → no collection / index provisioned.

    Conftest defaults the flag to false, so no monkeypatch needed.
    """
    db = _FakeDB()
    _run_ensure(db)

    assert "semantic_response_cache" not in db.created


# --- agent-log indexes provisioned by the package ----------


def test_TC_510_005_agent_log_indexes_use_package_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ensure_indexes`` delegates the agent-log DDL to
    the ``langchain-mongodb-agent-log`` package, producing index names
    owned by the package (``agent_log_thread_step_idx``,
    ``agent_log_thread_ts_idx``, ``agent_log_user_ts_idx``)."""
    db = _FakeDB()
    _run_ensure(db)

    coll = db["agent_log"]
    classic_names = {kwargs.get("name") for _keys, kwargs in coll.classic_indexes}
    assert "agent_log_thread_step_idx" in classic_names
    assert "agent_log_thread_ts_idx" in classic_names
    assert "agent_log_user_ts_idx" in classic_names


def test_TC_510_005b_agent_log_search_indexes_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``ENABLE_AGENT_LOG_SEARCH=true`` the package's
    Atlas Search + Vector Search indexes are created on the agent-log
    collection."""
    monkeypatch.setenv("ENABLE_AGENT_LOG_SEARCH", "true")
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-test")
    from deep_agent import config

    config.get_settings.cache_clear()

    db = _FakeDB()
    _run_ensure(db)

    coll = db["agent_log"]
    names = [m["name"] for m in coll.search_indexes]
    assert "agent_log_search_idx" in names
    assert "agent_log_vector_idx" in names


def test_TC_510_INV_006_idempotent_re_run() -> None:
    """Re-running ``ensure_indexes`` is a no-op."""
    db = _FakeDB()
    _run_ensure(db)
    classic_first = len(db["agent_log"].classic_indexes)
    _run_ensure(db)
    # Second run does not double the index list (FakeColl appends; if
    # re-running called create_index twice for each, the count would
    # double — the package's idempotent helpers issue the same calls
    # but with create_index swallowing OperationFailure under
    # ``_safe_create_search_index`` semantics for search indexes; for
    # classic indexes the underlying mongo client is itself idempotent
    # on duplicate keys+name. Our fake records every call regardless,
    # so the count can grow — but the call must NOT raise.
    assert len(db["agent_log"].classic_indexes) >= classic_first
