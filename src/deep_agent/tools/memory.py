"""Long-term memory tools.

Two tools let the main planner persist and recall cross-thread facts about
the user:

* :func:`remember_fact` - stores a short atom in ``long_term_memory``
  under the namespace ``("user", user_id, "memories")``. Embeddings are
  generated automatically by :class:`MongoDBStore`'s vector-index config
  so :func:`recall_memories` returns semantically-relevant items.

* :func:`recall_memories` - returns the top-N items from the same
  namespace, ranked by cosine similarity against the query.

Both tools resolve ``user_id`` at call time from the active LangGraph
runtime (``get_config()["configurable"]["user_id"]``), matching the
scoping documented in docs/security.md.

If the runtime is unavailable (bare Python invocation, tool-test harness)
the tools raise a clear ``MemoryScopeError`` rather than silently
writing to the wrong namespace.
"""
from __future__ import annotations

import uuid

from langchain_core.tools import tool
from langgraph.config import get_config, get_store

from ..persistence.store import build_namespace


class MemoryScopeError(RuntimeError):
    """Raised when user_id cannot be resolved from the runtime."""


def _resolve_user_id() -> str:
    try:
        cfg = get_config()
    except RuntimeError as exc:
        raise MemoryScopeError(
            "memory tools require an active LangGraph runtime (user_id is threaded "
            "via RunnableConfig.configurable)"
        ) from exc
    configurable = (cfg or {}).get("configurable") or {}
    user_id = configurable.get("user_id")
    if not user_id:
        raise MemoryScopeError(
            "user_id not present in RunnableConfig.configurable; "
            "memory tools cannot scope the namespace"
        )
    return str(user_id)


def _namespace(user_id: str) -> tuple[str, str, str]:
    # Delegate to build_namespace() so namespace construction (including
    # label sanitization) lives in exactly one place. See docs/security.md.
    return build_namespace(user_id)


@tool
def remember_fact(fact: str) -> str:
    """Persist a short fact about the user into long-term memory.

    Use this when the user shares a preference, a stable piece of
    context, or a goal that will be useful in FUTURE conversations
    (not just the current turn). Keep each fact atomic and self-contained.

    Args:
        fact: a single short statement (one or two sentences). Do not
              include secrets (API keys, tokens). The fact is scoped to
              the current user and is searchable across future threads.

    Returns: a confirmation string including the memory key.
    """
    if not fact or not fact.strip():
        return "refused: empty fact"
    try:
        user_id = _resolve_user_id()
    except MemoryScopeError as exc:
        return f"memory unavailable: {exc}"

    store = get_store()
    key = uuid.uuid4().hex
    # MongoDBStore's vector-index config indexes ``$`` (every top-level
    # field); we store the text under ``text`` so semantic search ranks
    # by content. Keep metadata minimal.
    store.put(
        _namespace(user_id),
        key,
        {"text": fact.strip()},
    )
    return f"remembered (key={key})"


@tool
def recall_memories(query: str, limit: int = 5) -> str:
    """Retrieve long-term memories about the user that are semantically
    related to ``query``.

    Call this at the start of a turn when the user's request references
    their preferences, prior context, or anything that might have been
    shared in an earlier conversation. Returns an empty list message
    when nothing relevant is found.

    Args:
        query: natural-language query to match against stored memories.
        limit: maximum number of memories to return (default 5, max 20).

    Returns: a newline-delimited string of recalled memories.
    """
    if not query or not query.strip():
        return "refused: empty query"
    limit = max(1, min(int(limit), 20))
    try:
        user_id = _resolve_user_id()
    except MemoryScopeError as exc:
        return f"memory unavailable: {exc}"

    store = get_store()
    items = store.search(_namespace(user_id), query=query.strip(), limit=limit)
    if not items:
        return "no matching memories"
    lines: list[str] = []
    for it in items:
        text = ""
        value = getattr(it, "value", None)
        if isinstance(value, dict):
            text = str(value.get("text", ""))
        if not text:
            text = str(value)
        lines.append(f"- {text}")
    return "\n".join(lines)


__all__ = ["MemoryScopeError", "recall_memories", "remember_fact"]
