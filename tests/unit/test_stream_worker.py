"""Change-stream worker tests."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clear_settings() -> None:
    from deep_agent import config

    config.get_settings.cache_clear()


class _FakeWatchContext:
    """Context manager simulating ``coll.watch()`` returning an iterable of events."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    def __enter__(self) -> list[dict[str, Any]]:
        return self._events

    def __exit__(self, *exc: Any) -> None:
        return None


def _build_worker_db(events: list[dict[str, Any]]):  # type: ignore[no-untyped-def]
    """Build a MagicMock db whose ``db[stream_events].watch(**kw)`` returns
    the scripted events."""
    coll = MagicMock()
    coll.watch.return_value = _FakeWatchContext(events)
    db = MagicMock()
    db.__getitem__.return_value = coll
    return db, coll


def test_TC_14_030_run_once_upserts_kb_with_metadata_minus_text(
    tmp_path, monkeypatch: pytest.MonkeyPatch  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setenv("DEEP_AGENT_STATE_DIR", str(tmp_path))
    from deep_agent.ingestion import stream_worker as sw

    events = [
        {
            "operationType": "insert",
            "_id": {"tok": 1},
            "fullDocument": {
                "text": "new finding alpha",
                "source": "events",
                "ts": 1.0,
            },
        }
    ]
    db, _ = _build_worker_db(events)
    vs = MagicMock()
    with patch("deep_agent.ingestion.stream_worker.get_db", return_value=db), patch(
        "deep_agent.ingestion.stream_worker.build_vector_store", return_value=vs
    ):
        n = sw.run_once(max_events=1)

    assert n == 1
    _, kwargs = vs.add_texts.call_args
    texts = kwargs.get("texts") or vs.add_texts.call_args.args[0]
    metas = kwargs.get("metadatas") or vs.add_texts.call_args.args[1]
    assert texts == ["new finding alpha"]
    # `text` key stripped from metadata; other keys remain
    assert metas == [{"source": "events", "ts": 1.0}]


def test_TC_14_033_run_once_reads_text_from_windowed_last_payload(
    tmp_path, monkeypatch: pytest.MonkeyPatch  # type: ignore[no-untyped-def]
) -> None:
    """The shipped ASP $group emits {_id, count, last} merged
    on _id, so the payload lives at ``last.text`` — not a flat top-level
    ``text``. The worker must read it there or the KB is never fed."""
    monkeypatch.setenv("DEEP_AGENT_STATE_DIR", str(tmp_path))
    from deep_agent.ingestion import stream_worker as sw

    events = [
        {
            "operationType": "insert",
            "_id": {"tok": 1},
            "fullDocument": {
                "_id": "finding",
                "count": 3,
                "last": {
                    "text": "windowed finding alpha",
                    "event_type": "finding",
                    "source": "events",
                    "ts": 9.0,
                },
            },
        }
    ]
    db, _ = _build_worker_db(events)
    vs = MagicMock()
    with patch("deep_agent.ingestion.stream_worker.get_db", return_value=db), patch(
        "deep_agent.ingestion.stream_worker.build_vector_store", return_value=vs
    ):
        n = sw.run_once(max_events=1)

    assert n == 1
    _, kwargs = vs.add_texts.call_args
    texts = kwargs.get("texts") or vs.add_texts.call_args.args[0]
    metas = kwargs.get("metadatas") or vs.add_texts.call_args.args[1]
    assert texts == ["windowed finding alpha"]
    # metadata comes from the windowed payload, text stripped
    assert "text" not in metas[0]
    assert metas[0].get("source") == "events"
    assert metas[0].get("event_type") == "finding"


def test_TC_14_031_run_once_skips_events_without_text(
    tmp_path, monkeypatch: pytest.MonkeyPatch  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setenv("DEEP_AGENT_STATE_DIR", str(tmp_path))
    from deep_agent.ingestion import stream_worker as sw

    events = [
        {"operationType": "insert", "_id": {"tok": 1}, "fullDocument": {"source": "nope"}},
        {"operationType": "update", "_id": {"tok": 2}, "fullDocument": None},
        {"operationType": "delete", "_id": {"tok": 3}, "fullDocument": {"text": "ignored"}},
    ]
    db, _ = _build_worker_db(events)
    vs = MagicMock()
    with patch("deep_agent.ingestion.stream_worker.get_db", return_value=db), patch(
        "deep_agent.ingestion.stream_worker.build_vector_store", return_value=vs
    ):
        n = sw.run_once()

    assert n == 0
    vs.add_texts.assert_not_called()


def test_TC_14_032_resume_token_written_to_state_dir(
    tmp_path, monkeypatch: pytest.MonkeyPatch  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setenv("DEEP_AGENT_STATE_DIR", str(tmp_path))
    from deep_agent.ingestion import stream_worker as sw

    token_path = sw.resume_token_path()
    assert str(token_path).startswith(str(tmp_path))

    events = [
        {
            "operationType": "insert",
            "_id": {"tok": "resume-42"},
            "fullDocument": {"text": "x", "source": "s"},
        }
    ]
    db, _ = _build_worker_db(events)
    vs = MagicMock()
    with patch("deep_agent.ingestion.stream_worker.get_db", return_value=db), patch(
        "deep_agent.ingestion.stream_worker.build_vector_store", return_value=vs
    ):
        sw.run_once(max_events=1)

    assert token_path.exists()
    import json

    saved = json.loads(token_path.read_text())
    assert saved == {"tok": "resume-42"}


def test_TC_14_050_run_with_backoff_retries_on_pymongo_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch  # type: ignore[no-untyped-def]
) -> None:
    from pymongo.errors import PyMongoError

    monkeypatch.setenv("DEEP_AGENT_STATE_DIR", str(tmp_path))
    from deep_agent.ingestion import stream_worker as sw

    calls = {"n": 0}

    def _scripted(**_kwargs: Any) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            raise PyMongoError("down")
        return 0

    sleeps: list[float] = []
    with patch("deep_agent.ingestion.stream_worker.run_once", side_effect=_scripted), patch(
        "deep_agent.ingestion.stream_worker.time.sleep", side_effect=sleeps.append
    ):
        sw.run_with_backoff(max_attempts=2, base_delay=1.0, cap=60.0)

    assert calls["n"] == 2
    # First failure slept once with base_delay.
    assert sleeps and sleeps[0] == 1.0
