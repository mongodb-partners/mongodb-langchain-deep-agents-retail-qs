"""Server tests: lifespan, /chat, /plans, /feedback, probes."""
from __future__ import annotations

from contextlib import ExitStack
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clear_settings() -> None:
    from deep_agent import config

    config.get_settings.cache_clear()


@pytest.fixture
def test_client():  # type: ignore[no-untyped-def]
    """TestClient with a mocked compiled graph + ensure_indexes."""
    from fastapi.testclient import TestClient

    fake_graph = MagicMock()

    async def _astream_events(*_args: Any, **_kwargs: Any):  # type: ignore[no-untyped-def]
        yield {"event": "on_chat_model_stream", "data": {"chunk": MagicMock(content="Hello")}}
        yield {"event": "on_chat_model_stream", "data": {"chunk": MagicMock(content=" world")}}

    fake_graph.astream_events = _astream_events
    fake_graph.get_state.return_value = MagicMock(values={"todos": []})

    with ExitStack() as stack:
        stack.enter_context(patch("deep_agent.server.app.build_graph", return_value=fake_graph))
        stack.enter_context(patch("deep_agent.server.app.ensure_indexes"))
        from deep_agent.server import app as appmod

        # Clear lifespan state in case a previous test ran the shutdown phase.
        appmod._GRAPH = None
        appmod._SHUTDOWN_EVENT = appmod.asyncio.Event()
        appmod._IN_FLIGHT_STREAMS = set()
        appmod._READINESS_CACHE.update(ok=False, checked_at=0.0, error=None)

        app = appmod.create_app()
        # Force readiness cache to "ok" for tests that don't need to check Mongo.
        with patch("deep_agent.server.app.get_client") as gc:
            gc.return_value.admin.command.return_value = {"ok": 1}
            with TestClient(app) as client:
                yield client, fake_graph


def test_TC_13_020_chat_sse_streams_tokens(test_client) -> None:  # type: ignore[no-untyped-def]
    client, _ = test_client
    with client.stream(
        "POST", "/chat", json={"user_id": "u1", "thread_id": "t1", "message": "hi"}
    ) as r:
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
        body = b"".join(r.iter_bytes()).decode()
    assert "data:" in body
    assert "Hello" in body and "world" in body
    assert "[DONE]" in body


def test_TC_13_021_chat_requires_user_id(test_client) -> None:  # type: ignore[no-untyped-def]
    client, _ = test_client
    r = client.post("/chat", json={"message": "hi"})
    assert r.status_code == 422


def test_TC_530_120_cart_route_returns_lines_and_subtotal(test_client) -> None:  # type: ignore[no-untyped-def]
    """GET /cart composes the f"{user_id}:{sub}" key, returns lines +
    sale-priced subtotal + savings, and 200/empty when absent."""
    import mongomock

    client, _ = test_client
    db = mongomock.MongoClient()["t"]
    db["carts"].insert_one(
        {
            # Natural key (user_id, thread_id); MongoDB owns the ObjectId _id.
            "user_id": "alice",
            "thread_id": "t1",
            "lines": [
                {"product_id": "p-3001", "name": "Barilla Spaghetti", "qty": 1,
                 "unit_price_usd": 1.49, "sale_price_usd": None},
                {"product_id": "p-3002", "name": "Rao's Marinara", "qty": 2,
                 "unit_price_usd": 7.99, "sale_price_usd": 5.99},
            ],
            "updated_at": "2026-06-02T00:00:00+00:00",
        }
    )
    with patch("deep_agent.server.app.get_db", return_value=db):
        r = client.get("/cart", params={"user_id": "alice", "thread_id": "t1"})
        empty = client.get("/cart", params={"user_id": "bob", "thread_id": "t9"})

    assert r.status_code == 200
    data = r.json()
    assert len(data["lines"]) == 2
    assert data["subtotal"] == 13.47  # 1.49 + 2*5.99
    assert data["total_savings"] == 4.0  # (7.99-5.99)*2

    assert empty.status_code == 200
    assert empty.json()["lines"] == []


def test_TC_13_022_chat_default_thread_id(test_client) -> None:  # type: ignore[no-untyped-def]
    """thread_id defaults to '{user_id}:default' (no domain segment)."""
    client, fake_graph = test_client
    captured_configs: list[Any] = []

    async def _capture(_state: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        captured_configs.append(kwargs.get("config"))
        yield {"event": "on_chat_model_stream", "data": {"chunk": MagicMock(content="ok")}}

    fake_graph.astream_events = _capture
    with client.stream("POST", "/chat", json={"user_id": "u1", "message": "hi"}) as r:
        b"".join(r.iter_bytes())
    assert captured_configs
    assert captured_configs[0]["configurable"]["thread_id"] == "u1:default"


def test_TC_13_023_chat_swallow_history_errors(test_client) -> None:  # type: ignore[no-untyped-def]
    """PyMongoError during plan persist must not 5xx /chat."""
    client, fake_graph = test_client

    async def _stream(*_args: Any, **_kwargs: Any):  # type: ignore[no-untyped-def]
        yield {"event": "on_chat_model_stream", "data": {"chunk": MagicMock(content="ok")}}

    fake_graph.astream_events = _stream
    fake_graph.get_state.side_effect = RuntimeError("plan-snapshot down")

    with client.stream(
        "POST", "/chat", json={"user_id": "u1", "thread_id": "t1", "message": "hi"}
    ) as r:
        body = b"".join(r.iter_bytes()).decode()
    assert "ok" in body and "[DONE]" in body


def test_TC_13_024_chat_surfaces_stream_errors(test_client) -> None:  # type: ignore[no-untyped-def]
    client, fake_graph = test_client

    async def _boom(*_args: Any, **_kwargs: Any):  # type: ignore[no-untyped-def]
        if False:  # pragma: no cover
            yield {}
        raise RuntimeError("graph exploded")

    fake_graph.astream_events = _boom
    with client.stream(
        "POST", "/chat", json={"user_id": "u1", "thread_id": "t1", "message": "hi"}
    ) as r:
        body = b"".join(r.iter_bytes()).decode()
    assert "event: error" in body
    # The error frame is now an OPAQUE code with the correlation id, NOT the
    # raw exception. Previously the raw 'RuntimeError' leaked into the body.
    # Now: assert the type/message do NOT leak and the opaque code + cid are
    # present so operators can still correlate.
    assert "internal_error" in body
    assert "RuntimeError" not in body
    assert "graph exploded" not in body


# ── query-keyed response cache integration in /chat ───────────────


def _fresh_state(messages: list[Any] | None = None) -> Any:
    return MagicMock(values={"messages": messages or []})


def _arm_cache(fake_graph: Any, *, prior_messages: list[Any] | None = None):  # type: ignore[no-untyped-def]
    """Make the fake graph cache-capable: async aget_state/aupdate_state, and a
    sentinel astream_events that fails if the agent is (wrongly) driven."""
    from unittest.mock import AsyncMock

    fake_graph.aget_state = AsyncMock(return_value=_fresh_state(prior_messages))
    fake_graph.aupdate_state = AsyncMock()


def test_TC_520_204_cache_hit_streams_stored_text_and_skips_graph(test_client) -> None:  # type: ignore[no-untyped-def]
    """A fresh-conversation hit streams the stored response and
    does NOT drive the agent graph."""
    client, fake_graph = test_client
    _arm_cache(fake_graph)
    fake_graph.astream_events = MagicMock(
        side_effect=AssertionError("graph must not run on a cache hit")
    )
    cache = MagicMock()
    cache.lookup.return_value = "CACHED ANSWER"

    with patch("deep_agent.server.app.build_response_cache", return_value=cache), client.stream(
        "POST", "/chat", json={"user_id": "u1", "thread_id": "t1", "message": "hi"}
    ) as r:
        body = b"".join(r.iter_bytes()).decode()

    assert "CACHED ANSWER" in body
    assert "[DONE]" in body
    fake_graph.astream_events.assert_not_called()
    cache.lookup.assert_called_once()


def test_TC_520_205_cache_hit_persists_turn_to_checkpoint(test_client) -> None:  # type: ignore[no-untyped-def]
    """A hit persists (human, ai) to the thread checkpoint so a
    follow-up runs the agent with coherent history."""
    client, fake_graph = test_client
    _arm_cache(fake_graph)
    cache = MagicMock()
    cache.lookup.return_value = "CACHED ANSWER"

    with patch("deep_agent.server.app.build_response_cache", return_value=cache), client.stream(
        "POST", "/chat", json={"user_id": "u1", "thread_id": "t1", "message": "hello there"}
    ) as r:
        b"".join(r.iter_bytes())

    fake_graph.aupdate_state.assert_awaited_once()
    (_cfg, update), _ = fake_graph.aupdate_state.call_args
    msgs = update["messages"]
    assert msgs[0].content == "hello there"  # HumanMessage
    assert msgs[1].content == "CACHED ANSWER"  # AIMessage


def test_TC_520_206_no_store_when_turn_errors(test_client) -> None:  # type: ignore[no-untyped-def]
    """A turn that errors mid-stream must NOT write a cache entry."""
    client, fake_graph = test_client
    _arm_cache(fake_graph)
    cache = MagicMock()
    cache.lookup.return_value = None  # miss → agent runs

    async def _boom(*_args: Any, **_kwargs: Any):  # type: ignore[no-untyped-def]
        if False:  # pragma: no cover
            yield {}
        raise RuntimeError("graph exploded")

    fake_graph.astream_events = _boom
    with patch("deep_agent.server.app.build_response_cache", return_value=cache), client.stream(
        "POST", "/chat", json={"user_id": "u1", "thread_id": "t1", "message": "hi"}
    ) as r:
        body = b"".join(r.iter_bytes()).decode()

    assert "internal_error" in body
    cache.save.assert_not_called()


def test_TC_520_207_non_fresh_thread_bypasses_cache(test_client) -> None:  # type: ignore[no-untyped-def]
    """A thread with prior messages bypasses the cache entirely."""
    client, fake_graph = test_client
    _arm_cache(fake_graph, prior_messages=[MagicMock()])  # not fresh
    cache = MagicMock()

    with patch("deep_agent.server.app.build_response_cache", return_value=cache), client.stream(
        "POST", "/chat", json={"user_id": "u1", "thread_id": "t1", "message": "hi"}
    ) as r:
        body = b"".join(r.iter_bytes()).decode()

    # Agent ran normally (fixture streams "Hello world"); cache untouched.
    assert "Hello" in body and "world" in body
    cache.lookup.assert_not_called()
    cache.save.assert_not_called()


def test_TC_520_208_disabled_cache_runs_agent(test_client) -> None:  # type: ignore[no-untyped-def]
    """build_response_cache None (disabled) → no lookup/store, the
    agent streams as today."""
    client, fake_graph = test_client
    _arm_cache(fake_graph)
    with patch("deep_agent.server.app.build_response_cache", return_value=None), client.stream(
        "POST", "/chat", json={"user_id": "u1", "thread_id": "t1", "message": "hi"}
    ) as r:
        body = b"".join(r.iter_bytes()).decode()
    assert "Hello" in body and "world" in body
    fake_graph.aget_state.assert_not_called()


def test_TC_520_209_fresh_miss_saves_response(test_client) -> None:  # type: ignore[no-untyped-def]
    """A fresh-conversation miss that completes saves the streamed
    answer keyed by (query, user_id, model)."""
    client, fake_graph = test_client
    _arm_cache(fake_graph)
    cache = MagicMock()
    cache.lookup.return_value = None  # miss

    with patch("deep_agent.server.app.build_response_cache", return_value=cache), client.stream(
        "POST", "/chat", json={"user_id": "u1", "thread_id": "t1", "message": "a question"}
    ) as r:
        b"".join(r.iter_bytes())

    cache.save.assert_called_once()
    args, _ = cache.save.call_args
    assert args[0] == "a question"  # query
    assert args[1] == "u1"  # user_id
    assert args[3] == "Hello world"  # accumulated streamed answer


def test_TC_530_530_mutating_turn_not_cached(test_client) -> None:  # type: ignore[no-untyped-def]
    """A fresh turn that invoked a side-effecting tool (add_to_cart,
    place_order, …) must NOT be response-cached — a replay would skip the
    mutation. Cache the answer text only for read-only turns."""
    client, fake_graph = test_client
    _arm_cache(fake_graph)
    cache = MagicMock()
    cache.lookup.return_value = None  # fresh miss → agent runs

    async def _stream(*_a: Any, **_k: Any):  # type: ignore[no-untyped-def]
        yield {"event": "on_tool_start", "name": "add_to_cart", "data": {}}
        yield {"event": "on_chat_model_stream",
               "data": {"chunk": MagicMock(content="added to cart")}}

    fake_graph.astream_events = _stream
    with patch("deep_agent.server.app.build_response_cache", return_value=cache), client.stream(
        "POST", "/chat",
        json={"user_id": "u1", "thread_id": "t1", "message": "add spaghetti to my cart"},
    ) as r:
        body = b"".join(r.iter_bytes()).decode()

    assert "added to cart" in body
    cache.save.assert_not_called()


def test_TC_13_030_feedback_inserts(test_client) -> None:  # type: ignore[no-untyped-def]
    client, _ = test_client
    with patch("deep_agent.server.app.get_db") as gdb:
        coll = MagicMock()
        db = MagicMock()
        db.__getitem__.return_value = coll
        gdb.return_value = db
        r = client.post(
            "/feedback",
            json={"run_id": "r1", "score": 1.0, "comment": "nice", "user_id": "u1"},
        )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    coll.insert_one.assert_called_once()


def test_TC_13_031_feedback_langsmith_mirror(
    monkeypatch: pytest.MonkeyPatch, test_client  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    from deep_agent import config

    config.get_settings.cache_clear()

    client, _ = test_client
    with patch("deep_agent.server.app.get_db") as gdb, patch("langsmith.Client") as lc:
        coll = MagicMock()
        db = MagicMock()
        db.__getitem__.return_value = coll
        gdb.return_value = db
        lc.return_value.create_feedback = MagicMock()
        r = client.post(
            "/feedback",
            json={"run_id": "r1", "score": 0.5, "comment": "c", "user_id": "u1"},
        )
    assert r.status_code == 200
    lc.return_value.create_feedback.assert_called_once()


def test_TC_13_032_feedback_mirror_failure_swallowed(
    monkeypatch: pytest.MonkeyPatch, test_client  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    from deep_agent import config

    config.get_settings.cache_clear()

    client, _ = test_client
    with patch("deep_agent.server.app.get_db") as gdb, patch("langsmith.Client") as lc:
        coll = MagicMock()
        db = MagicMock()
        db.__getitem__.return_value = coll
        gdb.return_value = db
        lc.side_effect = RuntimeError("ls down")
        r = client.post(
            "/feedback",
            json={"run_id": "r1", "score": 0.5, "comment": None, "user_id": "u1"},
        )
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_TC_13_033_feedback_persistence_error(test_client) -> None:  # type: ignore[no-untyped-def]
    from pymongo.errors import PyMongoError

    client, _ = test_client
    with patch("deep_agent.server.app.get_db") as gdb:
        coll = MagicMock()
        coll.insert_one.side_effect = PyMongoError("down")
        db = MagicMock()
        db.__getitem__.return_value = coll
        gdb.return_value = db
        r = client.post(
            "/feedback",
            json={"run_id": "r1", "score": 0.0, "comment": None, "user_id": "u1"},
        )
    assert r.status_code == 500


def test_TC_13_040_health_reports_ok(test_client) -> None:  # type: ignore[no-untyped-def]
    client, _ = test_client
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["mongo"] == "ok"


def test_TC_13_041_health_surfaces_pymongo_error(test_client) -> None:  # type: ignore[no-untyped-def]
    from pymongo.errors import PyMongoError

    client, _ = test_client
    with patch("deep_agent.server.app.get_client") as gc:
        gc.return_value.admin.command.side_effect = PyMongoError("unreachable")
        # Force-expire the readiness cache so the new ping is attempted.
        from deep_agent.server import app as appmod
        appmod._READINESS_CACHE["checked_at"] = 0.0
        r = client.get("/health")
    assert r.status_code == 200
    assert "error" in r.json()["mongo"]


# --- operational primitives ---------------------------------


def test_TC_R501_180_live_no_io(test_client) -> None:  # type: ignore[no-untyped-def]
    """/live never touches dependencies."""
    client, _ = test_client
    with patch("deep_agent.server.app.get_client") as gc:
        gc.side_effect = AssertionError("get_client must NOT be called")
        r = client.get("/live")
    assert r.status_code == 200
    assert r.json() == {"status": "live"}


def test_TC_R501_181_ready_returns_200_after_lifespan(test_client) -> None:  # type: ignore[no-untyped-def]
    """/ready 200 once lifespan + Mongo ping succeed."""
    client, _ = test_client
    r = client.get("/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["checks"]["mongo"] == "ok"


def test_TC_R501_182_ready_returns_503_when_mongo_down(test_client) -> None:  # type: ignore[no-untyped-def]
    """/ready 503 when the cached Mongo ping failed."""
    client, _ = test_client
    from deep_agent.server import app as appmod
    appmod._READINESS_CACHE["checked_at"] = 0.0
    with patch("deep_agent.server.app.get_client") as gc:
        gc.return_value.admin.command.side_effect = RuntimeError("down")
        r = client.get("/ready")
    assert r.status_code == 503
    assert r.json()["status"] == "not_ready"


def test_TC_R501_181_ready_returns_503_during_startup(test_client) -> None:  # type: ignore[no-untyped-def]
    """/ready 503 when _GRAPH is None (pre-lifespan startup)."""
    client, _ = test_client
    from deep_agent.server import app as appmod

    saved = appmod._GRAPH
    appmod._GRAPH = None
    try:
        r = client.get("/ready")
    finally:
        appmod._GRAPH = saved
    assert r.status_code == 503
    assert r.json()["status"] == "starting"


def test_TC_R501_170_correlation_id_from_header(test_client) -> None:  # type: ignore[no-untyped-def]
    """A valid X-Correlation-Id header is echoed."""
    import uuid

    client, _ = test_client
    cid = str(uuid.uuid4())
    r = client.get("/live", headers={"X-Correlation-Id": cid})
    assert r.headers["X-Correlation-Id"] == cid


def test_TC_R501_170_correlation_id_invalid_regenerated(test_client) -> None:  # type: ignore[no-untyped-def]
    """Invalid X-Correlation-Id → server generates a UUID v4."""
    import uuid

    client, _ = test_client
    r = client.get("/live", headers={"X-Correlation-Id": "not-a-uuid"})
    out = r.headers["X-Correlation-Id"]
    uuid.UUID(out)  # raises if not a UUID


def test_TC_R501_170_correlation_id_missing_generated(test_client) -> None:  # type: ignore[no-untyped-def]
    """No X-Correlation-Id → server generates one."""
    import uuid

    client, _ = test_client
    r = client.get("/live")
    out = r.headers["X-Correlation-Id"]
    uuid.UUID(out)


def test_TC_R501_172_chat_emits_correlation_frame(test_client) -> None:  # type: ignore[no-untyped-def]
    """/chat emits a leading correlation frame."""
    client, _ = test_client
    with client.stream("POST", "/chat", json={"user_id": "u1", "message": "hi"}) as r:
        body = b"".join(r.iter_bytes()).decode()
    assert "event: correlation" in body


def test_TC_R501_173_correlation_in_runnable_config(test_client) -> None:  # type: ignore[no-untyped-def]
    """correlation_id reaches the RunnableConfig.configurable."""
    client, fake_graph = test_client
    captured_configs: list[Any] = []

    async def _capture(_state: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        captured_configs.append(kwargs.get("config"))
        yield {"event": "on_chat_model_stream", "data": {"chunk": MagicMock(content="ok")}}

    fake_graph.astream_events = _capture
    with client.stream("POST", "/chat", json={"user_id": "u1", "message": "hi"}) as r:
        cid = r.headers["X-Correlation-Id"]
        b"".join(r.iter_bytes())
    assert captured_configs[0]["configurable"]["correlation_id"] == cid


def test_TC_R501_203_recursion_limit_passed_to_runnable_config(test_client) -> None:  # type: ignore[no-untyped-def]
    """recursion_limit comes from Settings.recursion_limit (default 50)."""
    client, fake_graph = test_client
    captured: list[Any] = []

    async def _capture(_state: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        captured.append(kwargs.get("config"))
        yield {"event": "on_chat_model_stream", "data": {"chunk": MagicMock(content="ok")}}

    fake_graph.astream_events = _capture
    with client.stream("POST", "/chat", json={"user_id": "u1", "message": "hi"}) as r:
        b"".join(r.iter_bytes())
    assert captured[0]["recursion_limit"] == 50


# --- /messages + /plans from mirror ---------------


def test_TC_R501_060_plans_returns_latest_from_mirror(test_client) -> None:  # type: ignore[no-untyped-def]
    """/plans returns the latest mirror doc's todos."""
    from datetime import UTC
    from datetime import datetime as _dt

    client, _ = test_client
    fake_coll = MagicMock()
    fake_coll.find.return_value.__iter__ = lambda self: iter([{
        "thread_id": "t1",
        "user_id": "u1",
        "step": 5,
        "ts": _dt.now(UTC),
        "todos": [
            {"id": "1", "content": "Done", "status": "completed"},
            {"id": "2", "content": "Doing", "status": "in_progress"},
        ],
    }])
    fake_db = MagicMock()
    fake_db.__getitem__.return_value = fake_coll
    with patch("deep_agent.server.app.get_db", return_value=fake_db):
        r = client.get("/plans?user_id=u1&thread_id=t1")
    assert r.status_code == 200
    body = r.json()
    assert body["todos"] == [
        {"id": "1", "text": "Done", "status": "completed"},
        {"id": "2", "text": "Doing", "status": "in_progress"},
    ]
    assert body["updated_at"] is not None


def test_TC_R501_060_plans_empty_when_no_mirror_doc(test_client) -> None:  # type: ignore[no-untyped-def]
    """Missing thread → 200 with empty todos + None updated_at."""
    client, _ = test_client
    fake_coll = MagicMock()
    fake_coll.find.return_value.__iter__ = lambda self: iter([])
    fake_db = MagicMock()
    fake_db.__getitem__.return_value = fake_coll
    with patch("deep_agent.server.app.get_db", return_value=fake_db):
        r = client.get("/plans?user_id=u1&thread_id=missing")
    assert r.status_code == 200
    assert r.json() == {"todos": [], "updated_at": None}


def test_TC_R501_062_messages_returns_message_list(test_client) -> None:  # type: ignore[no-untyped-def]
    """/messages returns the latest mirror doc's messages."""
    from datetime import UTC
    from datetime import datetime as _dt

    client, _ = test_client
    fake_coll = MagicMock()
    fake_coll.find.return_value.__iter__ = lambda self: iter([{
        "thread_id": "t1",
        "user_id": "u1",
        "step": 7,
        "ts": _dt.now(UTC),
        "messages": [
            {"type": "human", "content": "hi"},
            {"type": "ai", "content": "hello"},
        ],
    }])
    fake_db = MagicMock()
    fake_db.__getitem__.return_value = fake_coll
    with patch("deep_agent.server.app.get_db", return_value=fake_db):
        r = client.get("/messages?user_id=u1&thread_id=t1")
    assert r.status_code == 200
    assert r.json() == {
        "messages": [
            {"type": "human", "content": "hi"},
            {"type": "ai", "content": "hello"},
        ]
    }


def test_TC_R501_064_messages_empty_when_no_mirror_doc(test_client) -> None:  # type: ignore[no-untyped-def]
    """Missing thread → 200 with empty messages list."""
    client, _ = test_client
    fake_coll = MagicMock()
    fake_coll.find.return_value.__iter__ = lambda self: iter([])
    fake_db = MagicMock()
    fake_db.__getitem__.return_value = fake_coll
    with patch("deep_agent.server.app.get_db", return_value=fake_db):
        r = client.get("/messages?user_id=u1&thread_id=missing")
    assert r.status_code == 200
    assert r.json() == {"messages": []}


def test_threads_latest_returns_sub(test_client) -> None:  # type: ignore[no-untyped-def]
    """/threads/latest strips the ``{user_id}:`` prefix off the newest doc's
    composite thread_id and returns the per-conversation sub."""
    client, _ = test_client
    fake_coll = MagicMock()
    fake_coll.find.return_value.__iter__ = lambda self: iter(
        [{"thread_id": "u1:abc123"}]
    )
    fake_db = MagicMock()
    fake_db.__getitem__.return_value = fake_coll
    with patch("deep_agent.server.app.get_db", return_value=fake_db):
        r = client.get("/threads/latest?user_id=u1")
    assert r.status_code == 200
    assert r.json() == {"thread_id": "abc123"}


def test_threads_latest_handles_colon_in_user_id(test_client) -> None:  # type: ignore[no-untyped-def]
    """A user id containing ``:`` still recovers the sub (slice by prefix
    length, not split on the first colon)."""
    client, _ = test_client
    fake_coll = MagicMock()
    fake_coll.find.return_value.__iter__ = lambda self: iter(
        [{"thread_id": "a:b:xyz"}]
    )
    fake_db = MagicMock()
    fake_db.__getitem__.return_value = fake_coll
    with patch("deep_agent.server.app.get_db", return_value=fake_db):
        r = client.get("/threads/latest?user_id=a:b")
    assert r.status_code == 200
    assert r.json() == {"thread_id": "xyz"}


def test_threads_latest_null_when_no_history(test_client) -> None:  # type: ignore[no-untyped-def]
    """Unknown user → 200 with ``{"thread_id": null}`` rather than 404."""
    client, _ = test_client
    fake_coll = MagicMock()
    fake_coll.find.return_value.__iter__ = lambda self: iter([])
    fake_db = MagicMock()
    fake_db.__getitem__.return_value = fake_coll
    with patch("deep_agent.server.app.get_db", return_value=fake_db):
        r = client.get("/threads/latest?user_id=nobody")
    assert r.status_code == 200
    assert r.json() == {"thread_id": None}


# ─── /interrupts/resume body + Command shape ─────────────────


@pytest.fixture
def hitl_client(monkeypatch):  # type: ignore[no-untyped-def]
    """A TestClient with HITL_TOOLS set so /interrupts/resume is registered."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("HITL_TOOLS", "execute_sql")

    fake_graph = MagicMock()

    # /interrupts/resume now STREAMS the resumed turn via
    # astream_events(Command(resume=...)), not a synchronous graph.invoke.
    # Capture the streamed input so tests can assert the Command shape.
    fake_graph.captured_inputs = []

    async def _astream_events(stream_input: Any = None, *_a: Any, **_k: Any):  # type: ignore[no-untyped-def]
        fake_graph.captured_inputs.append(stream_input)
        if False:
            yield None

    fake_graph.astream_events = _astream_events

    with ExitStack() as stack:
        stack.enter_context(
            patch("deep_agent.server.app.build_graph", return_value=fake_graph)
        )
        stack.enter_context(patch("deep_agent.server.app.ensure_indexes"))
        from deep_agent import config
        from deep_agent.server import app as appmod

        config.get_settings.cache_clear()
        appmod._GRAPH = None
        appmod._SHUTDOWN_EVENT = appmod.asyncio.Event()
        appmod._IN_FLIGHT_STREAMS = set()
        appmod._READINESS_CACHE.update(ok=False, checked_at=0.0, error=None)

        app = appmod.create_app()
        with patch("deep_agent.server.app.get_client") as gc:
            gc.return_value.admin.command.return_value = {"ok": 1}
            with TestClient(app) as client:
                yield client, fake_graph


def test_TC_E_503_060_edit_command_shape(hitl_client) -> None:  # type: ignore[no-untyped-def]
    """Edit decision yields Command with edited_action shape."""
    client, fake_graph = hitl_client
    body = {
        "thread_id": "t1",
        "decision": "edit",
        "edited_action": {"name": "execute_sql", "args": {"query": "SELECT 1"}},
    }
    with client.stream("POST", "/interrupts/resume", json=body) as r:
        assert r.status_code == 200, r.text
        b"".join(r.iter_bytes())  # drain so the producer runs

    cmd = fake_graph.captured_inputs[-1]
    decisions = cmd.resume["decisions"]
    assert decisions == [
        {
            "type": "edit",
            "edited_action": {"name": "execute_sql", "args": {"query": "SELECT 1"}},
        }
    ]


def test_TC_E_503_061_approve_and_reject_shapes(hitl_client) -> None:  # type: ignore[no-untyped-def]
    """Approve and reject use the documented shapes (streamed)."""
    client, fake_graph = hitl_client

    # Approve: bare {"type": "approve"}
    with client.stream(
        "POST", "/interrupts/resume", json={"thread_id": "t1", "decision": "approve"}
    ) as r:
        assert r.status_code == 200, r.text
        b"".join(r.iter_bytes())
    assert fake_graph.captured_inputs[-1].resume["decisions"] == [{"type": "approve"}]

    # Reject with message
    with client.stream(
        "POST",
        "/interrupts/resume",
        json={"thread_id": "t1", "decision": "reject", "message": "no"},
    ) as r:
        assert r.status_code == 200, r.text
        b"".join(r.iter_bytes())
    assert fake_graph.captured_inputs[-1].resume["decisions"] == [
        {"type": "reject", "message": "no"}
    ]


def test_TC_E_503_062_edit_without_edited_action_400s(hitl_client) -> None:  # type: ignore[no-untyped-def]
    """Edit decision without edited_action returns HTTP 400."""
    client, _ = hitl_client
    r = client.post(
        "/interrupts/resume", json={"thread_id": "t1", "decision": "edit"}
    )
    assert r.status_code == 400


# ─── HITL checkout interrupt frame + streaming resume ────────


class _FakeInterrupt:
    def __init__(self, value: Any) -> None:
        self.value = value


class _FakeState:
    def __init__(self, interrupts: list[Any]) -> None:
        self.interrupts = tuple(interrupts)
        self.next = ("tools",)
        self.values: dict[str, Any] = {}


@pytest.fixture
def hitl_place_order_client(monkeypatch):  # type: ignore[no-untyped-def]
    """TestClient with HITL_TOOLS=place_order and a fake graph that streams one
    token then pauses on a place_order interrupt (durable checkpoint)."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("HITL_TOOLS", "place_order")

    fake_graph = MagicMock()
    fake_graph.captured_inputs = []

    async def _astream_events(stream_input: Any = None, *_a: Any, **_k: Any):  # type: ignore[no-untyped-def]
        fake_graph.captured_inputs.append(stream_input)
        yield {
            "event": "on_chat_model_stream",
            "data": {"chunk": MagicMock(content="Ready to place your order.")},
        }

    fake_graph.astream_events = _astream_events

    interrupt_value = {
        "action_requests": [
            {
                "name": "place_order",
                "args": {"summary": "6 items, $29.91"},
                "description": "Place this order?",
            }
        ],
        "review_configs": [
            {"action_name": "place_order",
             "allowed_decisions": ["approve", "edit", "reject"]}
        ],
    }

    async def _aget_state(_cfg: Any) -> _FakeState:
        return _FakeState([_FakeInterrupt(interrupt_value)])

    fake_graph.aget_state = _aget_state

    with ExitStack() as stack:
        stack.enter_context(
            patch("deep_agent.server.app.build_graph", return_value=fake_graph)
        )
        stack.enter_context(patch("deep_agent.server.app.ensure_indexes"))
        from deep_agent import config
        from deep_agent.server import app as appmod

        config.get_settings.cache_clear()
        appmod._GRAPH = None
        appmod._SHUTDOWN_EVENT = appmod.asyncio.Event()
        appmod._IN_FLIGHT_STREAMS = set()
        appmod._READINESS_CACHE.update(ok=False, checked_at=0.0, error=None)

        app = appmod.create_app()
        with patch("deep_agent.server.app.get_client") as gc:
            gc.return_value.admin.command.return_value = {"ok": 1}
            with TestClient(app) as client:
                yield client, fake_graph


def test_TC_530_512_chat_emits_interrupt_frame(hitl_place_order_client) -> None:  # type: ignore[no-untyped-def]
    """When the graph pauses on place_order, /chat emits an
    `interrupt` SSE frame (with the proposed action + allowed decisions) before
    `done`."""
    client, _ = hitl_place_order_client
    with client.stream(
        "POST", "/chat",
        json={"user_id": "cust_R002", "thread_id": "t1", "message": "check out"},
    ) as r:
        assert r.status_code == 200
        body = b"".join(r.iter_bytes()).decode()

    assert "event: interrupt" in body
    assert "place_order" in body
    assert "approve" in body and "reject" in body
    # The composite thread_id is echoed so the client can resume the right checkpoint.
    assert "cust_R002:t1" in body
    assert "[DONE]" in body


def test_TC_530_513_resume_streams_tokens(hitl_place_order_client) -> None:  # type: ignore[no-untyped-def]
    """/interrupts/resume streams the resumed turn (tokens +
    done) and forwards the decision as Command(resume=...)."""
    client, fake_graph = hitl_place_order_client
    with client.stream(
        "POST", "/interrupts/resume",
        json={"thread_id": "cust_R002:t1", "decision": "approve"},
    ) as r:
        assert r.status_code == 200
        body = b"".join(r.iter_bytes()).decode()

    assert "Ready to place your order." in body
    assert "[DONE]" in body
    assert fake_graph.captured_inputs[-1].resume["decisions"] == [{"type": "approve"}]


def test_TC_530_520_hitl_disabled_no_interrupt_surface(test_client) -> None:  # type: ignore[no-untyped-def]
    """With HITL_TOOLS empty (default), the /interrupts* endpoints
    are not registered and /chat never emits an interrupt frame."""
    client, _ = test_client
    assert client.get("/interrupts", params={"thread_id": "t1"}).status_code == 404
    assert client.post(
        "/interrupts/resume", json={"thread_id": "t1", "decision": "approve"}
    ).status_code == 404

    with client.stream(
        "POST", "/chat", json={"user_id": "u1", "thread_id": "t1", "message": "hi"}
    ) as r:
        body = b"".join(r.iter_bytes()).decode()
    assert "event: interrupt" not in body
    assert "[DONE]" in body


# ─── model dropdown + per-request model override ──────────


def test_TC_E_505_010_models_endpoint_lists_registry(test_client) -> None:  # type: ignore[no-untyped-def]
    """GET /models returns the AVAILABLE_MODELS registry + default."""
    client, _ = test_client
    r = client.get("/models")
    assert r.status_code == 200
    body = r.json()
    assert "default" in body and isinstance(body["default"], str)
    assert isinstance(body["models"], list) and body["models"]
    # Every entry has id + label
    for m in body["models"]:
        assert "id" in m and "label" in m
    # The Settings default appears in the list
    ids = {m["id"] for m in body["models"]}
    assert body["default"] in ids


def test_TC_E_505_011_models_default_is_settings_llm_model(test_client) -> None:  # type: ignore[no-untyped-def]
    """/models 'default' equals Settings.llm_model."""
    from deep_agent.config import get_settings

    client, _ = test_client
    r = client.get("/models")
    assert r.json()["default"] == get_settings().llm_model


def test_TC_E_505_020_chat_rejects_unknown_model(test_client) -> None:  # type: ignore[no-untyped-def]
    """/chat with model not in AVAILABLE_MODELS returns 400."""
    client, _ = test_client
    r = client.post(
        "/chat",
        json={
            "user_id": "u1",
            "thread_id": "t1",
            "message": "hi",
            "model": "us.fake.not-real-model-v1:0",
        },
    )
    assert r.status_code == 400


def test_TC_E_505_021_chat_accepts_known_model(test_client, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """/chat with a model in AVAILABLE_MODELS uses _graph_for to build it."""
    from deep_agent.server import app as appmod

    client, _default_graph = test_client
    chosen = "global.anthropic.claude-haiku-4-5-20251001-v1:0"

    # Stub build_graph so the haiku graph build doesn't actually init Bedrock.
    fake_haiku = MagicMock()

    async def _astream_events(*_args: Any, **_kwargs: Any):  # type: ignore[no-untyped-def]
        yield {"event": "on_chat_model_stream", "data": {"chunk": MagicMock(content="haiku-ok")}}

    fake_haiku.astream_events = _astream_events
    monkeypatch.setattr(appmod, "build_graph", lambda model=None: fake_haiku)
    appmod._GRAPHS_BY_MODEL.clear()

    with client.stream(
        "POST",
        "/chat",
        json={
            "user_id": "u1",
            "thread_id": "t1",
            "message": "hi",
            "model": chosen,
        },
    ) as r:
        assert r.status_code == 200
        body = b"".join(r.iter_bytes()).decode()
    assert "haiku-ok" in body
    # And the cache now holds the chosen-model graph
    assert chosen in appmod._GRAPHS_BY_MODEL


def test_TC_E_505_022_chat_omitted_model_uses_default(test_client) -> None:  # type: ignore[no-untyped-def]
    """/chat without model uses lifespan default (no per-model build)."""
    from deep_agent.server import app as appmod

    appmod._GRAPHS_BY_MODEL.clear()
    client, _ = test_client
    with client.stream(
        "POST", "/chat", json={"user_id": "u1", "thread_id": "t1", "message": "hi"}
    ) as r:
        assert r.status_code == 200
        b"".join(r.iter_bytes())
    # Default path should NOT populate the per-model cache.
    assert appmod._GRAPHS_BY_MODEL == {}


# --- /messages + /plans read agent_log collection ----------


def test_TC_510_006_plans_reads_agent_log_collection(test_client) -> None:  # type: ignore[no-untyped-def]
    """/plans reads from ``Settings.agent_log_collection``
    (default ``agent_log``), not the legacy ``checkpoint_mirror`` name."""
    client, _ = test_client
    fake_coll = MagicMock()
    fake_coll.find.return_value.__iter__ = lambda self: iter([])
    fake_db = MagicMock()
    fake_db.__getitem__.return_value = fake_coll
    with patch("deep_agent.server.app.get_db", return_value=fake_db):
        client.get("/plans?user_id=u1&thread_id=t1")
    fake_db.__getitem__.assert_called_with("agent_log")


def test_TC_510_006_messages_reads_agent_log_collection(test_client) -> None:  # type: ignore[no-untyped-def]
    """/messages reads from the agent-log collection."""
    client, _ = test_client
    fake_coll = MagicMock()
    fake_coll.find.return_value.__iter__ = lambda self: iter([])
    fake_db = MagicMock()
    fake_db.__getitem__.return_value = fake_coll
    with patch("deep_agent.server.app.get_db", return_value=fake_db):
        client.get("/messages?user_id=u1&thread_id=t1")
    fake_db.__getitem__.assert_called_with("agent_log")


# --- hardening: composite-key reads, index provisioning, shutdown, CORS ----


def test_TC_520_plans_composite_key_roundtrip(test_client) -> None:  # type: ignore[no-untyped-def]
    """/chat writes under f"{user}:{sub}"; the read side must compose the
    same key. Insert ONLY the composite doc and query
    the bare sub — found proves composition (the old raw-param query found
    nothing). Uses a real mongomock collection so the filter actually applies."""
    from datetime import UTC
    from datetime import datetime as _dt

    import mongomock

    client, _ = test_client
    db = mongomock.MongoClient()["t"]
    db["agent_log"].insert_one(
        {
            "thread_id": "u1:t1",  # composite, as the writer stores it
            "user_id": "u1",
            "step": 0,
            "ts": _dt.now(UTC),
            "todos": [{"id": "1", "content": "x", "status": "pending"}],
            "messages": [{"type": "human", "content": "hi"}],
        }
    )
    with patch("deep_agent.server.app.get_db", return_value=db):
        plans = client.get("/plans?user_id=u1&thread_id=t1").json()
        msgs = client.get("/messages?user_id=u1&thread_id=t1").json()
    assert plans["todos"] == [{"id": "1", "text": "x", "status": "pending"}]
    assert msgs["messages"] == [{"type": "human", "content": "hi"}]


def _boot_app(monkeypatch, **patches):  # type: ignore[no-untyped-def]
    """Build + enter the app under explicit patches; returns the ExitStack so
    the caller can inspect mocks after the lifespan startup ran."""
    from contextlib import ExitStack

    from fastapi.testclient import TestClient

    from deep_agent import config
    from deep_agent.server import app as appmod

    config.get_settings.cache_clear()
    appmod._GRAPH = None
    appmod._SHUTDOWN_EVENT = appmod.asyncio.Event()
    appmod._IN_FLIGHT_STREAMS = set()
    appmod._READINESS_CACHE.update(ok=False, checked_at=0.0, error=None)
    stack = ExitStack()
    stack.enter_context(patch("deep_agent.server.app.build_graph", return_value=MagicMock()))
    ei = stack.enter_context(patch("deep_agent.server.app.ensure_indexes"))
    al = stack.enter_context(patch("deep_agent.server.app._agent_log"))
    gc = stack.enter_context(patch("deep_agent.server.app.get_client"))
    gc.return_value.admin.command.return_value = {"ok": 1}
    app = appmod.create_app()
    return stack, ei, al, app, TestClient


def test_TC_520_indexes_not_provisioned_on_boot_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """ensure_indexes must NOT run on a normal boot (the runtime
    role has no CREATE_INDEX in the RBAC playbook)."""
    monkeypatch.delenv("PROVISION_INDEXES_ON_BOOT", raising=False)
    stack, ei, _al, app, TestClient = _boot_app(monkeypatch)
    with stack, TestClient(app):
        pass
    assert ei.call_count == 0


def test_TC_520_indexes_provisioned_when_opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """PROVISION_INDEXES_ON_BOOT=true runs the one-shot DDL."""
    monkeypatch.setenv("PROVISION_INDEXES_ON_BOOT", "true")
    stack, ei, _al, app, TestClient = _boot_app(monkeypatch)
    with stack, TestClient(app):
        pass
    assert ei.call_count == 1


def test_TC_520_agent_log_closed_on_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """The AgentLog daemon worker is flushed/closed on shutdown
    before the MongoClient closes, so the final super-step's doc isn't lost."""
    monkeypatch.delenv("PROVISION_INDEXES_ON_BOOT", raising=False)
    stack, _ei, al, app, TestClient = _boot_app(monkeypatch)
    with stack:
        with TestClient(app):
            pass
        # close() called on the singleton during lifespan shutdown.
        al.return_value.close.assert_called_once()


def test_TC_520_user_id_field_not_auth_boundary() -> None:
    """The user_id field must not claim to be an auth boundary."""
    from deep_agent.server.app import ChatRequest

    desc = ChatRequest.model_fields["user_id"].description or ""
    assert "NOT authenticated" in desc
    assert "auth boundary" not in desc.replace("NOT authenticated", "")


def test_TC_520_health_redacts_db_and_model(test_client) -> None:  # type: ignore[no-untyped-def]
    """/health must not echo the Atlas DB name or model id."""
    client, _ = test_client
    body = client.get("/health").json()
    assert "db" not in body
    assert "llm_model" not in body
    assert body["status"] in ("ok", "degraded")
    assert "mongo" in body


def test_TC_520_cors_no_credentials_with_wildcard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wildcard CORS origins must not be combined with
    allow_credentials=True (Starlette would reflect any Origin credentialed)."""
    from starlette.middleware.cors import CORSMiddleware

    monkeypatch.setenv("ALLOW_INSECURE", "true")
    from deep_agent import config
    from deep_agent.server import app as appmod

    config.get_settings.cache_clear()
    appmod._GRAPH = None
    appmod._SHUTDOWN_EVENT = appmod.asyncio.Event()
    appmod._IN_FLIGHT_STREAMS = set()
    with (
        patch("deep_agent.server.app.build_graph", return_value=MagicMock()),
        patch("deep_agent.server.app.ensure_indexes"),
    ):
        app = appmod.create_app()
    cors = next(m for m in app.user_middleware if m.cls is CORSMiddleware)
    opts = cors.kwargs
    assert opts["allow_origins"] == ["*"]
    assert opts["allow_credentials"] is False


# ─── feedback run_id propagation ─────────────────────────────────
#
# The frontend reads the leading `correlation` SSE frame and round-trips that
# id as the feedback `run_id`. For the LangSmith `user_score` mirror to land on
# the real trace, the RunnableConfig MUST set `run_id` to that same correlation
# id. These tests pin that contract.


def test_TC_540_A01_chat_config_run_id_equals_correlation(test_client) -> None:  # type: ignore[no-untyped-def]
    """/chat sets the LangSmith root run_id == correlation_id, and the same
    id is emitted on the `correlation` frame the frontend reads."""
    import uuid

    client, fake_graph = test_client
    captured: dict[str, Any] = {}

    async def _capture(*args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        captured["config"] = kwargs.get("config")
        yield {"event": "on_chat_model_stream", "data": {"chunk": MagicMock(content="hi")}}

    fake_graph.astream_events = _capture
    with client.stream(
        "POST", "/chat", json={"user_id": "u1", "thread_id": "t1", "message": "hi"}
    ) as r:
        body = b"".join(r.iter_bytes()).decode()

    cfg = captured["config"]
    assert "run_id" in cfg, "RunnableConfig must set run_id for the feedback mirror to attach"
    cid = cfg["configurable"]["correlation_id"]
    assert cfg["run_id"] == uuid.UUID(cid)
    # The frontend reads this exact id off the `correlation` frame.
    assert cid in body


def test_TC_540_A02_resume_config_run_id_equals_correlation(hitl_client) -> None:  # type: ignore[no-untyped-def]
    """/interrupts/resume sets run_id == correlation_id too."""
    import uuid

    client, fake_graph = hitl_client
    captured: dict[str, Any] = {}

    async def _capture(stream_input: Any = None, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        fake_graph.captured_inputs.append(stream_input)
        captured["config"] = kwargs.get("config")
        if False:
            yield None

    fake_graph.astream_events = _capture
    with client.stream(
        "POST", "/interrupts/resume", json={"thread_id": "t1", "decision": "approve"}
    ) as r:
        assert r.status_code == 200, r.text
        b"".join(r.iter_bytes())

    cfg = captured["config"]
    assert "run_id" in cfg, "resume RunnableConfig must set run_id"
    assert cfg["run_id"] == uuid.UUID(cfg["configurable"]["correlation_id"])


def test_TC_540_A03_feedback_mirror_targets_run_id_and_persists(
    monkeypatch: pytest.MonkeyPatch, test_client  # type: ignore[no-untyped-def]
) -> None:
    """The LangSmith mirror targets exactly the submitted run_id with key
    'user_score', AND the Mongo feedback doc is always written. (The run_id ==
    correlation_id linkage is proven by TC-540-A01 plus the unchanged frontend
    round-trip.)"""
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    from deep_agent import config

    config.get_settings.cache_clear()

    client, _ = test_client
    with patch("deep_agent.server.app.get_db") as gdb, patch("langsmith.Client") as lc:
        coll = MagicMock()
        db = MagicMock()
        db.__getitem__.return_value = coll
        gdb.return_value = db
        r = client.post(
            "/feedback",
            json={"run_id": "abc-run", "score": 0.0, "comment": "bad", "user_id": "u1"},
        )

    assert r.status_code == 200
    coll.insert_one.assert_called_once()  # always persisted to Mongo
    lc.return_value.create_feedback.assert_called_once_with(
        run_id="abc-run", key="user_score", score=0.0, comment="bad"
    )


def test_TC_540_A04_run_ids_distinct_and_uuid_safe(test_client) -> None:  # type: ignore[no-untyped-def]
    """Distinct turns get distinct run_ids, and a garbage X-Correlation-Id
    still yields a valid-UUID run_id (the correlation middleware regenerates
    one)."""
    import uuid

    client, fake_graph = test_client
    seen: list[Any] = []

    async def _capture(*args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        # Capture the whole config OUTSIDE any indexing so a missing run_id
        # surfaces as a failed assertion below, not a swallowed KeyError.
        seen.append(kwargs.get("config"))
        yield {"event": "on_chat_model_stream", "data": {"chunk": MagicMock(content="x")}}

    fake_graph.astream_events = _capture

    for _ in range(2):
        with client.stream(
            "POST", "/chat", json={"user_id": "u1", "thread_id": "t1", "message": "hi"}
        ) as r:
            b"".join(r.iter_bytes())

    # Garbage correlation header → middleware regenerates a valid UUID.
    with client.stream(
        "POST",
        "/chat",
        headers={"X-Correlation-Id": "not-a-uuid"},
        json={"user_id": "u1", "thread_id": "t1", "message": "hi"},
    ) as r:
        b"".join(r.iter_bytes())

    assert len(seen) == 3, "all three turns must have driven the graph"
    run_ids = [cfg.get("run_id") for cfg in seen]
    assert all(isinstance(x, uuid.UUID) for x in run_ids), f"run_ids not all UUIDs: {run_ids}"
    assert len(set(run_ids)) == 3, "each turn must get a distinct run_id"
