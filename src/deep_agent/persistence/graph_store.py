"""GraphRAG knowledge graph (``MongoDBGraphStore``)."""
from __future__ import annotations

from langchain_mongodb.graphrag.graph import MongoDBGraphStore

from ..config import get_settings
from ..models import get_llm
from .mongo import get_db


def build_graph_store() -> MongoDBGraphStore:
    """Return a :class:`MongoDBGraphStore` using the shared LLM for entity extraction.

    Binds to ``Settings.mongodb_db``.
    """
    s = get_settings()
    collection = get_db()[s.knowledge_graph_collection]
    return MongoDBGraphStore(
        collection=collection,
        entity_extraction_model=get_llm(),
    )
