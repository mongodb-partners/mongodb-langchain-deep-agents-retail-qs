"""LangGraph checkpointer backed by MongoDB Atlas.

Uses :class:`langgraph.checkpoint.mongodb.MongoDBSaver` — no custom MongoDB code.
"""
from __future__ import annotations

from langgraph.checkpoint.mongodb import MongoDBSaver

from ..config import get_settings
from .mongo import get_client


def build_checkpointer() -> MongoDBSaver:
    """Return a :class:`MongoDBSaver` configured against the shared client.

    Binds to ``Settings.mongodb_db``.
    """
    s = get_settings()
    return MongoDBSaver(
        client=get_client(),
        db_name=s.mongodb_db,
        checkpoint_collection_name=s.checkpoints_collection,
        writes_collection_name=s.checkpoint_writes_collection,
    )
