"""Idempotent creation of Atlas Search / Vector Search / compound indexes.

Called once at application startup so adopters don't have to click through the
Atlas UI. Each index creation is wrapped in a try/except on
``OperationFailure`` so re-runs are safe.
"""
from __future__ import annotations

import contextlib
import logging
from typing import Any

from pymongo.errors import OperationFailure

from ..config import get_settings
from .mongo import get_db

log = logging.getLogger(__name__)


def _ensure_collection(db: Any, name: str) -> Any:
    """Atlas requires a namespace to exist before vector/search indexes can be
    created on it. ``listCollections`` + ``create_collection`` is cheap and
    idempotent (we swallow ``NamespaceExists``).
    """
    if name not in db.list_collection_names():
        try:
            db.create_collection(name)
        except OperationFailure as exc:
            if getattr(exc, "code", None) != 48:  # NamespaceExists
                raise
    return db[name]


def _search_index_needs_update(
    collection: Any, name: str, definition: dict[str, Any]
) -> bool:
    """Return True if an existing search index drifts from ``definition``.

    Detect drift on filter fields (existing behavior) AND on the vector
    field's ``numDimensions`` and ``similarity``. Atlas's
    ``create_search_index`` is a no-op when an
    index with the same name exists, so we need explicit drift detection
    to heal stale shapes — e.g. a vector index whose ``numDimensions``
    no longer matches the embedder.
    """
    want_filters = {
        f["path"]
        for f in definition.get("fields", [])
        if f.get("type") == "filter" and isinstance(f.get("path"), str)
    }
    want_vector = next(
        (f for f in definition.get("fields", []) if f.get("type") == "vector"),
        None,
    )
    if not want_filters and want_vector is None:
        return False
    try:
        existing = list(collection.list_search_indexes(name))
    except OperationFailure:
        return False
    for ix in existing:
        latest = ix.get("latestDefinition") or ix.get("definition") or {}
        # Filter-fields drift (existing rule).
        if want_filters:
            have_filters = {
                f.get("path")
                for f in latest.get("fields", [])
                if f.get("type") == "filter"
            }
            if not want_filters.issubset(have_filters):
                log.warning(
                    "search index %s drift: missing filter fields", name
                )
                return True
        # Vector-config drift.
        if want_vector is not None:
            have_vector = next(
                (f for f in latest.get("fields", []) if f.get("type") == "vector"),
                None,
            )
            if have_vector is not None:
                if want_vector.get("numDimensions") != have_vector.get(
                    "numDimensions"
                ):
                    log.warning(
                        "search index %s drift: numDimensions changed from %s to %s",
                        name,
                        have_vector.get("numDimensions"),
                        want_vector.get("numDimensions"),
                    )
                    return True
                if want_vector.get("similarity") != have_vector.get("similarity"):
                    log.warning(
                        "search index %s drift: similarity changed from %s to %s",
                        name,
                        have_vector.get("similarity"),
                        want_vector.get("similarity"),
                    )
                    return True
    return False


def _safe_create_search_index(
    collection: Any,
    definition: dict[str, Any],
    name: str,
    type: str = "vectorSearch",
) -> None:
    """Idempotently create an Atlas Search / Vector Search index. If the
    index already exists but is missing required filter paths, drop and
    recreate it (Atlas has no in-place filter-add API)."""
    model = {"name": name, "type": type, "definition": definition}

    dropped = False
    if _search_index_needs_update(collection, name, definition):
        log.warning(
            "search index %s on %s is missing required filter fields; "
            "dropping and recreating",
            name,
            collection.name,
        )
        with contextlib.suppress(OperationFailure):
            collection.drop_search_index(name)
            dropped = True

    try:
        collection.create_search_index(model=model)
        log.info("created Atlas Search index %s on %s", name, collection.name)
    except OperationFailure as exc:
        if "already exists" in str(exc).lower() or getattr(exc, "code", None) in (68, 86):
            if dropped:
                # Atlas deletes search indexes asynchronously,
                # so the recreate can race the in-flight drop and come back
                # "already exists" — leaving the OLD drifted index in place.
                # Do NOT treat this as success: warn loudly so an operator
                # re-runs the bootstrap once the drop has settled.
                log.warning(
                    "search index %s on %s: recreate raced the async drop and "
                    "returned 'already exists'; the drifted index may persist. "
                    "Re-run index provisioning once the drop completes.",
                    name,
                    collection.name,
                )
                return
            log.info("index %s already exists on %s - skipping", name, collection.name)
            return
        raise


def ensure_indexes() -> None:
    """Create every Atlas index the runtime needs. Idempotent.

    Binds to ``Settings.mongodb_db``.
    """
    s = get_settings()
    db = get_db()

    # Knowledge base — vector + text
    kb = _ensure_collection(db, s.knowledge_base_collection)
    _safe_create_search_index(
        kb,
        {
            # A vectorSearch index takes only ``fields``; the
            # stray ``mappings`` key (valid only for a lexical ``search`` index)
            # was a copy/paste artifact Atlas ignores. Dropped to match the
            # long_term_memory vector definition.
            "fields": [
                {
                    "type": "vector",
                    "path": "embedding",
                    "numDimensions": s.voyage_dimensions,
                    "similarity": "cosine",
                },
                {"type": "filter", "path": "metadata.source"},
            ],
        },
        name=s.knowledge_base_vector_index,
    )
    _safe_create_search_index(
        kb,
        {"mappings": {"dynamic": True}},
        name=s.knowledge_base_search_index,
        type="search",
    )

    # The retired prompt-level LLM cache index is no longer
    # provisioned (see the response cache below).

    # Query-keyed semantic RESPONSE cache. Vector over the QUERY
    # embedding (not the whole prompt) + exact (user_id, model) filters, so
    # different queries / users / models never collide. A TTL index on
    # ``created_at`` bounds staleness (prices/inventory drift). Provisioned
    # only when the feature is enabled.
    if s.enable_response_cache:
        rc = _ensure_collection(db, s.response_cache_collection)
        _safe_create_search_index(
            rc,
            {
                "fields": [
                    {
                        "type": "vector",
                        "path": "query_embedding",
                        "numDimensions": s.voyage_dimensions,
                        "similarity": "cosine",
                    },
                    {"type": "filter", "path": "user_id"},
                    {"type": "filter", "path": "model"},
                ]
            },
            name=s.response_cache_vector_index,
        )
        rc.create_index(
            [("created_at", 1)],
            name="response_cache_ttl_idx",
            expireAfterSeconds=s.response_cache_ttl_days * 60 * 60 * 24,
        )

    # Long-term memory. MongoDBStore.search runs an aggregation that filters
    # by ``namespace_prefix`` before/around the $vectorSearch stage, so Atlas
    # requires that field to be declared as a filter. Without it, every
    # search() raises "Path 'namespace_prefix' needs to be indexed as filter".
    mem = _ensure_collection(db, s.long_term_memory_collection)

    # Cleanup: prior schemas wrote ``namespace`` as a flat string
    # (e.g. ``"memories"``). The current MongoDBStore writes it as a tuple
    # array (``["user", <user_id>, "memories"]``). The compound unique
    # index ``(namespace, key)`` is a multikey index, so an array entry
    # ``'memories'`` collides with a stale scalar ``"memories"`` doc on
    # the same key — surfaced as ``E11000 duplicate key error`` whenever
    # any user's ``remember_fact`` lands a doc whose key matches a stale
    # one. Purge non-array namespace docs at startup; safe because the
    # current code path never writes that shape.
    delete_many = getattr(mem, "delete_many", None)
    if callable(delete_many):
        try:
            # The delete is an unindexed full-scan (no index on
            # ``namespace``, and ``$not``/``$type`` can't use one). Probe first
            # with a cheap find_one + limit so the expensive delete only runs
            # when a stale legacy doc actually exists — instead of scanning the
            # whole collection on every boot for a one-time migration.
            stale_filter = {"namespace": {"$not": {"$type": "array"}}}
            probe = mem.find_one(stale_filter, {"_id": 1})
            if probe is not None:
                result = delete_many(stale_filter)
                if getattr(result, "deleted_count", 0):
                    log.warning(
                        "purged %d stale long_term_memory docs with non-array namespace",
                        result.deleted_count,
                    )
        except OperationFailure as exc:
            log.warning("long_term_memory cleanup skipped: %s", exc)
    _safe_create_search_index(
        mem,
        {
            "fields": [
                {
                    "type": "vector",
                    "path": "embedding",
                    "numDimensions": s.voyage_dimensions,
                    "similarity": "cosine",
                },
                {"type": "filter", "path": "namespace_prefix"},
            ]
        },
        name=s.long_term_memory_vector_index,
    )

    # Chat history — ``MongoDBChatMessageHistory(create_index=True)`` owns the
    # ``(SessionId, 1)`` index and picks its own name. Creating it here under a
    # different name raises ``IndexOptionsConflict`` (code 85) at first write.

    # Knowledge graph — MongoDBGraphStore lazy-creates the collection on first
    # add_documents(), but if seed never succeeds (e.g. first boot before
    # seeding) then knowledge_graph_search tools fail with "collection does
    # not exist". Provision upfront and add a type index to make traversal
    # queries (filter by entity type) cheap.
    kg = _ensure_collection(db, s.knowledge_graph_collection)
    with contextlib.suppress(OperationFailure):
        kg.create_index([("type", 1)], name="kg_type_idx")

    # Stream events — 30-day TTL
    events = _ensure_collection(db, s.stream_events_collection)
    with contextlib.suppress(OperationFailure):
        events.create_index(
            [("ts", 1)],
            expireAfterSeconds=60 * 60 * 24 * 30,
            name="ts_ttl_idx",
        )

    # Feedback — index by thread_id for lookup when investigating a turn
    feedback = _ensure_collection(db, s.feedback_collection)
    with contextlib.suppress(OperationFailure):
        feedback.create_index(
            [("thread_id", 1), ("ts", -1)],
            name="feedback_thread_ts_idx",
        )

    # VFS metadata — unique per (thread_id, path) so duplicate writes upsert
    vfs = db[s.vfs_files_collection]
    with contextlib.suppress(OperationFailure):
        vfs.create_index(
            [("thread_id", 1), ("path", 1)],
            unique=True,
            name="vfs_thread_path_unique",
        )

    # Retail commerce surfaces.
    # ``carts`` is written ONLY by the dedicated cart tools (never NL→MQL).
    # Documents are keyed by the NATURAL ``(user_id, thread_id)`` — MongoDB owns
    # the ObjectId _id. The UNIQUE compound index enforces one cart per
    # conversation and backs the upsert (the DuplicateKeyError create-race
    # fallback in add_to_cart depends on this uniqueness).
    carts = _ensure_collection(db, s.carts_collection)
    with contextlib.suppress(OperationFailure):
        carts.create_index(
            [("user_id", 1), ("thread_id", 1)],
            name="carts_user_thread_uniq",
            unique=True,
        )

    # ``promotions`` holds structured coupon terms the savings_calculator reads.
    # Index the per-item product mapping for fast coupon→SKU coverage lookups.
    promotions = _ensure_collection(db, s.promotions_collection)
    with contextlib.suppress(OperationFailure):
        promotions.create_index(
            [("applies_to.product_id", 1)],
            name="promotions_applies_idx",
        )

    # Operational collections key on a NATURAL field (MongoDB owns the
    # ObjectId _id). Unique indexes on those keys make re-seeding idempotent and
    # the tool lookups (cart product, profile customer, NL→MQL joins) fast.
    # Create against freshly-seeded data; on a collection still holding legacy
    # _id-keyed docs the unique create may fail and is suppressed.
    for coll_name, key in (
        ("products", "product_id"),
        ("customers", "customer_id"),
        ("orders", "order_id"),
        ("promotions", "code"),
    ):
        coll = _ensure_collection(db, coll_name)
        with contextlib.suppress(OperationFailure):
            coll.create_index([(key, 1)], unique=True, name=f"{coll_name}_{key}_uniq")

    # Agent log persistence + hybrid retrieval are owned by the
    # langchain-mongodb-agent-log package. Delegate the index DDL here so
    # the package owns the doc shape (``agent_log_text`` + ``agent_log_embedding``)
    # and the index names (``agent_log_thread_step_idx``,
    # ``agent_log_search_idx``, ``agent_log_vector_idx``) end-to-end.
    from langchain_mongodb_agent_log import (
        ensure_agent_log_indexes,
        ensure_search_indexes,
    )

    agent_log = _ensure_collection(db, s.agent_log_collection)
    ttl = s.agent_log_retention_days * 86400 if s.agent_log_retention_days > 0 else None
    ensure_agent_log_indexes(agent_log, ttl_seconds=ttl)
    if s.enable_agent_log_search:
        # Pass the configured index names so DDL and query paths
        # (graph.build_tool) use one source of truth — no silent drift.
        ensure_search_indexes(
            agent_log,
            embeddings_dim=s.voyage_dimensions,
            vector_index=s.agent_log_vector_index,
            search_index=s.agent_log_search_index,
        )
