"""Deterministic stacked-coupon savings math.

``compute_savings`` is a pure function (integer-cents) so penny-exact savings
never depend on the LLM. Coupon-policy rules encoded: sale/member price first;
at most one manufacturer + one store coupon per item; a coupon never drives a
line below $0; points are valued at 100 pts = $1.
"""
from __future__ import annotations

from typing import Any

import pytest


def _promo(code: str, ptype: str, items: list[tuple[str, float]]) -> dict[str, Any]:
    return {
        "code": code,
        "type": ptype,
        "kind": "amount_off",
        "stackable": True,
        "applies_to": [{"product_id": pid, "amount": amt} for pid, amt in items],
    }


def test_TC_530_210_single_coupon_penny_exact() -> None:
    from deep_agent.tools.savings import compute_savings

    lines = [{"product_id": "p-3003", "qty": 2, "unit_price_usd": 5.99, "sale_price_usd": 4.99}]
    promos = [_promo("JFU-MEAT-2OFF", "store", [("p-3003", 1.00)])]

    out = compute_savings(lines, promos)
    assert out["coupon_savings_usd"] == 2.00  # $1 * 2
    assert out["sale_savings_usd"] == 2.00  # (5.99-4.99) * 2
    assert out["total_savings_usd"] == 4.00
    assert out["applied_coupons"] == ["JFU-MEAT-2OFF"]
    # new total = base 4.99*2 - coupon 2.00 = 7.98
    assert out["new_total_usd"] == 7.98


def test_TC_530_211_stacks_manufacturer_and_store_on_one_item() -> None:
    from deep_agent.tools.savings import compute_savings

    lines = [{"product_id": "p-3002", "qty": 1, "unit_price_usd": 7.99, "sale_price_usd": 5.99}]
    promos = [
        _promo("JFU-PASTA-50", "manufacturer", [("p-3002", 2.00)]),
        _promo("JFU-SAUCE-1OFF", "store", [("p-3002", 1.00)]),
    ]
    out = compute_savings(lines, promos)
    # base 5.99 - (2.00 + 1.00) = 2.99
    assert out["coupon_savings_usd"] == 3.00
    assert out["new_total_usd"] == 2.99
    assert set(out["applied_coupons"]) == {"JFU-PASTA-50", "JFU-SAUCE-1OFF"}
    assert out["total_savings_usd"] == 5.00  # 2.00 sale + 3.00 coupon


def test_TC_530_212_caps_one_per_type_and_floors_at_zero() -> None:
    from deep_agent.tools.savings import compute_savings

    # Two manufacturer coupons on one item → only the BEST applies (cap 1/type).
    # The chosen amount exceeds the price → final line floored at $0, never negative.
    lines = [{"product_id": "p-3008", "qty": 1, "unit_price_usd": 0.69, "sale_price_usd": None}]
    promos = [
        _promo("BIG", "manufacturer", [("p-3008", 5.00)]),
        _promo("SMALL", "manufacturer", [("p-3008", 0.10)]),
    ]
    out = compute_savings(lines, promos)
    assert out["applied_coupons"] == ["BIG"]  # best of type, only one manufacturer
    assert out["new_total_usd"] == 0.0  # floored, not -4.31
    assert out["coupon_savings_usd"] == 0.69  # capped at the price


def test_TC_530_213_points_to_dollars() -> None:
    from deep_agent.tools.savings import points_to_dollars

    assert points_to_dollars(4200) == 42.00
    assert points_to_dollars(9850) == 98.50
    assert points_to_dollars(0) == 0.0


def test_TC_530_214_savings_calculator_applies_to_cart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tool reads the live cart + promotions, computes savings, and stamps
    line_savings/applied_coupons onto the cart so checkout + the Cart panel
    reflect them."""
    import mongomock

    from deep_agent.tools import savings

    db = mongomock.MongoClient()["t"]
    db["promotions"].insert_many(
        [
            _promo("JFU-PASTA-50", "manufacturer", [("p-3001", 0.50), ("p-3002", 2.00)]),
            _promo("JFU-SAUCE-1OFF", "store", [("p-3002", 1.00)]),
        ]
    )
    db["carts"].insert_one(
        {
            # Natural key (user_id, thread_id); MongoDB owns the ObjectId _id.
            "user_id": "cust_R002",
            "thread_id": "t1",
            "lines": [
                {"product_id": "p-3002", "name": "Rao's Marinara", "qty": 1,
                 "unit_price_usd": 7.99, "sale_price_usd": 5.99},
            ],
        }
    )
    monkeypatch.setattr("deep_agent.tools.savings.get_db", lambda: db)
    monkeypatch.setattr(
        "deep_agent.tools.savings.get_config",
        lambda: {"configurable": {"user_id": "cust_R002", "thread_id": "cust_R002:t1"}},
    )

    out = savings.savings_calculator.invoke({})
    assert "5.00" in out or "$5" in out  # total savings shown

    cart = db["carts"].find_one({"user_id": "cust_R002", "thread_id": "t1"})
    line = cart["lines"][0]
    assert set(line["applied_coupons"]) == {"JFU-PASTA-50", "JFU-SAUCE-1OFF"}
    assert line["line_savings"] == 3.00
