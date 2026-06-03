"""Deterministic savings math for the deal_optimizer subagent.

``compute_savings`` is a PURE function over integer cents, so penny-exact
stacked-coupon savings never depend on LLM arithmetic. It encodes the
coupon-policy rules verbatim:

* the sale / member price applies first (``sale_price_usd`` when present);
* per item, at most ONE manufacturer + ONE store coupon may stack (best of
  each type wins);
* a coupon never drives a line below $0;
* loyalty points are valued at 100 pts = $1 (``points_to_dollars``).

``savings_calculator`` is the agent-facing tool: it reads the live cart +
``promotions`` collection, computes the optimal stack, stamps the chosen
coupons + per-line savings back onto the cart (so checkout and the Cart panel
reflect them), and returns a human-readable summary.
"""
from __future__ import annotations

from typing import Any, cast

from langchain_core.tools import tool
from langgraph.config import get_config

from ..config import get_settings
from ..persistence.mongo import get_db
from .cart import cart_key

_COUPON_TYPES = ("manufacturer", "store")


def _cents(value: float) -> int:
    return round(float(value) * 100)


def _dollars(cents: int) -> float:
    return round(cents / 100, 2)


def points_to_dollars(points: int) -> float:
    """Value loyalty points at 100 pts = $1."""
    return round(int(points) / 100, 2)


def compute_savings(
    lines: list[dict[str, Any]], promotions: list[dict[str, Any]]
) -> dict[str, Any]:
    """Compute penny-exact sale + stacked-coupon savings for ``lines``.

    Args:
        lines: cart lines ``{product_id, qty, unit_price_usd, sale_price_usd?}``.
        promotions: promotion docs ``{code, type, applies_to:[{product_id,
            amount}]}``.

    Returns a dict with per-line savings, the chosen coupon set, and totals.
    """
    # product_id -> list of (type, code, amount_cents)
    cover: dict[str, list[tuple[str, str, int]]] = {}
    for p in promotions:
        code = str(p.get("code", ""))
        ptype = str(p.get("type"))
        for app in p.get("applies_to", []) or []:
            cover.setdefault(str(app["product_id"]), []).append(
                (ptype, code, _cents(app["amount"]))
            )

    per_line: list[dict[str, Any]] = []
    total_coupon = 0
    total_sale = 0
    new_total = 0
    applied: set[str] = set()

    for line in lines:
        pid = str(line["product_id"])
        qty = int(line["qty"])
        unit = _cents(line.get("unit_price_usd", 0.0))
        sale = line.get("sale_price_usd")
        base = _cents(sale) if sale is not None else unit

        # best coupon per type (cap one manufacturer + one store)
        best: dict[str, tuple[int, str]] = {}
        for ptype, code, amt in cover.get(pid, []):
            if ptype not in best or amt > best[ptype][0]:
                best[ptype] = (amt, code)
        chosen = [best[t] for t in _COUPON_TYPES if t in best]

        discount_per_unit = min(sum(amt for amt, _ in chosen), base)  # floor at $0
        line_coupon = discount_per_unit * qty
        line_sale = max(0, unit - base) * qty
        codes = sorted(code for _, code in chosen)
        applied.update(codes)

        total_coupon += line_coupon
        total_sale += line_sale
        new_total += base * qty - line_coupon

        per_line.append(
            {
                "product_id": pid,
                "qty": qty,
                "base_price_usd": _dollars(base),
                "coupon_savings_usd": _dollars(line_coupon),
                "applied_coupons": codes,
            }
        )

    return {
        "lines": per_line,
        "sale_savings_usd": _dollars(total_sale),
        "coupon_savings_usd": _dollars(total_coupon),
        "total_savings_usd": _dollars(total_sale + total_coupon),
        "applied_coupons": sorted(applied),
        "new_total_usd": _dollars(new_total),
    }


def _render(result: dict[str, Any]) -> str:
    rows = []
    for pl in result["lines"]:
        if pl["coupon_savings_usd"] > 0:
            codes = ", ".join(pl["applied_coupons"])
            rows.append(
                f"- {pl['product_id']}: -${pl['coupon_savings_usd']:.2f} ({codes})"
            )
    body = "\n".join(rows) if rows else "- no stackable coupons matched the cart"
    return (
        f"{body}\n"
        f"Coupon savings: ${result['coupon_savings_usd']:.2f}; "
        f"sale savings: ${result['sale_savings_usd']:.2f}; "
        f"total you save: ${result['total_savings_usd']:.2f}. "
        f"New cart total: ${result['new_total_usd']:.2f}."
    )


@tool
def savings_calculator(coupons: list[str] | None = None) -> str:
    """Compute the best penny-exact coupon stack for the current cart and apply it.

    Reads the shopper's live cart and the ``promotions`` catalog, picks the
    optimal stack (≤1 manufacturer + ≤1 store coupon per item, sale price
    first, never below $0), stamps the chosen coupons and per-line savings onto
    the cart, and returns a summary. Arithmetic is deterministic — do NOT
    recompute the math yourself.

    Args:
        coupons: optional list of coupon codes to restrict evaluation to. When
            omitted, every applicable promotion is considered.
    """
    try:
        cfg = get_config()
    except RuntimeError:
        return "savings unavailable: no active LangGraph runtime"
    configurable = (cfg or {}).get("configurable") or {}
    thread_id = configurable.get("thread_id")
    if not thread_id:
        return "savings unavailable: thread_id missing from runtime"
    # The cart is keyed by the natural (user_id, thread_id) — MongoDB owns the
    # ObjectId _id. Resolve the same key the cart tools write under.
    key = cart_key(str(configurable.get("user_id") or ""), str(thread_id))

    s = get_settings()
    carts = get_db()[s.carts_collection]
    cart = cast("dict[str, Any] | None", carts.find_one(key))
    if not cart or not cart.get("lines"):
        return "cart is empty — add items before optimizing savings"

    promo_coll = get_db()[s.promotions_collection]
    # Promotions are keyed by the natural ``code`` field (not _id).
    query: dict[str, Any] = {"code": {"$in": list(coupons)}} if coupons else {}
    promotions = list(promo_coll.find(query))

    result = compute_savings(cart["lines"], promotions)

    # Apply: stamp the chosen coupons + per-line savings onto the cart so
    # checkout (place_order) and the Cart panel reflect the optimized basket.
    by_pid = {pl["product_id"]: pl for pl in result["lines"]}
    for line in cart["lines"]:
        pl = by_pid.get(line["product_id"])
        if pl is not None:
            line["applied_coupons"] = pl["applied_coupons"]
            line["line_savings"] = pl["coupon_savings_usd"]
    carts.replace_one(key, cart, upsert=True)

    return _render(result)


__all__ = ["compute_savings", "points_to_dollars", "savings_calculator"]
