"""Safety-wrapped MongoDBDatabaseToolkit."""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    from deep_agent import config, models

    config.get_settings.cache_clear()
    models.get_llm.cache_clear()


def test_TC_10_010_destructive_stage_refused() -> None:
    from deep_agent.tools.database_toolkit import QueryRefusedError, enforce_safety

    with pytest.raises(QueryRefusedError, match="destructive"):
        enforce_safety(
            "orders",
            json.dumps([{"$match": {"x": 1}}, {"$out": "leak"}]),
            {"orders"},
        )


def test_TC_10_020_destructive_keyword_refused() -> None:
    from deep_agent.tools.database_toolkit import QueryRefusedError, enforce_safety

    with pytest.raises(QueryRefusedError, match="destructive"):
        enforce_safety("orders", json.dumps([{"$match": {"drop": 1}}]), {"orders"})


def test_TC_10_030_underscore_prefix_refused() -> None:
    from deep_agent.tools.database_toolkit import QueryRefusedError, enforce_safety

    with pytest.raises(QueryRefusedError, match="underscore"):
        enforce_safety("_system", json.dumps([{"$match": {}}]), {"_system"})


def test_TC_10_040_allow_list_enforced() -> None:
    from deep_agent.tools.database_toolkit import QueryRefusedError, enforce_safety

    with pytest.raises(QueryRefusedError, match="disallowed"):
        enforce_safety("not_in_list", json.dumps([{"$match": {}}]), {"orders"})


def test_TC_10_050_implicit_limit_injected() -> None:
    from deep_agent.tools.database_toolkit import DEFAULT_PIPELINE_LIMIT, enforce_safety

    safe = enforce_safety("orders", json.dumps([{"$match": {"a": 1}}]), {"orders"})
    stages = json.loads(safe)
    assert stages[-1] == {"$limit": DEFAULT_PIPELINE_LIMIT}


def test_TC_10_051_limit_not_duplicated() -> None:
    from deep_agent.tools.database_toolkit import enforce_safety

    original = json.dumps([{"$match": {}}, {"$limit": 10}])
    safe = enforce_safety("orders", original, {"orders"})
    stages = json.loads(safe)
    assert sum(1 for s in stages if "$limit" in s) == 1


def test_TC_10_052_unparseable_pipeline_untouched() -> None:
    from deep_agent.tools.database_toolkit import enforce_safety

    raw = "not-json"
    assert enforce_safety("orders", raw, {"orders"}) == raw


def test_TC_10_060_toolkit_instantiated_with_db_and_llm() -> None:
    with patch("deep_agent.tools.database_toolkit.MongoDBDatabaseToolkit") as tk, patch(
        "deep_agent.tools.database_toolkit._database"
    ) as db, patch("deep_agent.tools.database_toolkit.get_llm") as gl:
        db.return_value = object()
        gl.return_value = object()
        from deep_agent.tools.database_toolkit import _toolkit

        _toolkit.cache_clear()
        _toolkit()
    tk.assert_called_once()
    _, kwargs = tk.call_args
    assert kwargs["db"] is db.return_value
    assert kwargs["llm"] is gl.return_value


def test_TC_10_061_get_data_tools_wraps_each_inner_tool() -> None:
    from langchain_core.tools import BaseTool

    class _Inner(BaseTool):
        name: str = "inner"
        description: str = "desc"

        def _run(self, *args: Any, **kwargs: Any) -> str:
            return "ok"

    fake_toolkit = MagicMock()
    fake_toolkit.get_tools.return_value = [_Inner(), _Inner()]

    from deep_agent.tools import database_toolkit as dbt

    dbt._toolkit.cache_clear()
    with patch("deep_agent.tools.database_toolkit._toolkit", return_value=fake_toolkit):
        tools = dbt.get_data_tools()

    assert len(tools) == 2
    assert all(isinstance(t, dbt._SafeToolWrapper) for t in tools)
    assert "safety-wrapped" in tools[0].description


def test_TC_10_062_safe_wrapper_refuses_unsafe_and_runs_safe() -> None:
    from langchain_core.tools import BaseTool

    from deep_agent.tools import database_toolkit as dbt

    captured: dict[str, Any] = {}

    class _Inner(BaseTool):
        name: str = "mongodb_query"
        description: str = "runs a query"

        def _run(self, *args: Any, **kwargs: Any) -> str:
            captured.clear()
            captured.update(kwargs)
            return "ran"

    wrapped = dbt._SafeToolWrapper(inner=_Inner(), allow_list={"orders"})

    # Unsafe: $out in pipeline
    result = wrapped._run(
        query=json.dumps([{"$out": "leak"}]), collection="orders"
    )
    assert isinstance(result, str) and result.startswith("QUERY REFUSED")
    assert captured == {}

    # Safe: pipeline with $match; implicit $limit injected
    out = wrapped._run(
        query=json.dumps([{"$match": {"ok": 1}}]), collection="orders"
    )
    assert out == "ran"
    stages = json.loads(captured["query"])
    assert stages[-1] == {"$limit": dbt.DEFAULT_PIPELINE_LIMIT}


def test_TC_10_063_safe_wrapper_arun_delegates_to_run() -> None:
    from langchain_core.tools import BaseTool

    from deep_agent.tools import database_toolkit as dbt

    class _Inner(BaseTool):
        name: str = "t"
        description: str = "d"

        def _run(self, *args: Any, **kwargs: Any) -> str:
            return "sync"

    wrapped = dbt._SafeToolWrapper(inner=_Inner(), allow_list={"orders"})
    loop = asyncio.new_event_loop()
    try:
        out = loop.run_until_complete(
            wrapped._arun(query=json.dumps([{"$match": {}}]), collection="orders")
        )
    finally:
        loop.close()
    assert out == "sync"


def test_TC_10_064_safe_wrapper_normalizes_pipeline_kwarg_and_positional() -> None:
    from langchain_core.tools import BaseTool

    from deep_agent.tools import database_toolkit as dbt

    captured: dict[str, Any] = {"args": (), "kwargs": {}}

    class _Inner(BaseTool):
        name: str = "mongodb_query"
        description: str = "d"

        def _run(self, *args: Any, **kwargs: Any) -> str:
            captured["args"] = args
            captured["kwargs"] = dict(kwargs)
            return "ok"

    wrapped = dbt._SafeToolWrapper(inner=_Inner(), allow_list={"orders"})

    wrapped._run(pipeline=json.dumps([{"$match": {}}]), collection="orders")
    stages = json.loads(captured["kwargs"]["pipeline"])
    assert stages[-1] == {"$limit": dbt.DEFAULT_PIPELINE_LIMIT}

    captured["args"] = ()
    captured["kwargs"] = {}
    wrapped._run(json.dumps([{"$match": {}}]), collection="orders")
    stages = json.loads(captured["args"][0])
    assert stages[-1] == {"$limit": dbt.DEFAULT_PIPELINE_LIMIT}


def test_TC_10_070_data_agent_uri_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MONGODB_URI", "mongodb+srv://app@cluster/deep_agent")
    monkeypatch.setenv("DATA_AGENT_MONGODB_URI", "mongodb+srv://dataagent@cluster/deep_agent")
    from deep_agent import config

    config.get_settings.cache_clear()

    with patch("deep_agent.tools.database_toolkit.MongoDBDatabase") as mdb:
        from deep_agent.tools.database_toolkit import _database

        _database.cache_clear()
        _database()
    _, kwargs = mdb.from_connection_string.call_args
    assert kwargs["connection_string"] == "mongodb+srv://dataagent@cluster/deep_agent"


def test_TC_10_080_falls_back_to_mongodb_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MONGODB_URI", "mongodb+srv://app@cluster/deep_agent")
    monkeypatch.delenv("DATA_AGENT_MONGODB_URI", raising=False)
    from deep_agent import config

    config.get_settings.cache_clear()

    with patch("deep_agent.tools.database_toolkit.MongoDBDatabase") as mdb:
        from deep_agent.tools.database_toolkit import _database

        _database.cache_clear()
        _database()
    _, kwargs = mdb.from_connection_string.call_args
    assert kwargs["connection_string"] == "mongodb+srv://app@cluster/deep_agent"


# -------------------- per-(domain, db_name) cache keys --------------------


def test_TC_40_INV_404_enforce_safety_unchanged() -> None:
    """Safety rules (destructive ops, underscore prefix, allow-list,
    implicit $limit) are untouched by the cache-key change. Spot-check all four."""
    from deep_agent.tools.database_toolkit import (
        DEFAULT_PIPELINE_LIMIT,
        QueryRefusedError,
        enforce_safety,
    )

    # Destructive op
    with pytest.raises(QueryRefusedError, match="destructive"):
        enforce_safety("orders", json.dumps([{"$out": "x"}]), {"orders"})
    # Underscore-prefixed collection
    with pytest.raises(QueryRefusedError, match="underscore"):
        enforce_safety("_hidden", json.dumps([{"$match": {}}]), set())
    # Non-allow-listed collection
    with pytest.raises(QueryRefusedError, match="disallowed"):
        enforce_safety("evil", json.dumps([{"$match": {}}]), {"orders"})
    # Implicit $limit injected
    out = enforce_safety("orders", json.dumps([{"$match": {}}]), {"orders"})
    stages = json.loads(out)
    assert stages[-1] == {"$limit": DEFAULT_PIPELINE_LIMIT}


def test_TC_40_427_safe_wrapper_catches_inner_tool_exception() -> None:
    """The safety wrapper must catch exceptions raised by the underlying
    MongoDB toolkit tool — a bare raise leaves the parent agent's ``tool_use``
    block without a ``tool_result`` and Bedrock rejects the next turn with
    ``tool_use ids were found without tool_result blocks``. Return a
    structured error string instead."""
    from langchain_core.tools import BaseTool

    from deep_agent.tools import database_toolkit as dbt

    class _Exploding(BaseTool):
        name: str = "t"
        description: str = "d"

        def _run(self, *args: Any, **kwargs: Any) -> str:
            raise RuntimeError("collection 'accounts' not found")

    wrapped = dbt._SafeToolWrapper(inner=_Exploding(), allow_list={"accounts"})

    out = wrapped._run(
        query=json.dumps([{"$match": {}}]), collection="accounts"
    )
    assert isinstance(out, str)
    assert out.startswith("QUERY ERROR")
    assert "RuntimeError" in out


def test_TC_40_INV_401_get_data_tools_no_args_uses_env() -> None:
    """get_data_tools() with no args still resolves to env DOMAIN +
    Settings.mongodb_db (single-domain CLI path stays unchanged)."""
    from langchain_core.tools import BaseTool

    from deep_agent.tools import database_toolkit as dbt

    class _Inner(BaseTool):
        name: str = "q"
        description: str = "d"

        def _run(self, *args: Any, **kwargs: Any) -> str:
            return "ok"

    fake_toolkit = MagicMock()
    fake_toolkit.get_tools.return_value = [_Inner()]

    dbt._database.cache_clear()
    dbt._toolkit.cache_clear()
    with patch(
        "deep_agent.tools.database_toolkit._toolkit", return_value=fake_toolkit
    ) as mock_tk:
        tools = dbt.get_data_tools()

    # No-arg path does not pass an override: _toolkit should be called with None
    # (or no positional) so it binds to Settings.mongodb_db.
    args, _ = mock_tk.call_args
    assert args in ((), (None,))
    assert len(tools) == 1


# --- AST safety walk --------------------------------


def test_TC_R501_110_lookup_disallowed_collection_refused() -> None:
    """$lookup.from outside the allow-list is refused."""
    from deep_agent.tools.database_toolkit import (
        QueryRefusedError,
        enforce_safety,
    )

    pipeline = json.dumps([
        {"$match": {"x": 1}},
        {"$lookup": {"from": "admin_users", "localField": "id", "foreignField": "uid", "as": "u"}},
    ])
    with pytest.raises(QueryRefusedError, match="admin_users"):
        enforce_safety("orders", pipeline, allow_list={"orders"})


def test_TC_R501_110_lookup_allowed_collection_ok() -> None:
    """$lookup to a collection IN the allow-list is fine."""
    from deep_agent.tools.database_toolkit import enforce_safety

    pipeline = json.dumps([
        {"$lookup": {"from": "customers", "localField": "uid", "foreignField": "_id", "as": "c"}},
        {"$limit": 50},
    ])
    out = enforce_safety("orders", pipeline, allow_list={"orders", "customers"})
    assert json.loads(out) == json.loads(pipeline)


def test_TC_R501_110_graphlookup_disallowed_refused() -> None:
    """$graphLookup.from outside the allow-list is refused."""
    from deep_agent.tools.database_toolkit import (
        QueryRefusedError,
        enforce_safety,
    )

    pipeline = json.dumps([
        {"$graphLookup": {
            "from": "secret_audit",
            "startWith": "$id",
            "connectFromField": "parent",
            "connectToField": "id",
            "as": "tree",
        }},
    ])
    with pytest.raises(QueryRefusedError, match="secret_audit"):
        enforce_safety("orders", pipeline, allow_list={"orders"})


def test_TC_R501_110_unionwith_string_form_refused() -> None:
    """$unionWith: 'collname' (string form) refused."""
    from deep_agent.tools.database_toolkit import (
        QueryRefusedError,
        enforce_safety,
    )

    pipeline = json.dumps([
        {"$match": {"y": 1}},
        {"$unionWith": "admin_users"},
    ])
    with pytest.raises(QueryRefusedError, match="admin_users"):
        enforce_safety("orders", pipeline, allow_list={"orders"})


def test_TC_R501_110_unionwith_dict_form_refused() -> None:
    """$unionWith: {coll: ..., pipeline: ...} dict form refused."""
    from deep_agent.tools.database_toolkit import (
        QueryRefusedError,
        enforce_safety,
    )

    pipeline = json.dumps([
        {"$unionWith": {"coll": "secrets", "pipeline": [{"$match": {"x": 1}}]}},
    ])
    with pytest.raises(QueryRefusedError, match="secrets"):
        enforce_safety("orders", pipeline, allow_list={"orders"})


def test_TC_R501_110_nested_unionwith_pipeline_walked() -> None:
    """A nested $unionWith.pipeline containing $lookup → also walked."""
    from deep_agent.tools.database_toolkit import (
        QueryRefusedError,
        enforce_safety,
    )

    pipeline = json.dumps([
        {"$unionWith": {
            "coll": "orders",  # allowed
            "pipeline": [
                {"$lookup": {"from": "admin_users", "localField": "id", "foreignField": "uid", "as": "u"}},
            ],
        }},
    ])
    with pytest.raises(QueryRefusedError, match="admin_users"):
        enforce_safety("orders", pipeline, allow_list={"orders"})


def test_TC_R501_110_underscore_pipeline_target_refused() -> None:
    """$lookup to an underscore-prefixed collection is refused even in explicit
    open mode (``allow_all=True``).

    Previously, an empty allow-list silently meant "allow all non-underscore"
    (fail-OPEN). Now: open mode must be opted into via ``allow_all=True``
    (DATA_AGENT_ALLOW_ALL); an empty allow-list without it fails CLOSED. The
    security intent — underscore targets refused even in open mode — is
    unchanged, now expressed with the explicit flag.
    """
    from deep_agent.tools.database_toolkit import (
        QueryRefusedError,
        enforce_safety,
    )

    pipeline = json.dumps([
        {"$lookup": {"from": "_internal", "localField": "x", "foreignField": "y", "as": "z"}},
    ])
    with pytest.raises(QueryRefusedError, match="_internal"):
        enforce_safety("orders", pipeline, allow_list=set(), allow_all=True)


# -------------------- sandbox hardening --------------------


def test_TC_R501_130_empty_allow_list_fails_closed() -> None:
    """#4(c): an empty allow-list refuses every collection (fail-CLOSED),
    matching the documented contract — not fail-OPEN."""
    from deep_agent.tools.database_toolkit import QueryRefusedError, enforce_safety

    with pytest.raises(QueryRefusedError, match="allow-list"):
        enforce_safety("any_collection", json.dumps([{"$match": {}}]), set())


def test_TC_R501_131_allow_all_opt_in_permits() -> None:
    """#4(c): the explicit DATA_AGENT_ALLOW_ALL escape hatch allows an empty
    allow-list to run non-underscore queries (still limit-capped)."""
    from deep_agent.tools.database_toolkit import DEFAULT_PIPELINE_LIMIT, enforce_safety

    out = enforce_safety("anything", json.dumps([{"$match": {}}]), set(), allow_all=True)
    assert json.loads(out)[-1] == {"$limit": DEFAULT_PIPELINE_LIMIT}


def test_TC_R501_112_lookup_in_mongosh_string_refused() -> None:
    """#4(a): a $lookup into a non-allow-listed collection in a MONGOSH STRING
    (not JSON) is refused — the AST walk no longer only fires on JSON input."""
    from deep_agent.tools.database_toolkit import QueryRefusedError, enforce_safety

    text = (
        "db.orders.aggregate([{ $lookup: { from: 'long_term_memory', "
        "localField: 'x', foreignField: 'y', as: 'z' } }])"
    )
    with pytest.raises(QueryRefusedError, match="long_term_memory"):
        enforce_safety("orders", text, allow_list={"orders"})


def test_TC_R501_113_unionwith_mongosh_string_refused() -> None:
    """#4(a): $unionWith into agent_log (v0.3 conversation log) in a mongosh
    string is refused."""
    from deep_agent.tools.database_toolkit import QueryRefusedError, enforce_safety

    text = "db.orders.aggregate([{ $unionWith: 'agent_log' }])"
    with pytest.raises(QueryRefusedError, match="agent_log"):
        enforce_safety("orders", text, allow_list={"orders"})


def test_TC_R501_140_limit_substring_false_positive() -> None:
    """#4(d): a literal '$limit' inside match data must NOT be mistaken for an
    explicit $limit stage — the cap is still injected structurally."""
    from deep_agent.tools.database_toolkit import DEFAULT_PIPELINE_LIMIT, enforce_safety

    out = enforce_safety(
        "orders", json.dumps([{"$match": {"note": "mentions $limit here"}}]), {"orders"}
    )
    assert json.loads(out)[-1] == {"$limit": DEFAULT_PIPELINE_LIMIT}


def test_TC_R501_141_mongosh_aggregate_gets_limit() -> None:
    """#4(d): a mongosh aggregate string without $limit gets the cap injected."""
    from deep_agent.tools.database_toolkit import DEFAULT_PIPELINE_LIMIT, enforce_safety

    text = "db.orders.aggregate([{ $match: {} }])"
    out = enforce_safety("orders", text, allow_list={"orders"})
    assert "$limit" in out and str(DEFAULT_PIPELINE_LIMIT) in out


def test_TC_R501_120_schema_tool_refuses_non_allowlisted() -> None:
    """#4(b): mongodb_schema must NOT dump sample docs from a non-allow-listed
    (internal) collection — it is gated by the same allow-list, not bypassed."""
    from langchain_core.tools import BaseTool

    from deep_agent.tools import database_toolkit as dbt

    seen: dict[str, object] = {}

    class _FakeSchema(BaseTool):
        name: str = "mongodb_schema"
        description: str = "schema"

        def _run(self, *args: object, **kwargs: object) -> str:
            seen["called"] = (args, kwargs)
            return "SENSITIVE SAMPLE DOCS"

    wrapped = dbt._SafeToolWrapper(inner=_FakeSchema(), allow_list={"orders"})
    out = wrapped._run(tool_input="long_term_memory")
    assert isinstance(out, str) and out.startswith("QUERY REFUSED")
    assert "called" not in seen, "inner schema tool must not run for a disallowed collection"


def test_TC_R501_121_schema_tool_allows_allowlisted() -> None:
    """#4(b): an allow-listed collection still reaches the schema tool."""
    from langchain_core.tools import BaseTool

    from deep_agent.tools import database_toolkit as dbt

    class _FakeSchema(BaseTool):
        name: str = "mongodb_schema"
        description: str = "schema"

        def _run(self, *args: object, **kwargs: object) -> str:
            return "orders schema"

    wrapped = dbt._SafeToolWrapper(inner=_FakeSchema(), allow_list={"orders"})
    out = wrapped._run(tool_input="orders")
    assert out == "orders schema"


def test_TC_R501_111_non_json_pipeline_falls_back_to_regex() -> None:
    """Non-JSON pipelines still hit the regex layer for $out etc."""
    from deep_agent.tools.database_toolkit import (
        QueryRefusedError,
        enforce_safety,
    )

    # Mongosh-style query string that's not pure JSON. The AST walk gives up
    # silently; the regex still catches $out.
    text = "db.orders.aggregate([{ $out: 'leaked' }])"
    with pytest.raises(QueryRefusedError):
        enforce_safety("orders", text, allow_list={"orders"})
