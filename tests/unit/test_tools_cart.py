"""Unit tests for the cart tools (carts collection write path).

The cart is a MongoDB-backed collection driven by deterministic write-tools so
the NL→MQL toolkit can stay strictly read-only. Identity (the cart ``_id``) is
the composite ``thread_id`` resolved from the LangGraph runtime, mirroring
``tools/memory.py``.
"""
from __future__ import annotations

from typing import Any

import pytest


def _db_with_products() -> Any:
    import mongomock

    db = mongomock.MongoClient()["deep_agent_test"]
    db["products"].insert_many(
        [
            {
                "product_id": "p-3001",
                "name": "Barilla Spaghetti",
                "price_usd": 1.49,
                "sale_price_usd": None,
                "in_stock": True,
            },
            {
                "product_id": "p-3002",
                "name": "Rao's Homemade Marinara Sauce",
                "price_usd": 7.99,
                "sale_price_usd": 5.99,
                "in_stock": True,
            },
        ]
    )
    return db


def _bind(monkeypatch: pytest.MonkeyPatch, db: Any, *, thread: str = "alice:t1",
          user: str = "alice") -> None:
    from deep_agent.tools import cart

    monkeypatch.setattr("deep_agent.tools.cart.get_db", lambda: db)
    monkeypatch.setattr(
        "deep_agent.tools.cart.get_config",
        lambda: {"configurable": {"user_id": user, "thread_id": thread}},
    )
    return cart


def test_TC_530_110_add_to_cart_upserts_line_with_product_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _db_with_products()
    cart = _bind(monkeypatch, db)

    out = cart.add_to_cart.invoke({"product_id": "p-3002", "qty": 2})
    assert "Rao's Homemade Marinara Sauce" in out

    doc = db["carts"].find_one({"user_id": "alice", "thread_id": "t1"})
    assert doc is not None
    assert doc["user_id"] == "alice"
    (line,) = doc["lines"]
    assert line["product_id"] == "p-3002"
    assert line["qty"] == 2
    assert line["unit_price_usd"] == 7.99
    assert line["sale_price_usd"] == 5.99

    # Second add of the same product sums the quantity (not a duplicate line).
    cart.add_to_cart.invoke({"product_id": "p-3002", "qty": 1})
    doc = db["carts"].find_one({"user_id": "alice", "thread_id": "t1"})
    assert len(doc["lines"]) == 1
    assert doc["lines"][0]["qty"] == 3


def test_TC_530_111_update_remove_view_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _db_with_products()
    cart = _bind(monkeypatch, db)

    cart.add_to_cart.invoke({"product_id": "p-3001", "qty": 1})
    cart.add_to_cart.invoke({"product_id": "p-3002", "qty": 2})

    # view_cart uses the sale price for the subtotal when present.
    view = cart.view_cart.invoke({})
    assert "Barilla Spaghetti" in view
    # subtotal = 1.49 + 2*5.99 = 13.47
    assert "13.47" in view

    # update to an absolute quantity
    cart.update_cart_item.invoke({"product_id": "p-3001", "qty": 4})
    doc = db["carts"].find_one({"user_id": "alice", "thread_id": "t1"})
    spaghetti = next(line for line in doc["lines"] if line["product_id"] == "p-3001")
    assert spaghetti["qty"] == 4

    # qty <= 0 removes the line
    cart.update_cart_item.invoke({"product_id": "p-3001", "qty": 0})
    doc = db["carts"].find_one({"user_id": "alice", "thread_id": "t1"})
    assert all(line["product_id"] != "p-3001" for line in doc["lines"])

    # explicit remove
    cart.remove_from_cart.invoke({"product_id": "p-3002"})
    doc = db["carts"].find_one({"user_id": "alice", "thread_id": "t1"})
    assert doc["lines"] == []

    # clear is idempotent and reports an empty cart
    out = cart.clear_cart.invoke({})
    assert "cleared" in out.lower() or "empty" in out.lower()


def test_TC_530_112_no_identity_returns_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _db_with_products()
    from deep_agent.tools import cart

    monkeypatch.setattr("deep_agent.tools.cart.get_db", lambda: db)
    monkeypatch.setattr(
        "deep_agent.tools.cart.get_config", lambda: {"configurable": {}}
    )
    out = cart.add_to_cart.invoke({"product_id": "p-3001", "qty": 1})
    assert "cart unavailable" in out
    # nothing written
    assert db["carts"].count_documents({}) == 0


def test_TC_530_113_two_threads_have_isolated_carts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _db_with_products()
    cart = _bind(monkeypatch, db, thread="alice:t1", user="alice")
    cart.add_to_cart.invoke({"product_id": "p-3001", "qty": 1})

    # Same user, different thread → a separate cart.
    _bind(monkeypatch, db, thread="alice:t2", user="alice")
    cart.add_to_cart.invoke({"product_id": "p-3002", "qty": 5})

    t1 = db["carts"].find_one({"user_id": "alice", "thread_id": "t1"})
    t2 = db["carts"].find_one({"user_id": "alice", "thread_id": "t2"})
    assert [line["product_id"] for line in t1["lines"]] == ["p-3001"]
    assert [line["product_id"] for line in t2["lines"]] == ["p-3002"]
    # MongoDB owns the ObjectId _id — the composite is NOT stored as _id.
    assert not isinstance(t1["_id"], str)


def test_TC_530_130_place_order_writes_order_and_clears_cart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _db_with_products()
    cart = _bind(monkeypatch, db, thread="cust_R001:t1", user="cust_R001")

    cart.add_to_cart.invoke({"product_id": "p-3001", "qty": 2})  # 2 * 1.49
    cart.add_to_cart.invoke({"product_id": "p-3002", "qty": 1})  # sale 5.99 (saves 2.00)

    out = cart.place_order.invoke({})
    assert "order" in out.lower()

    # A new order doc exists, attributed to the user, in processing status.
    order = db["orders"].find_one({"customer_id": "cust_R001"})
    assert order is not None
    assert order["status"] == "processing"
    assert order["channel"] == "app"
    pids = {item["product_id"] for item in order["items"]}
    assert pids == {"p-3001", "p-3002"}
    # total = 2*1.49 + 1*5.99 = 8.97 ; savings (sale) = 7.99-5.99 = 2.00
    assert order["total_usd"] == 8.97
    assert order["savings_usd"] == 2.0

    # Cart is cleared after checkout.
    doc = db["carts"].find_one({"user_id": "cust_R001", "thread_id": "t1"})
    assert doc is None or doc["lines"] == []


def test_TC_530_130b_place_order_empty_cart_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _db_with_products()
    cart = _bind(monkeypatch, db)
    out = cart.place_order.invoke({})
    assert "empty" in out.lower()
    assert db["orders"].count_documents({}) == 0
