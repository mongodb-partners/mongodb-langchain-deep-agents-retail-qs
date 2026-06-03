"""Knowledge-base vector store (Atlas Vector Search)."""
from __future__ import annotations

from langchain_mongodb import MongoDBAtlasVectorSearch

from ..config import get_settings
from ..models import get_embeddings
from .mongo import get_db


def build_vector_store() -> MongoDBAtlasVectorSearch:
    """Return a :class:`MongoDBAtlasVectorSearch` bound to the knowledge-base collection.

    Binds to ``Settings.mongodb_db``.
    """
    s = get_settings()
    collection = get_db()[s.knowledge_base_collection]
    return MongoDBAtlasVectorSearch(
        collection=collection,
        embedding=get_embeddings(),
        index_name=s.knowledge_base_vector_index,
        text_key="text",
        embedding_key="embedding",
        relevance_score_fn="cosine",
        dimensions=s.voyage_dimensions,
        auto_create_index=False,  # ensure_indexes() handles DDL explicitly
    )
