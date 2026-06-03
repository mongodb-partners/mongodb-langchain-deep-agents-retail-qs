"""Sub-phase 03: mongo.py singleton client."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    from deep_agent import config
    from deep_agent.persistence import mongo

    config.get_settings.cache_clear()
    mongo.reset_for_tests()


def test_TC_03_010_get_client_is_singleton() -> None:
    from deep_agent.persistence import mongo

    with patch("deep_agent.persistence.mongo.MongoClient") as mc:
        mc.return_value = MagicMock()
        a = mongo.get_client()
        b = mongo.get_client()
    assert a is b
    mc.assert_called_once()
    args, _ = mc.call_args
    assert args[0].startswith("mongodb://")


def test_TC_03_015_get_client_redacts_uri_on_failure() -> None:
    from pymongo.errors import PyMongoError

    from deep_agent.persistence import mongo

    with patch("deep_agent.persistence.mongo.MongoClient") as mc:
        mc.side_effect = PyMongoError("kaboom")
        with pytest.raises(ConnectionError) as excinfo:
            mongo.get_client()

    message = str(excinfo.value)
    assert "[URI redacted]" in message
    assert "PyMongoError" in message
    assert "mongodb://" not in message
    assert "fake:27017" not in message


def test_TC_03_020_get_db_returns_configured_name() -> None:
    from deep_agent.persistence import mongo

    fake_client = MagicMock()
    with patch("deep_agent.persistence.mongo.get_client", return_value=fake_client):
        mongo.get_db()
    fake_client.__getitem__.assert_called_once_with("deep_agent_test")


def test_TC_03_025_reset_for_tests_clears_cache() -> None:
    from deep_agent.persistence import mongo

    with patch("deep_agent.persistence.mongo.MongoClient") as mc:
        mc.return_value = MagicMock()
        mongo.get_client()
        mongo.reset_for_tests()
        mongo.get_client()
    assert mc.call_count == 2


# --- MongoClient pool tuning -------------------------


def test_TC_R501_020_client_kwargs_present() -> None:
    """MongoClient is built with the safe-defaults kwargs."""
    from deep_agent.persistence import mongo

    with patch("deep_agent.persistence.mongo.MongoClient") as mc:
        mc.return_value = MagicMock()
        mongo.get_client()
    _, kwargs = mc.call_args
    assert kwargs.get("appname") == "deep-agent"
    assert kwargs.get("tz_aware") is True
    assert kwargs.get("uuidRepresentation") == "standard"
    assert kwargs.get("retryWrites") is True
    assert kwargs.get("retryReads") is True


def test_TC_R501_021_pool_kwargs_default() -> None:
    """Production-sized pool defaults."""
    from deep_agent.persistence import mongo

    with patch("deep_agent.persistence.mongo.MongoClient") as mc:
        mc.return_value = MagicMock()
        mongo.get_client()
    _, kwargs = mc.call_args
    assert kwargs.get("maxPoolSize") == 100
    assert kwargs.get("minPoolSize") == 10
    assert kwargs.get("serverSelectionTimeoutMS") == 5000
    assert kwargs.get("socketTimeoutMS") == 30000


def test_TC_R501_022_pool_kwargs_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env vars override the pool defaults."""
    from deep_agent import config
    from deep_agent.persistence import mongo

    monkeypatch.setenv("MONGODB_MAX_POOL_SIZE", "250")
    monkeypatch.setenv("MONGODB_MIN_POOL_SIZE", "25")
    monkeypatch.setenv("MONGODB_SERVER_SELECTION_TIMEOUT_MS", "8000")
    monkeypatch.setenv("MONGODB_SOCKET_TIMEOUT_MS", "60000")
    config.get_settings.cache_clear()
    mongo.reset_for_tests()

    with patch("deep_agent.persistence.mongo.MongoClient") as mc:
        mc.return_value = MagicMock()
        mongo.get_client()
    _, kwargs = mc.call_args
    assert kwargs["maxPoolSize"] == 250
    assert kwargs["minPoolSize"] == 25
    assert kwargs["serverSelectionTimeoutMS"] == 8000
    assert kwargs["socketTimeoutMS"] == 60000
