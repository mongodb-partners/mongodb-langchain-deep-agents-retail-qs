"""Current-shopper identity tool.

The server threads ``user_id`` through ``RunnableConfig.configurable`` (it is
the customer id in the retail demo), but it is NOT visible to the model as
text. Subagents that query ``customers`` / ``orders`` by ``customer_id`` (the
loyalty and reorder specialists) need to know WHO they are serving. This tool
resolves the runtime ``user_id`` and returns the customer's profile so the
agent can scope its NL→MQL queries — the same identity-from-runtime pattern as
:mod:`deep_agent.tools.memory` and :mod:`deep_agent.tools.cart`.
"""
from __future__ import annotations

from typing import Any, cast

from langchain_core.tools import tool
from langgraph.config import get_config

from ..persistence.mongo import get_db


def _resolve_user_id() -> str | None:
    try:
        cfg = get_config()
    except RuntimeError:
        return None
    configurable = (cfg or {}).get("configurable") or {}
    user_id = configurable.get("user_id")
    return str(user_id) if user_id else None


@tool
def current_shopper() -> str:
    """Return the current shopper's profile — customer id, name, loyalty tier and
    points, dietary preferences, household size.

    Call this FIRST when a request is about "my" orders, loyalty, points, or
    reorders, so you know which ``customer_id`` to scope queries to.
    """
    user_id = _resolve_user_id()
    if not user_id:
        return "shopper unavailable: no user_id in the runtime"
    doc = cast(
        "dict[str, Any] | None",
        get_db()["customers"].find_one({"customer_id": user_id}),
    )
    if doc is None:
        return (
            f"shopper {user_id}: no loyalty profile on file (treat as a guest; "
            "scope order/loyalty queries to customer_id="
            f"{user_id!r} if needed)"
        )
    prefs = ", ".join(doc.get("dietary_preferences", [])) or "none"
    return (
        f"customer_id={doc.get('customer_id', user_id)}; name={doc.get('name', '?')}; "
        f"loyalty_tier={doc.get('loyalty_tier', '?')}; "
        f"loyalty_points={doc.get('loyalty_points', 0)}; "
        f"dietary_preferences={prefs}; "
        f"household_size={doc.get('household_size', '?')}"
    )


__all__ = ["current_shopper"]
