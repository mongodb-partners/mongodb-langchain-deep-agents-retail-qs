"""Query-keyed semantic response cache (turn-level).

Replaces the retired prompt-level LangChain semantic cache, which
embedded the *entire* serialized prompt. That prompt is dominated by the ~15 KB
shared system prompt, so different user queries embedded at 0.978-0.988 cosine
and collided above the 0.9 threshold — it returned one answer for every query.

This cache (modelled on the ``memory-mcp`` reference) embeds ONLY the user query
and stores the final answer. Query-only embeddings are discriminative
(``"hi"`` vs ``"chicken recipe"`` ~0.19; genuine paraphrases ~0.84), so the
threshold is meaningful. Matches are scoped by an exact ``(user_id, model)``
filter so one user's personalized answer is never served to another, and a
``model`` change never serves a stale answer from a different model.

Embedding is symmetric by construction: both store and lookup use
``embed_query`` (the asymmetric Voyage embedder's query model), so an identical
query self-matches at cosine 1.0.
"""
from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from langchain_core.embeddings import Embeddings

from ..config import get_settings
from ..models import get_embeddings
from .mongo import get_db

# How many candidate vectors Atlas scans before applying the filter + limit.
_NUM_CANDIDATES = 50


class ResponseCache:
    """Semantic cache of final assistant responses, keyed by query similarity."""

    def __init__(
        self,
        *,
        collection: Any,
        embeddings: Embeddings,
        index_name: str,
        threshold: float,
    ) -> None:
        self._collection = collection
        self._embeddings = embeddings
        self._index_name = index_name
        self._threshold = threshold

    def lookup(self, query: str, user_id: str, model: str) -> str | None:
        """Return a cached response for a semantically-similar prior query by the
        same ``user_id`` and ``model``, or ``None`` on a miss / below threshold."""
        vector = self._embeddings.embed_query(query)
        pipeline = [
            {
                "$vectorSearch": {
                    "index": self._index_name,
                    "path": "query_embedding",
                    "queryVector": vector,
                    "numCandidates": _NUM_CANDIDATES,
                    "limit": 1,
                    "filter": {
                        "user_id": {"$eq": user_id},
                        "model": {"$eq": model},
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "response": 1,
                    "score": {"$meta": "vectorSearchScore"},
                }
            },
        ]
        for doc in self._collection.aggregate(pipeline):
            if float(doc.get("score", 0.0)) >= self._threshold:
                response = doc.get("response")
                return response if isinstance(response, str) else None
            return None  # top (nearest) result is below threshold → miss
        return None

    def save(self, query: str, user_id: str, model: str, response: str) -> None:
        """Store ``response`` for ``query`` scoped to ``(user_id, model)``."""
        vector = self._embeddings.embed_query(query)
        self._collection.insert_one(
            {
                "query": query,
                "response": response,
                "user_id": user_id,
                "model": model,
                "query_embedding": vector,
                "created_at": datetime.now(UTC),
            }
        )


@lru_cache(maxsize=1)
def build_response_cache() -> ResponseCache | None:
    """Return a process-singleton :class:`ResponseCache`, or ``None`` when the
    feature is disabled or no Voyage key is configured."""
    s = get_settings()
    if not s.enable_response_cache:
        return None
    if s.voyage_api_key is None:
        return None
    return ResponseCache(
        collection=get_db()[s.response_cache_collection],
        embeddings=get_embeddings(),
        index_name=s.response_cache_vector_index,
        threshold=s.response_cache_threshold,
    )
