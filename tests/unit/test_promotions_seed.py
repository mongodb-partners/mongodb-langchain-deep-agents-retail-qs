"""Promotions seed + coupon graph-edge + allow-list coherence."""
from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SEED_ROOT = REPO_ROOT / "examples" / "retail_assistant" / "seeds"
PROMOTIONS = SEED_ROOT / "operational" / "promotions.json"
ORDERS = SEED_ROOT / "operational" / "orders.json"
KG = SEED_ROOT / "knowledge_graph.json"


@pytest.fixture(autouse=True)
def _clear_settings() -> None:
    from deep_agent import config

    config.get_settings.cache_clear()


def _coupon_codes() -> set[str]:
    return {row["code"] for row in json.loads(PROMOTIONS.read_text())}


def test_TC_530_300_promotions_seed_shape() -> None:
    import mongomock

    from deep_agent.ingestion import seed as seeder

    db = mongomock.MongoClient()["deep_agent_test"]
    with patch("deep_agent.ingestion.seed.get_db", return_value=db):
        counts = seeder.seed_operational_data()

    assert "promotions" in counts
    rows = list(db["promotions"].find({}))
    assert len(rows) == counts["promotions"] >= 7
    for row in rows:
        assert row["type"] in {"manufacturer", "store"}
        assert isinstance(row["applies_to"], list) and row["applies_to"]
        for app in row["applies_to"]:
            assert set(app) >= {"product_id", "amount"}
            assert isinstance(app["amount"], (int, float))


def test_TC_530_301_every_order_coupon_is_defined() -> None:
    """Every coupon used in order history must have a structured definition."""
    order_coupons: set[str] = set()
    for o in json.loads(ORDERS.read_text()):
        order_coupons.update(o.get("coupons_used", []))
    missing = order_coupons - _coupon_codes()
    assert not missing, f"order coupons missing from promotions.json: {missing}"


def test_TC_530_310_all_coupons_resolvable_in_knowledge_graph() -> None:
    """kg-promotion-product edge text must name every coupon so
    knowledge_graph_search can resolve coupon→SKU coverage."""
    rows = json.loads(KG.read_text())
    edge = next(
        r for r in rows if r["metadata"]["source"] == "kg-promotion-product"
    )
    text = edge["text"]
    missing = {c for c in _coupon_codes() if c not in text}
    assert not missing, f"coupons absent from kg-promotion-product: {missing}"


def test_TC_530_310b_promotions_target_real_products() -> None:
    products = {p["product_id"] for p in json.loads(
        (SEED_ROOT / "operational" / "products.json").read_text())}
    for row in json.loads(PROMOTIONS.read_text()):
        for app in row["applies_to"]:
            assert app["product_id"] in products, (
                f"{row['code']} targets unknown product {app['product_id']}"
            )


def test_TC_530_311_allow_list_includes_promotions_not_carts() -> None:
    """promotions is NL→MQL read-allow-listed;
    carts is deliberately NOT (it is written only by dedicated tools)."""
    env = (REPO_ROOT / ".env.example").read_text()
    m = re.search(r"^DATA_AGENT_ALLOW_LIST=(.*)$", env, re.MULTILINE)
    assert m, "DATA_AGENT_ALLOW_LIST not found in .env.example"
    allow = {x.strip() for x in m.group(1).split(",") if x.strip()}
    assert "promotions" in allow
    assert "carts" not in allow
