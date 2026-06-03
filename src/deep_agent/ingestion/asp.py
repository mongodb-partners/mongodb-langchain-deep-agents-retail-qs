"""Atlas Stream Processing operational helpers.

Python wrapper around the ``sp.*`` mongosh verbs. Lets tests and operator
scripts register or tear down stream processors without shelling out to
mongosh. The canonical pipeline ships as ``streaming/atlas_sp_pipeline.js``
for mongosh-driven setups.
"""
from __future__ import annotations

import logging
from typing import Any

from pymongo import MongoClient
from pymongo.errors import OperationFailure

log = logging.getLogger(__name__)

_NAMESPACE_EXISTS = 48
_NAMESPACE_NOT_FOUND = 26


def default_pipeline_spec(
    *,
    kafka_topic: str = "events",
    atlas_db: str = "deep_agent",
    atlas_coll: str = "stream_events",
) -> list[dict[str, Any]]:
    """Return the canonical ``$source → $tumblingWindow → $merge`` spec.

    Connection names (``kafka_conn``, ``atlas_conn``) must already exist in the
    Stream Processing instance's connection registry.
    """
    return [
        {
            "$source": {
                "connectionName": "kafka_conn",
                "topic": kafka_topic,
                "timeField": {"$dateFromString": {"dateString": "$ts"}},
            }
        },
        {
            "$tumblingWindow": {
                "interval": {"size": 10, "unit": "second"},
                "pipeline": [
                    {
                        "$group": {
                            "_id": "$event_type",
                            "count": {"$sum": 1},
                            "last": {"$last": "$$ROOT"},
                        }
                    }
                ],
            }
        },
        {
            "$merge": {
                "into": {
                    "connectionName": "atlas_conn",
                    "db": atlas_db,
                    "coll": atlas_coll,
                },
                "on": "_id",
                "whenMatched": "merge",
                "whenNotMatched": "insert",
            }
        },
    ]


def register_pipeline(
    sp_uri: str,
    *,
    pipeline_name: str,
    pipeline_spec: list[dict[str, Any]],
) -> None:
    """Create and start a stream processor on the ASP instance at ``sp_uri``.

    Idempotent: ``NamespaceExists`` (48) on create is swallowed so
    re-registering the same processor is a no-op.
    """
    client: MongoClient[Any] = MongoClient(sp_uri)
    try:
        try:
            client.admin.command(
                {"createStreamProcessor": pipeline_name, "pipeline": pipeline_spec}
            )
        except OperationFailure as exc:
            if exc.code != _NAMESPACE_EXISTS:
                raise
            log.info("stream processor %s already exists; skipping create", pipeline_name)
        client.admin.command({"startStreamProcessor": pipeline_name})
    finally:
        client.close()


def stop_pipeline(sp_uri: str, pipeline_name: str) -> None:
    """Stop and drop a stream processor. Missing processors are a no-op."""
    client: MongoClient[Any] = MongoClient(sp_uri)
    try:
        for cmd in (
            {"stopStreamProcessor": pipeline_name},
            {"dropStreamProcessor": pipeline_name},
        ):
            try:
                client.admin.command(cmd)
            except OperationFailure as exc:
                if exc.code != _NAMESPACE_NOT_FOUND:
                    raise
                log.info("stream processor %s already absent; skipping", pipeline_name)
    finally:
        client.close()
