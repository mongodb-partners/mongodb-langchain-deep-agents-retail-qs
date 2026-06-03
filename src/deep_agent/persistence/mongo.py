"""Singleton MongoDB client + database handle."""
from __future__ import annotations

from typing import Any

from pymongo import MongoClient
from pymongo.errors import ConfigurationError, PyMongoError

from ..config import get_settings

_singleton_client: MongoClient[dict[str, Any]] | None = None


def get_client() -> MongoClient[dict[str, Any]]:
    """Return a process-wide singleton :class:`pymongo.MongoClient`.

    On first call, connects using ``MONGODB_URI``. Subsequent calls return the
    cached instance. Connection failures raise :class:`ConnectionError` with the
    URI redacted.

    The constructor receives Atlas-driver
    best-practice kwargs (``appname`` for Profiler, ``tz_aware`` so timestamps
    round-trip, ``uuidRepresentation="standard"`` for forward-compat, explicit
    retry flags) plus production-sized pool defaults that are env-overridable
    (``MONGODB_MAX_POOL_SIZE`` etc.). Pool sizes target a single backend pod
    serving ~10-50 in-flight SSE requests.

    ``connectTimeoutMS`` is intentionally not overridden — the PyMongo
    default (20s) bounds initial TCP+TLS handshake against Atlas, which
    is well within the ``serverSelectionTimeoutMS`` envelope and matches
    the Atlas operator playbook. Override at the URI level if a network
    path needs tighter bounds.
    """
    global _singleton_client
    if _singleton_client is not None:
        return _singleton_client
    s = get_settings()
    try:
        _singleton_client = MongoClient(
            s.mongodb_uri.get_secret_value(),
            appname="deep-agent",
            tz_aware=True,
            uuidRepresentation="standard",
            retryWrites=True,
            retryReads=True,
            maxPoolSize=s.mongodb_max_pool_size,
            minPoolSize=s.mongodb_min_pool_size,
            serverSelectionTimeoutMS=s.mongodb_server_selection_timeout_ms,
            socketTimeoutMS=s.mongodb_socket_timeout_ms,
        )
    except (PyMongoError, ConfigurationError, ValueError) as exc:
        raise ConnectionError(
            f"Failed to connect to MongoDB: {type(exc).__name__}: [URI redacted]"
        ) from None
    return _singleton_client


def get_db() -> Any:
    """Return the :class:`pymongo.database.Database` for ``Settings.mongodb_db``.

    The ``db_name`` parameter is gone;
    every caller binds to the configured database.
    """
    return get_client()[get_settings().mongodb_db]


def reset_for_tests() -> None:
    """Clear the singleton. Intended for test fixtures only."""
    global _singleton_client
    _singleton_client = None
