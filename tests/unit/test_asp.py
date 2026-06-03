"""Sub-phase 14: ASP helpers."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from pymongo.errors import OperationFailure


def test_TC_14_040_default_pipeline_spec_shape() -> None:
    from deep_agent.ingestion import asp

    spec = asp.default_pipeline_spec(
        kafka_topic="events", atlas_db="deep_agent", atlas_coll="stream_events"
    )
    assert len(spec) == 3
    assert "$source" in spec[0]
    assert "$tumblingWindow" in spec[1]
    assert "$merge" in spec[2]
    assert spec[0]["$source"]["topic"] == "events"
    assert spec[2]["$merge"]["into"]["coll"] == "stream_events"


def test_TC_14_041_register_pipeline_swallows_namespace_exists() -> None:
    from deep_agent.ingestion import asp

    fake_client = MagicMock()
    # First call raises NamespaceExists (48); subsequent command call for start succeeds.
    fake_client.admin.command.side_effect = [
        OperationFailure("exists", code=48),
        {"ok": 1},
    ]
    with patch("deep_agent.ingestion.asp.MongoClient", return_value=fake_client):
        asp.register_pipeline(
            "mongodb://sp-instance/",
            pipeline_name="agentic_test",
            pipeline_spec=asp.default_pipeline_spec(),
        )
    # Two admin.command calls made: create (raises 48), start (ok).
    assert fake_client.admin.command.call_count == 2
    fake_client.close.assert_called_once()


def test_TC_14_042_stop_pipeline_swallows_namespace_not_found() -> None:
    from deep_agent.ingestion import asp

    fake_client = MagicMock()
    fake_client.admin.command.side_effect = [
        OperationFailure("gone", code=26),
        OperationFailure("gone", code=26),
    ]
    with patch("deep_agent.ingestion.asp.MongoClient", return_value=fake_client):
        asp.stop_pipeline("mongodb://sp-instance/", "agentic_test")
    # stop + drop both attempted; both gracefully swallowed
    assert fake_client.admin.command.call_count == 2
    fake_client.close.assert_called_once()
