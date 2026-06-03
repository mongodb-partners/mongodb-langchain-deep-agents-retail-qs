"""Subagent factories for the deep-agent graph."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from deepagents import SubAgent
from langchain_mongodb_agent_log import AgentLogMiddleware

from ..config import get_settings
from ..middleware.patch_dangling import PatchDanglingToolCallsMiddleware
from ..prompts import (
    BASKET_CROSS_SELL_PROMPT,
    DEAL_OPTIMIZER_PROMPT,
    LOYALTY_CONCIERGE_PROMPT,
    REORDER_CONCIERGE_PROMPT,
    RESEARCHER_PROMPT,
    WRITER_PROMPT,
)
from ..tools.cart import add_to_cart, update_cart_item, view_cart
from ..tools.database_toolkit import get_data_tools
from ..tools.fetch_and_cache import fetch_and_cache
from ..tools.knowledge_base_search import (
    knowledge_base_hybrid_search,
    knowledge_base_search,
)
from ..tools.knowledge_graph_search import knowledge_graph_search
from ..tools.memory import recall_memories
from ..tools.profile import current_shopper
from ..tools.savings import savings_calculator
from ..tools.web_search import web_search


def _subagent_log_middleware(agent_name: str) -> AgentLogMiddleware:
    """Attribute a subagent's super-steps in ``agent_log`` under its own
    ``agent_name`` instead of the main agent's ``"main"``.

    Uses the v0.3 package seam ``AgentLogMiddleware(log, agent_name=...)`` over
    the shared process-singleton engine. ``_agent_log`` is imported lazily from
    ``graph`` at call-time to avoid the graph<->subagents import cycle (graph
    imports this module at load time).
    """
    from ..graph import _agent_log

    return AgentLogMiddleware(_agent_log(), agent_name=agent_name)


def _subagent_skills_dir() -> list[str]:
    """Resolve the per-subagent skills dir.

    Custom subagents do not inherit the main agent's skills; we hand the same
    path so
    the researcher gets the same SKILL.md corpus.
    """
    raw = get_settings().agent_skills_dir
    candidate = Path(raw) if Path(raw).is_absolute() else Path.cwd() / raw
    return [str(candidate)] if candidate.is_dir() else []


def researcher_subagent() -> SubAgent:
    """Return the ``researcher`` subagent specification.

    Bound tools match the ``RESEARCHER_PROMPT`` instructions: KB retrieval
    (vector + hybrid + graph) and web tooling (search + fetch_and_cache).
    There is exactly one researcher subagent per process.
    """
    tools: list[Any] = [
        web_search,
        fetch_and_cache,
        knowledge_base_search,
        knowledge_base_hybrid_search,
        knowledge_graph_search,
    ]
    return SubAgent(
        name="researcher",
        description=(
            "Deep-dive a sub-question using the knowledge base and the web; "
            "ingest new findings back into the knowledge base."
        ),
        system_prompt=RESEARCHER_PROMPT,
        tools=tools,
        middleware=[
            PatchDanglingToolCallsMiddleware(),
            _subagent_log_middleware("researcher"),
        ],
        skills=_subagent_skills_dir(),
    )


def writer_subagent() -> SubAgent:
    """Return the ``writer`` subagent specification.

    Composition specialist. Takes a research bundle (paths in
    ``/workspace/**``) plus an outline from the planner, produces a
    long-form Markdown artifact, and saves it via ``write_file`` to a
    ``/workspace/**`` path — landing on the MongoDB-backed S3 VFS by way
    of the ``CompositeBackend`` default leg.

    Tool surface is intentionally bare: ``tools=[]`` so the harness
    provides ONLY the default filesystem tools (``read_file`` /
    ``write_file`` / ``edit_file`` / ``ls`` / ``glob`` / ``grep``).
    The writer does NOT inherit the researcher's KB or web tools — if
    the bundle is incomplete, the writer returns ``INSUFFICIENT_BUNDLE:
    ...`` and the planner routes back to the researcher.
    """
    return SubAgent(
        name="writer",
        description=(
            "Compose long-form artifacts (reports, briefs, summaries) from a "
            "research bundle of files in /workspace/**. Saves the final "
            "artifact via write_file. Has no KB or web tools — call the "
            "researcher first if you need new sources."
        ),
        system_prompt=WRITER_PROMPT,
        tools=[],
        middleware=[
            PatchDanglingToolCallsMiddleware(),
            _subagent_log_middleware("writer"),
        ],
        skills=_subagent_skills_dir(),
    )


def deal_optimizer_subagent(data_tools: list[Any] | None = None) -> SubAgent:
    """Return the ``deal_optimizer`` subagent.

    Optimizes coupon savings on the CURRENT cart: resolves coupon→SKU coverage
    via the knowledge graph + the structured ``promotions`` collection, applies
    the penny-exact optimal stack with the deterministic ``savings_calculator``,
    and writes a savings plan. Binds an explicit tool list so it does NOT
    inherit the main agent's ``place_order`` (checkout is main-agent-only).

    ``data_tools`` lets the caller share one NL→MQL toolkit build across the
    main agent and subagents; when omitted it builds its own.
    """
    if data_tools is None:
        data_tools = get_data_tools()
    tools: list[Any] = [
        view_cart,
        update_cart_item,
        savings_calculator,
        knowledge_graph_search,
        *data_tools,
    ]
    return SubAgent(
        name="deal_optimizer",
        description=(
            "Maximize savings on the shopper's current cart: stack the best "
            "manufacturer + store coupons (penny-exact via savings_calculator), "
            "apply them to the cart, and write a savings plan. Call this when "
            "the user asks to save money / find coupons on what's in their cart."
        ),
        system_prompt=DEAL_OPTIMIZER_PROMPT,
        tools=tools,
        middleware=[
            PatchDanglingToolCallsMiddleware(),
            _subagent_log_middleware("deal_optimizer"),
        ],
        skills=_subagent_skills_dir(),
    )


def loyalty_concierge_subagent(data_tools: list[Any] | None = None) -> SubAgent:
    """Return the ``loyalty_concierge`` subagent.

    Produces a personalized loyalty briefing by fusing the authoritative
    ``customers`` row + cross-thread ``recall_memories`` + the loyalty-program
    KB policy + year-to-date savings from ``orders``. No cart tools — this is
    informational, not a purchase.

    ``data_tools`` lets the caller share one NL→MQL toolkit build; when omitted
    it builds its own.
    """
    if data_tools is None:
        data_tools = get_data_tools()
    tools: list[Any] = [
        current_shopper,
        recall_memories,
        knowledge_base_search,
        knowledge_base_hybrid_search,
        *data_tools,
    ]
    return SubAgent(
        name="loyalty_concierge",
        description=(
            "Personalized loyalty briefing: tier perks, points value (100 pts = "
            "$1), spend-to-next-tier, and year-to-date savings for the current "
            "shopper. Call this for loyalty / points / membership questions."
        ),
        system_prompt=LOYALTY_CONCIERGE_PROMPT,
        tools=tools,
        middleware=[
            PatchDanglingToolCallsMiddleware(),
            _subagent_log_middleware("loyalty_concierge"),
        ],
        skills=_subagent_skills_dir(),
    )


def reorder_concierge_subagent(data_tools: list[Any] | None = None) -> SubAgent:
    """Return the ``reorder_concierge`` subagent.

    Derives repurchase cadence from the shopper's dated order history (NL→MQL
    ``$unwind``/``$group``/``$lookup`` + ``$dateFromString``/``$dateDiff`` over
    the ``"YYYY-MM-DD"`` strings) and adds a "due to reorder" basket to the
    cart. ``data_tools`` lets the caller share one NL→MQL toolkit build.
    """
    if data_tools is None:
        data_tools = get_data_tools()
    tools: list[Any] = [
        current_shopper,
        add_to_cart,
        view_cart,
        *data_tools,
    ]
    return SubAgent(
        name="reorder_concierge",
        description=(
            "Build a reorder basket from the shopper's purchase history — mines "
            "order cadence and adds regularly-bought staples that are due again "
            "to the cart. Call this when the user asks to reorder / restock / "
            "'what do I usually buy'."
        ),
        system_prompt=REORDER_CONCIERGE_PROMPT,
        tools=tools,
        middleware=[
            PatchDanglingToolCallsMiddleware(),
            _subagent_log_middleware("reorder_concierge"),
        ],
        skills=_subagent_skills_dir(),
    )


def basket_cross_sell_subagent(data_tools: list[Any] | None = None) -> SubAgent:
    """Return the ``basket_cross_sell`` subagent.

    Surfaces complementary items via real co-purchase affinity (NL→MQL
    ``$unwind`` self-join over ``orders``) + recipe completion
    (``knowledge_graph_search``), and optionally adds the strongest complements
    to the cart. ``data_tools`` lets the caller share one NL→MQL toolkit build.
    """
    if data_tools is None:
        data_tools = get_data_tools()
    tools: list[Any] = [
        add_to_cart,
        view_cart,
        knowledge_graph_search,
        *data_tools,
    ]
    return SubAgent(
        name="basket_cross_sell",
        description=(
            "Suggest complementary items for the current cart from real "
            "co-purchase affinity (orders) + recipe completion (knowledge "
            "graph). Call this for 'what goes with this' / complete-the-recipe."
        ),
        system_prompt=BASKET_CROSS_SELL_PROMPT,
        tools=tools,
        middleware=[
            PatchDanglingToolCallsMiddleware(),
            _subagent_log_middleware("basket_cross_sell"),
        ],
        skills=_subagent_skills_dir(),
    )
