"""current_shopper identity tool."""
from __future__ import annotations

from typing import Any

import pytest


def _db_with_customer() -> Any:
    import mongomock

    db = mongomock.MongoClient()["t"]
    db["customers"].insert_one(
        {
            "customer_id": "cust_R001",
            "name": "Maria Gonzalez",
            "loyalty_tier": "Gold",
            "loyalty_points": 4200,
            "dietary_preferences": ["vegetarian"],
            "household_size": 4,
        }
    )
    return db


def test_TC_530_402_current_shopper_returns_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deep_agent.tools import profile

    monkeypatch.setattr("deep_agent.tools.profile.get_db", _db_with_customer)
    monkeypatch.setattr(
        "deep_agent.tools.profile.get_config",
        lambda: {"configurable": {"user_id": "cust_R001"}},
    )
    out = profile.current_shopper.invoke({})
    assert "cust_R001" in out
    assert "Gold" in out
    assert "4200" in out
    assert "vegetarian" in out


def test_TC_530_403_current_shopper_no_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deep_agent.tools import profile

    monkeypatch.setattr("deep_agent.tools.profile.get_db", _db_with_customer)
    monkeypatch.setattr(
        "deep_agent.tools.profile.get_config", lambda: {"configurable": {}}
    )
    assert "unavailable" in profile.current_shopper.invoke({})


def test_TC_530_404_current_shopper_guest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown user_id → a guest sentinel that still names the id for scoping."""
    from deep_agent.tools import profile

    monkeypatch.setattr("deep_agent.tools.profile.get_db", _db_with_customer)
    monkeypatch.setattr(
        "deep_agent.tools.profile.get_config",
        lambda: {"configurable": {"user_id": "stranger"}},
    )
    out = profile.current_shopper.invoke({})
    assert "stranger" in out
    assert "guest" in out.lower()
