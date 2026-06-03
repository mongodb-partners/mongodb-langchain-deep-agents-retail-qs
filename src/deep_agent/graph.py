"""Compile the deep-agent graph via :func:`deepagents.create_deep_agent`.

Single-domain reference. One graph compiled per process; no
``lru_cache``; no ``domain`` parameter. Vertical apps fork this repo and
swap prompts/tools/seeds; multi-tenant deployments are out of scope here.

``backend=`` is a :class:`deepagents.backends.composite.CompositeBackend`
that routes ``/memories/**`` to a per-user :class:`StoreBackend` and falls
through to :class:`MongoVfsBackend` for everything else. ``skills=`` is
populated from ``Settings.agent_skills_dir`` so SKILL.md files are loaded
on demand by the harness.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends.composite import CompositeBackend
from deepagents.backends.store import StoreBackend
from deepagents.middleware.permissions import FilesystemPermission
from langchain_mongodb_agent_log import AgentLog, AgentLogMiddleware
from langgraph.config import get_config
from langgraph.store.base import BaseStore

from .agents.subagents import (
    basket_cross_sell_subagent,
    deal_optimizer_subagent,
    loyalty_concierge_subagent,
    reorder_concierge_subagent,
    researcher_subagent,
    writer_subagent,
)
from .backends.mongo_backend import MongoVfsBackend
from .config import get_settings
from .middleware.patch_dangling import PatchDanglingToolCallsMiddleware
from .models import get_llm
from .persistence.checkpointer import build_checkpointer
from .persistence.mongo import get_db
from .persistence.store import build_namespace, build_store
from .prompts import MAIN_PROMPT
from .tools.cart import CART_TOOLS, place_order
from .tools.database_toolkit import get_data_tools
from .tools.fetch_and_cache import fetch_and_cache
from .tools.knowledge_base_search import (
    knowledge_base_hybrid_search,
    knowledge_base_search,
)
from .tools.knowledge_graph_search import knowledge_graph_search
from .tools.memory import recall_memories, remember_fact
from .tools.profile import current_shopper

log = logging.getLogger(__name__)

# Declarative write allow-list. ``..`` segments are
# rejected by ``VirtualFilesystem._validate_path`` on every write (reads/ls
# of a traversal path simply miss the exact-match metadata lookup) — NOT by
# the deepagents permission validator, which abstains. This allow-list layers
# on top so the LLM cannot write to arbitrary paths even within the rooted
# filesystem (e.g. blowing away ``/system/...`` style fictions).
#
# ``/memories/**`` is intentionally NOT writable via the
# generic write_file tool. That prefix routes (via CompositeBackend) to
# the StoreBackend → MongoDBStore.put surface, which is reserved for
# the typed ``remember_fact`` tool. Letting write_file land arbitrary
# Markdown bundles there caused E11000 collisions on the
# (namespace, key) multikey index when a writer subagent saved a
# /sources.md bundle to /memories/. Keep the writer in /workspace.
_FS_PERMISSIONS: list[FilesystemPermission] = [
    FilesystemPermission(
        operations=["write"],
        paths=[
            "/workspace/**",
            "/scratch/**",
            "/web_cache/**",
        ],
        mode="allow",
    ),
    # Default-deny writes everywhere else. Reads outside the allow-list
    # remain permitted (the agent often needs to read its own outputs).
    FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
]


_AGENT_LOG: AgentLog | None = None
_AGENT_LOG_LOCK = threading.Lock()


def _build_agent_log() -> AgentLog:
    s = get_settings()
    coll = get_db()[s.agent_log_collection]
    embeddings: Any = None
    if s.enable_agent_log_search:
        # The same Voyage embedder powers both the agent log and the KB
        # tools — lazy-import to keep the cycle clean.
        from .models import get_embeddings

        embeddings = get_embeddings()
    return AgentLog(
        collection=coll,
        embeddings=embeddings,
        max_content_bytes=s.agent_log_max_content_bytes,
        max_search_text_bytes=s.agent_log_search_text_max_bytes,
    )


def _agent_log() -> AgentLog:
    """Single ``AgentLog`` instance per process.

    The package's ``AgentLog`` carries a daemon worker thread; one instance
    per process preserves FIFO order across all concurrent super-steps.

    Construction is guarded by a double-checked lock rather than
    ``functools.lru_cache`` — lru_cache is not atomic across the user function
    body, so two threads racing the first call could each build an AgentLog
    (two daemon threads). ``_agent_log.cache_clear()`` is preserved for tests.
    """
    global _AGENT_LOG
    if _AGENT_LOG is not None:
        return _AGENT_LOG
    with _AGENT_LOG_LOCK:
        if _AGENT_LOG is None:
            _AGENT_LOG = _build_agent_log()
        return _AGENT_LOG


def _agent_log_cache_clear() -> None:
    """Reset the singleton (test helper; mirrors lru_cache's cache_clear)."""
    global _AGENT_LOG
    _AGENT_LOG = None


_agent_log.cache_clear = _agent_log_cache_clear  # type: ignore[attr-defined]


def _main_agent_tools(data_tools: list[Any] | None = None) -> list[Any]:
    if data_tools is None:
        data_tools = get_data_tools()
    s = get_settings()
    tools: list[Any] = [
        knowledge_base_search,
        knowledge_base_hybrid_search,
        knowledge_graph_search,
        fetch_and_cache,
        remember_fact,
        recall_memories,
        # Cart + checkout. ``place_order`` is the HITL target and MUST
        # live on the MAIN agent (subagents run under no checkpointer, so an
        # interrupt raised inside one cannot be resumed). ``current_shopper``
        # lets the planner identify the user for order/loyalty scoping.
        current_shopper,
        *CART_TOOLS,
        place_order,
        *data_tools,
    ]
    # Register the package's prebuilt past-conversations tool
    # only when hybrid agent-log search is enabled (otherwise the Atlas
    # indexes the tool depends on don't exist).
    if s.enable_agent_log_search:
        from langchain_mongodb_agent_log.retrieval.tool import build_tool

        from .models import get_embeddings

        # Thread the index-name + top_k settings through so the query
        # path matches the DDL path (no more dead AGENT_LOG_*_INDEX config).
        tools.append(
            build_tool(
                get_db()[s.agent_log_collection],
                embeddings=get_embeddings(),
                search_index=s.agent_log_search_index,
                vector_index=s.agent_log_vector_index,
                top_k=s.agent_log_search_top_k,
            )
        )
    return tools


def _hitl_interrupt_on() -> dict[str, Any] | None:
    """Pass ``interrupt_on=`` only when ``HITL_TOOLS`` is non-empty.

    The reference ships with no tool listed; verticals (e.g. disputes-analyst)
    flip this on for their own destructive tools.
    """
    raw = get_settings().hitl_tools.strip()
    if not raw:
        return None
    return {
        name.strip(): {"allowed_decisions": ["approve", "edit", "reject"]}
        for name in raw.split(",")
        if name.strip()
    }


def _middleware_chain() -> list[Any]:
    """Build the middleware list applied to the deep-agent.

    - PatchDanglingToolCallsMiddleware is Bedrock-only.
    - AgentLogMiddleware (from ``langchain-mongodb-agent-log``)
      runs unconditionally, replacing the earlier custom
      ``CheckpointMirrorMiddleware``. It writes one decoded log doc per
      super-step into ``Settings.agent_log_collection``.
    """
    s = get_settings()
    out: list[Any] = []
    if s.llm_provider == "bedrock":
        out.append(PatchDanglingToolCallsMiddleware())
    out.append(AgentLogMiddleware(_agent_log()))
    return out


def _resolve_skills_dir() -> list[str]:
    """Resolve ``Settings.agent_skills_dir`` to a list[str].

    Absolute paths are used as-is; relative paths resolve against
    ``os.getcwd()``. If the path does not exist, log a warning and return
    ``[]`` so graph build does not crash on a missing skills directory.
    """
    raw = get_settings().agent_skills_dir
    candidate = Path(raw) if Path(raw).is_absolute() else Path.cwd() / raw
    if candidate.is_dir():
        return [str(candidate)]
    log.warning("agent skills dir %s not found; loading no skills", candidate)
    return []


def _user_namespace(_rt: Any) -> tuple[str, ...]:
    """Namespace factory for the ``/memories/`` StoreBackend route.

    Reads ``user_id`` from the LangGraph runtime config and returns the
    canonical per-user memory namespace (matches
    :func:`deep_agent.persistence.store.build_namespace`).
    """
    try:
        cfg = get_config() or {}
    except (RuntimeError, KeyError):
        cfg = {}
    configurable = cfg.get("configurable") if isinstance(cfg, dict) else None
    user_id = "anonymous"
    if isinstance(configurable, dict):
        raw = configurable.get("user_id")
        if raw:
            user_id = str(raw)
    return build_namespace(user_id)


def _build_backend(store: BaseStore) -> CompositeBackend:
    """Composite backend with /memories route on the Store.

    The default leg is the existing :class:`MongoVfsBackend` (S3 blobs +
    MongoDB metadata, thread-scoped). The ``/memories/`` route is a
    :class:`StoreBackend` constructed with the same store the rest of the
    agent uses, so semantic memory tools and ``write_file('/memories/...')``
    converge on a single user-scoped surface.
    """
    return CompositeBackend(
        default=MongoVfsBackend(),
        routes={
            "/memories/": StoreBackend(store=store, namespace=_user_namespace),
        },
    )


def build_graph(model: str | None = None) -> Any:
    """Build and compile the deep-agent graph with MongoDB persistence wired in.

    Single-domain reference: one graph per process, no caching at this
    layer. The server lifespan caches the compiled graph in a
    module-level slot.

    Hybrid VFS routing via ``CompositeBackend`` and on-demand
    skill loading via ``skills=``.

    The optional ``model`` overrides ``Settings.llm_model``. The
    server caches one graph per model in ``server/app.py``.
    """
    interrupt_on = _hitl_interrupt_on()
    store = build_store()

    # Build the NL→MQL toolkit ONCE and share it with the main agent
    # and the data-driven subagents (avoids 3 toolkit builds; keeps a single
    # patch seam — ``graph.get_data_tools`` — for tests).
    data_tools = get_data_tools()

    kwargs: dict[str, Any] = dict(
        model=get_llm(model),
        tools=_main_agent_tools(data_tools),
        system_prompt=MAIN_PROMPT,
        subagents=[
            researcher_subagent(),
            writer_subagent(),
            deal_optimizer_subagent(data_tools=data_tools),
            loyalty_concierge_subagent(data_tools=data_tools),
            reorder_concierge_subagent(data_tools=data_tools),
            basket_cross_sell_subagent(data_tools=data_tools),
        ],
        middleware=_middleware_chain(),
        checkpointer=build_checkpointer(),
        store=store,
        backend=_build_backend(store),
        permissions=_FS_PERMISSIONS,
        skills=_resolve_skills_dir(),
        # No node cache is wired on create_deep_agent. LangGraph node
        # caching is exact-key over the state tuple — essentially never useful
        # in an LLM-driven graph. Semantic caching happens at the turn level via
        # the response cache; the per-model LLM cache is retired.
        name="deep-agent",
    )
    if interrupt_on is not None:
        kwargs["interrupt_on"] = interrupt_on

    return create_deep_agent(**kwargs)


def build_graph_uncheckpointed() -> Any:
    """Build the graph without persistence. Used by unit tests and linters.

    Passes ``backend=MongoVfsBackend()`` (no checkpointer/store,
    so the StoreBackend route would error — composite is reserved for the
    full builder).
    """
    data_tools = get_data_tools()
    return create_deep_agent(
        model=get_llm(),
        tools=_main_agent_tools(data_tools),
        system_prompt=MAIN_PROMPT,
        subagents=[
            researcher_subagent(),
            writer_subagent(),
            deal_optimizer_subagent(data_tools=data_tools),
            loyalty_concierge_subagent(data_tools=data_tools),
            reorder_concierge_subagent(data_tools=data_tools),
            basket_cross_sell_subagent(data_tools=data_tools),
        ],
        backend=MongoVfsBackend(),
    )
