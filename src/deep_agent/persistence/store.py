"""LangGraph cross-thread long-term memory store backed by MongoDB Atlas.

Uses :class:`langgraph.store.mongodb.MongoDBStore` with a Voyage AI vector
index so ``store.search(namespace, query=...)`` returns items ranked by
semantic similarity.
"""
from __future__ import annotations

from langgraph.store.mongodb import MongoDBStore, create_vector_index_config

from ..config import get_settings
from ..models import get_embeddings
from .mongo import get_db


def build_store() -> MongoDBStore:
    """Return a :class:`MongoDBStore` with semantic-search enabled.

    Binds to ``Settings.mongodb_db``.
    """
    s = get_settings()
    collection = get_db()[s.long_term_memory_collection]

    index_config = create_vector_index_config(
        dims=s.voyage_dimensions,
        embed=get_embeddings(),
        fields=["$"],
        name=s.long_term_memory_vector_index,
        relevance_score_fn="cosine",
    )
    return MongoDBStore(collection=collection, index_config=index_config, auto_index_timeout=0)


def build_namespace(user_id: str) -> tuple[str, str, str]:
    """Return the canonical namespace for per-user long-term memory."""
    return ("user", user_id, "memories")
