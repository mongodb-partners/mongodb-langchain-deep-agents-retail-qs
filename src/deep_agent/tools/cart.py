"""Shopping-cart tools (MongoDB ``carts`` collection).

The cart is the object the retail specialist subagents build into
(``deal_optimizer`` / ``reorder_concierge`` / ``basket_cross_sell``) and that
checkout (``place_order``) turns into an ``orders`` document. It is a
first-class operational surface: one document per shopper conversation,
identified by the NATURAL key ``(user_id, thread_id)``. MongoDB owns the
ObjectId ``_id`` — we never set it. A unique compound index on
``(user_id, thread_id)`` enforces one cart per conversation and backs the
upsert. ``thread_id`` here is the per-conversation *sub* (the server threads a
composite ``f"{user_id}:{sub}"`` as ``configurable.thread_id``; we strip the
``user_id`` prefix so the stored key matches the ``GET /cart`` read path, which
receives the bare sub).

These tools write via ``get_db()`` directly (deterministic pymongo), which is
exactly why the NL→MQL ``database_toolkit`` can stay strictly read-only:
mutations never flow through it, and ``carts`` is deliberately kept OUT of
``DATA_AGENT_ALLOW_LIST`` so the data agent cannot read or leak it.

Writes use ATOMIC MongoDB update operators (``$inc`` / ``$push`` / ``$pull``),
NOT read-modify-write. The planner fires independent ``add_to_cart`` calls in
PARALLEL within one super-step; a load→mutate→replace_one would let those
concurrent writes clobber each other (last-write-wins drops items). Atomic
operators let parallel adds of distinct products each ``$push`` their own line.

If the runtime is unavailable (bare Python, tool-test harness) the tools
return a sentinel string rather than raising — Bedrock pairs every
``tool_use`` with a ``tool_result``, so a raised exception would orphan the
turn.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

from langchain_core.tools import tool
from langgraph.config import get_config
from pymongo.errors import DuplicateKeyError

from ..config import get_settings
from ..persistence.mongo import get_db


class CartScopeError(RuntimeError):
    """Raised when the cart identity cannot be resolved from the runtime."""


def _sub(user_id: str, thread_id: str) -> str:
    """Return the bare conversation sub from a possibly-composite thread id.

    The server sets ``configurable.thread_id = f"{user_id}:{sub}"``; the
    ``GET /cart`` route passes the bare ``sub``. Accept either so the stored
    ``(user_id, thread_id)`` key is identical on the write and read paths.
    """
    prefix = f"{user_id}:"
    if user_id and thread_id.startswith(prefix):
        return thread_id[len(prefix):]
    return thread_id


def cart_key(user_id: str, thread_id: str) -> dict[str, str]:
    """The natural key for a cart document: ``{user_id, thread_id}`` (sub).

    Used by the cart tools (write), ``savings_calculator``, and the
    ``GET /cart`` route so they all converge on the same document. MongoDB owns
    the ObjectId ``_id``.
    """
    return {"user_id": user_id, "thread_id": _sub(user_id, thread_id)}


def _resolve_key() -> dict[str, str]:
    """Resolve the cart's natural key from the LangGraph runtime."""
    try:
        cfg = get_config()
    except RuntimeError as exc:  # outside a LangGraph runtime
        raise CartScopeError(
            "cart tools require an active LangGraph runtime (user_id / thread_id "
            "are threaded via RunnableConfig.configurable)"
        ) from exc
    configurable = (cfg or {}).get("configurable") or {}
    thread_id = configurable.get("thread_id")
    if not thread_id:
        raise CartScopeError(
            "thread_id not present in RunnableConfig.configurable; cart tools "
            "cannot scope the cart document"
        )
    user_id = str(configurable.get("user_id") or "")
    return cart_key(user_id, str(thread_id))


def _carts() -> Any:
    return get_db()[get_settings().carts_collection]


def _products() -> Any:
    # ``products`` is the literal operational collection (also NL→MQL read).
    return get_db()["products"]


def _orders() -> Any:
    return get_db()["orders"]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _effective_price(line: dict[str, Any]) -> float:
    sale = line.get("sale_price_usd")
    if sale is not None:
        return float(sale)
    return float(line.get("unit_price_usd", 0.0))


def _load_cart(key: dict[str, str]) -> dict[str, Any] | None:
    return cast("dict[str, Any] | None", _carts().find_one(key))


def _subtotal(lines: list[dict[str, Any]]) -> float:
    return round(sum(_effective_price(line) * int(line["qty"]) for line in lines), 2)


def _sale_savings(lines: list[dict[str, Any]]) -> float:
    total = 0.0
    for line in lines:
        unit = float(line.get("unit_price_usd", 0.0))
        eff = _effective_price(line)
        total += max(0.0, unit - eff) * int(line["qty"])
    return round(total, 2)


def _coupon_savings(lines: list[dict[str, Any]]) -> float:
    """Coupon savings deal_optimizer stamps onto lines (``line_savings``)."""
    return round(sum(float(line.get("line_savings", 0.0)) for line in lines), 2)


def cart_summary(cart: dict[str, Any] | None) -> dict[str, Any]:
    """Serialise a cart doc for the ``GET /cart`` route / Cart panel.

    Always returns a stable shape, even for a missing cart (empty lines).
    """
    lines = list((cart or {}).get("lines", []))
    return {
        "lines": lines,
        "subtotal": _subtotal(lines),
        "total_savings": round(_sale_savings(lines) + _coupon_savings(lines), 2),
        "updated_at": (cart or {}).get("updated_at"),
    }


def _render(cart: dict[str, Any]) -> str:
    lines = cart.get("lines", [])
    if not lines:
        return "cart is empty"
    rows = []
    for line in lines:
        eff = _effective_price(line)
        marker = (
            f" (sale {eff:.2f}, was {float(line['unit_price_usd']):.2f})"
            if line.get("sale_price_usd") is not None
            else f" ({eff:.2f})"
        )
        rows.append(f"- {line['qty']}x {line['name']}{marker}")
    subtotal = _subtotal(lines)
    savings = round(_sale_savings(lines) + _coupon_savings(lines), 2)
    body = "\n".join(rows)
    tail = f"\nSubtotal: ${subtotal:.2f}"
    if savings > 0:
        tail += f"  (you save ${savings:.2f})"
    return body + tail


@tool
def add_to_cart(product_id: str, qty: int = 1) -> str:
    """Add a product to the shopper's cart (or increase its quantity).

    Args:
        product_id: the catalog product id (e.g. ``p-3001``).
        qty: quantity to add (default 1). Negative values are ignored.

    Returns: a one-line confirmation with the updated cart line count.
    """
    qty = int(qty)
    if qty <= 0:
        return "refused: qty must be positive"
    try:
        key = _resolve_key()
    except CartScopeError as exc:
        return f"cart unavailable: {exc}"

    product = _products().find_one({"product_id": product_id})
    if product is None:
        return f"refused: unknown product {product_id}"

    coll = _carts()
    now = _now()
    # 1) Atomically bump an existing line for this product.
    res = coll.update_one(
        {**key, "lines.product_id": product_id},
        {"$inc": {"lines.$.qty": qty}, "$set": {"updated_at": now}},
    )
    if res.matched_count == 0:
        # 2) No existing line: atomically push a new one, creating the cart if
        #    needed. ``$push`` is atomic, so PARALLEL adds of distinct products
        #    each append their own line (no clobber). A concurrent create can
        #    lose the unique-index race (DuplicateKeyError) — the doc exists by
        #    then, so push again without the upsert.
        line = {
            "product_id": product_id,
            "name": product.get("name", product_id),
            "qty": qty,
            "unit_price_usd": float(product.get("price_usd", 0.0)),
            "sale_price_usd": product.get("sale_price_usd"),
        }
        try:
            coll.update_one(
                key,
                {"$push": {"lines": line}, "$set": {"updated_at": now}},
                upsert=True,
            )
        except DuplicateKeyError:
            coll.update_one(
                key, {"$push": {"lines": line}, "$set": {"updated_at": now}}
            )

    n = len((_load_cart(key) or {}).get("lines", []))
    return f"added {qty}x {product.get('name', product_id)} — cart has {n} line(s)"


@tool
def update_cart_item(product_id: str, qty: int) -> str:
    """Set the absolute quantity of a cart line. A quantity of 0 or less
    removes the line.

    Args:
        product_id: the catalog product id to update.
        qty: the new absolute quantity (``<= 0`` removes the line).
    """
    qty = int(qty)
    try:
        key = _resolve_key()
    except CartScopeError as exc:
        return f"cart unavailable: {exc}"

    coll = _carts()
    now = _now()
    if qty <= 0:
        res = coll.update_one(
            key,
            {"$pull": {"lines": {"product_id": product_id}}, "$set": {"updated_at": now}},
        )
        return f"removed {product_id}" if res.modified_count else f"refused: {product_id} not in cart"

    res = coll.update_one(
        {**key, "lines.product_id": product_id},
        {"$set": {"lines.$.qty": qty, "updated_at": now}},
    )
    return f"set {product_id} to qty {qty}" if res.matched_count else f"refused: {product_id} not in cart"


@tool
def remove_from_cart(product_id: str) -> str:
    """Remove a product from the cart entirely.

    Args:
        product_id: the catalog product id to remove.
    """
    try:
        key = _resolve_key()
    except CartScopeError as exc:
        return f"cart unavailable: {exc}"

    res = _carts().update_one(
        key,
        {"$pull": {"lines": {"product_id": product_id}}, "$set": {"updated_at": _now()}},
    )
    return f"removed {product_id}" if res.modified_count else f"refused: {product_id} not in cart"


@tool
def view_cart() -> str:
    """Show the current cart: line items, quantities, sale prices, and the
    running subtotal (sale prices applied)."""
    try:
        key = _resolve_key()
    except CartScopeError as exc:
        return f"cart unavailable: {exc}"
    return _render(_load_cart(key) or {"lines": []})


@tool
def clear_cart() -> str:
    """Empty the shopper's cart."""
    try:
        key = _resolve_key()
    except CartScopeError as exc:
        return f"cart unavailable: {exc}"
    _carts().update_one(
        key, {"$set": {"lines": [], "updated_at": _now()}}, upsert=True
    )
    return "cart cleared"


@tool
def place_order() -> str:
    """Place the order for the current cart and clear it.

    This is the human-in-the-loop checkout action: when ``HITL_TOOLS`` lists
    ``place_order`` the graph pauses for approval BEFORE this runs. On
    execution it writes a new ``orders`` document (attributed to the shopper,
    today's date, the cart's items and any applied coupons) and empties the
    cart. The write goes through pymongo directly — it does NOT use the
    read-only NL→MQL toolkit, and the order is keyed by the natural
    ``order_id`` field (MongoDB owns the ObjectId _id).

    Returns: an order confirmation, or a refusal if the cart is empty.
    """
    try:
        key = _resolve_key()
    except CartScopeError as exc:
        return f"cart unavailable: {exc}"
    user_id = key["user_id"]

    cart = _load_cart(key)
    lines = (cart or {}).get("lines", [])
    if not lines:
        return "refused: cart is empty"

    items: list[dict[str, Any]] = []
    coupons: set[str] = set()
    for line in lines:
        unit = float(line.get("unit_price_usd", 0.0))
        eff = _effective_price(line)
        qty = int(line["qty"])
        per_unit_discount = round(unit - eff, 2)
        items.append(
            {
                "product_id": line["product_id"],
                "name": line["name"],
                "qty": qty,
                "unit_price_usd": round(eff, 2),
                "discount_usd": round(per_unit_discount * qty, 2),
            }
        )
        coupons.update(line.get("applied_coupons", []) or [])

    total = round(_subtotal(lines) - _coupon_savings(lines), 2)
    savings = round(_sale_savings(lines) + _coupon_savings(lines), 2)
    order_id = f"o-{uuid.uuid4().hex[:10]}"
    # The orders collection is keyed by the natural ``order_id`` field;
    # MongoDB autogenerates the ObjectId _id (we never set it).
    order = {
        "order_id": order_id,
        "customer_id": user_id,
        "order_date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "status": "processing",
        "channel": "app",
        "items": items,
        "coupons_used": sorted(coupons),
        "savings_usd": savings,
        "total_usd": total,
    }
    _orders().insert_one(order)

    # Atomically empty the cart (keep the doc so the panel shows an empty cart).
    _carts().update_one(key, {"$set": {"lines": [], "updated_at": _now()}})
    return (
        f"order {order_id} placed — {len(items)} item(s), total ${total:.2f}"
        + (f", you saved ${savings:.2f}" if savings > 0 else "")
    )


CART_TOOLS = [
    add_to_cart,
    update_cart_item,
    remove_from_cart,
    view_cart,
    clear_cart,
]

__all__ = [
    "CART_TOOLS",
    "CartScopeError",
    "add_to_cart",
    "cart_key",
    "cart_summary",
    "clear_cart",
    "place_order",
    "remove_from_cart",
    "update_cart_item",
    "view_cart",
]
