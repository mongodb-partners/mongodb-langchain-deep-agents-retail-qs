"""Tavily web-search tool bound to the researcher subagent."""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from langchain_core.tools import tool

from ..config import get_settings

log = logging.getLogger(__name__)


class ToolDisabledError(RuntimeError):
    """Raised internally when a tool's prerequisites (e.g. an API key) are missing.

    Not surfaced to the LLM — the tool layer catches it and returns a sentinel
    so the parent agent's ``tool_use`` block always gets a ``tool_result``
    (Bedrock's strict validator otherwise rejects the next turn with
    "tool_use ids were found without tool_result blocks").
    """


@lru_cache(maxsize=1)
def _tavily_client() -> Any:
    from tavily import TavilyClient  # imported lazily so unit tests do not need the SDK

    s = get_settings()
    if s.tavily_api_key is None:  # pragma: no cover — gated at the tool layer
        raise ToolDisabledError("web_search disabled: TAVILY_API_KEY not set")
    return TavilyClient(api_key=s.tavily_api_key.get_secret_value())


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """Search the web via Tavily.

    Returns a JSON-serialized list of ``{url, title, content, score}``
    entries as a string. Best-effort: when ``TAVILY_API_KEY`` is not set or
    the Tavily client fails, returns a short note string so the
    ``tool_use``/``tool_result`` pairing stays intact.

    We deliberately return a string rather than a Python ``list`` because
    LangChain's Bedrock adapter (``_format_anthropic_messages``) silently
    drops ``ToolMessage`` content that is an empty list — leaving the
    corresponding ``tool_use`` orphaned and making Bedrock reject the next
    turn with ``messages.N: tool_use ids were found without tool_result
    blocks``. Returning a string guarantees the result survives the
    conversion.
    """
    import json

    s = get_settings()
    if s.tavily_api_key is None:
        log.warning("web_search disabled: TAVILY_API_KEY not set")
        return "web_search disabled: TAVILY_API_KEY not set; no web results."
    try:
        client = _tavily_client()
        resp = client.search(query, max_results=max_results)
    except Exception as exc:
        log.warning("web_search Tavily call failed: %s", exc)
        return f"web_search failed: {type(exc).__name__}: {exc}"
    hits: list[dict[str, Any]] = []
    for r in resp.get("results", []) or []:
        hits.append(
            {
                "url": r.get("url", ""),
                "title": r.get("title", ""),
                "content": r.get("content", ""),
                "score": r.get("score"),
            }
        )
    if not hits:
        return "No results."
    return json.dumps(hits, ensure_ascii=False)
