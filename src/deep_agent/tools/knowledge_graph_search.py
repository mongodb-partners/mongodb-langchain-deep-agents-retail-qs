"""GraphRAG traversal tool backed by :class:`MongoDBGraphStore`."""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from ..persistence.graph_store import build_graph_store

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _graph_store() -> Any:
    """Cached graph-store singleton."""
    return build_graph_store()


@tool
def knowledge_graph_search(
    query: str, config: RunnableConfig | None = None
) -> str:
    """Traverse the knowledge graph to answer an entity-relation question.

    Returns a natural-language answer composed by the underlying
    ``MongoDBGraphStore.chat_response`` (which retrieves related entities,
    builds context, and asks the LLM).
    """
    try:
        result = _graph_store().chat_response(query)
    except Exception as exc:
        # chat_response both traverses (OperationFailure on a bad `$in` shape)
        # AND makes a live LLM call — a Bedrock
        # throttle/timeout/validation error would otherwise propagate and
        # leave the parent ``tool_use`` with no ``tool_result`` (the Bedrock
        # pairing rejection the sibling KB/web tools guard against). KG is
        # best-effort: surface a sentinel so the agent can still answer from
        # parametric knowledge and the next turn stays valid.
        log.warning("knowledge_graph_search failed: %s", exc)
        return "No matching entities found in the knowledge graph for this query."
    return str(getattr(result, "content", result))
