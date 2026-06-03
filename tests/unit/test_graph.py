"""Sub-phase 12: deep-agent graph composition."""
from __future__ import annotations

from contextlib import ExitStack
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    from deep_agent import config, models

    config.get_settings.cache_clear()
    models.get_llm.cache_clear()


def test_TC_12_010_build_graph_invokes_create_deep_agent() -> None:
    from deep_agent import graph as graph_mod

    fake_graph = MagicMock()
    fake_checkpointer = object()
    fake_store = object()
    fake_llm = MagicMock()

    with patch("deep_agent.graph.create_deep_agent", return_value=fake_graph) as cda, patch(
        "deep_agent.graph.get_llm", return_value=fake_llm
    ), patch(
        "deep_agent.graph.build_checkpointer", return_value=fake_checkpointer
    ), patch("deep_agent.graph.build_store", return_value=fake_store), patch("deep_agent.graph.get_data_tools", return_value=[]):
        result = graph_mod.build_graph()

    assert result is fake_graph
    cda.assert_called_once()
    _, kwargs = cda.call_args
    assert kwargs["model"] is fake_llm
    assert kwargs["checkpointer"] is fake_checkpointer
    assert kwargs["store"] is fake_store
    assert kwargs["system_prompt"]
    assert kwargs["subagents"]
    # Main agent tools include KB + KG + fetch_and_cache (plus data toolkit output).
    tool_names = {_tool_name(t) for t in kwargs["tools"]}
    assert {"knowledge_base_search", "knowledge_base_hybrid_search",
            "knowledge_graph_search", "fetch_and_cache"} <= tool_names


def test_TC_20_070_build_graph_wires_mongo_backend_instance() -> None:
    """build_graph must pass a backend INSTANCE (not a factory callable) to
    create_deep_agent so deepagents 0.7+ keeps working.

    Earlier the backend was a bare MongoVfsBackend instance; it is now wrapped in
    a CompositeBackend so ``/memories/**`` routes to a StoreBackend; the
    no-factory invariant still holds — both Composite and its default leg
    must be live instances.
    """
    from deepagents.backends.composite import CompositeBackend

    from deep_agent import graph as graph_mod
    from deep_agent.backends.mongo_backend import MongoVfsBackend

    with patch("deep_agent.graph.create_deep_agent") as cda, patch(
        "deep_agent.graph.get_llm", return_value=MagicMock()
    ), patch("deep_agent.graph.build_checkpointer", return_value=object()), patch(
        "deep_agent.graph.build_store", return_value=object()
    ), patch(
        "deep_agent.graph.get_data_tools", return_value=[]
    ):
        graph_mod.build_graph()

    _, kwargs = cda.call_args
    backend = kwargs.get("backend")
    assert isinstance(backend, CompositeBackend), (
        f"backend must be a CompositeBackend, got {type(backend).__name__}"
    )
    assert isinstance(backend.default, MongoVfsBackend), (
        f"composite default must be MongoVfsBackend, got {type(backend.default).__name__}"
    )
    # No bare factory callables on either layer.
    assert not (callable(backend) and not isinstance(backend, CompositeBackend))


def test_TC_12_020_researcher_subagent_has_expected_tools() -> None:
    from deep_agent.agents.subagents import researcher_subagent

    sub = researcher_subagent()
    names = {_tool_name(t) for t in sub["tools"]}
    assert {
        "web_search",
        "fetch_and_cache",
        "knowledge_base_search",
        "knowledge_base_hybrid_search",
        "knowledge_graph_search",
    } <= names


def test_TC_12_021_researcher_subagent_dict_valid() -> None:
    from deep_agent.agents.subagents import researcher_subagent

    sub = researcher_subagent()
    assert sub["name"] == "researcher"
    assert sub["description"]
    assert sub["system_prompt"]


def test_TC_12_030_graph_gets_mongodb_saver_and_store() -> None:
    from deep_agent import graph as graph_mod

    saver = object()
    store = object()
    with patch("deep_agent.graph.create_deep_agent") as cda, patch(
        "deep_agent.graph.get_llm", return_value=MagicMock()
    ), patch(
        "deep_agent.graph.build_checkpointer", return_value=saver
    ), patch("deep_agent.graph.build_store", return_value=store), patch("deep_agent.graph.get_data_tools", return_value=[]):
        graph_mod.build_graph()

    _, kwargs = cda.call_args
    assert kwargs["checkpointer"] is saver
    assert kwargs["store"] is store




def test_TC_12_070_build_graph_uncheckpointed_has_no_persistence() -> None:
    from deep_agent import graph as graph_mod

    with patch("deep_agent.graph.create_deep_agent") as cda, patch(
        "deep_agent.graph.get_llm", return_value=MagicMock()
    ), patch("deep_agent.graph.get_data_tools", return_value=[]):
        graph_mod.build_graph_uncheckpointed()

    _, kwargs = cda.call_args
    # Unchecked variant must not pass checkpointer or store
    assert kwargs.get("checkpointer") is None
    assert kwargs.get("store") is None


# -------------------- per-domain graph cache --------------------


def _tool_name(t: Any) -> str:
    # Tools produced by @langchain_core.tools.tool expose `.name`; safety-wrapped tools too.
    return getattr(t, "name", "") or ""


def _build_graph_capture_kwargs() -> dict[str, Any]:
    """Build the graph with create_deep_agent stubbed; return its kwargs."""
    from deep_agent import graph as graph_mod

    with patch("deep_agent.graph.create_deep_agent") as cda, patch(
        "deep_agent.graph.get_llm", return_value=MagicMock()
    ), patch("deep_agent.graph.build_checkpointer", return_value=object()), patch(
        "deep_agent.graph.build_store", return_value=object()
    ), patch("deep_agent.graph.get_data_tools", return_value=[]):
        graph_mod.build_graph()
    _, kwargs = cda.call_args
    return kwargs


def test_TC_530_510_hitl_interrupt_on_place_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """HITL_TOOLS=place_order → interrupt_on entry."""
    monkeypatch.setenv("HITL_TOOLS", "place_order")
    from deep_agent import config, graph

    config.get_settings.cache_clear()
    assert graph._hitl_interrupt_on() == {
        "place_order": {"allowed_decisions": ["approve", "edit", "reject"]}
    }


def test_TC_530_400_build_graph_registers_all_subagents() -> None:
    """The 4 specialists join researcher + writer."""
    kwargs = _build_graph_capture_kwargs()
    names = {s["name"] for s in kwargs["subagents"]}
    assert names == {
        "researcher",
        "writer",
        "deal_optimizer",
        "loyalty_concierge",
        "reorder_concierge",
        "basket_cross_sell",
    }


def test_TC_530_401_subagent_tool_bindings() -> None:
    """deal_optimizer binds savings + cart-read;
    loyalty_concierge binds current_shopper + memory and NO cart tools."""
    from deep_agent.agents.subagents import (
        deal_optimizer_subagent,
        loyalty_concierge_subagent,
    )

    deal = {_tool_name(t) for t in deal_optimizer_subagent(data_tools=[])["tools"]}
    assert {"savings_calculator", "view_cart", "update_cart_item",
            "knowledge_graph_search"} <= deal

    loyalty = {_tool_name(t) for t in loyalty_concierge_subagent(data_tools=[])["tools"]}
    assert {"current_shopper", "recall_memories", "knowledge_base_search"} <= loyalty
    # Informational briefing — no cart mutation tools.
    assert not ({"add_to_cart", "update_cart_item", "view_cart", "place_order"} & loyalty)

    from deep_agent.agents.subagents import (
        basket_cross_sell_subagent,
        reorder_concierge_subagent,
    )

    reorder = {_tool_name(t) for t in reorder_concierge_subagent(data_tools=[])["tools"]}
    assert {"current_shopper", "add_to_cart", "view_cart"} <= reorder

    cross = {_tool_name(t) for t in basket_cross_sell_subagent(data_tools=[])["tools"]}
    assert {"add_to_cart", "view_cart", "knowledge_graph_search"} <= cross


def test_TC_530_501_place_order_is_main_agent_only() -> None:
    """place_order MUST live on the
    main agent (resumable HITL) and NEVER on a subagent (subagents run with no
    checkpointer, so an interrupt inside one is unrecoverable)."""
    from deep_agent.agents.subagents import (
        basket_cross_sell_subagent,
        deal_optimizer_subagent,
        loyalty_concierge_subagent,
        reorder_concierge_subagent,
        researcher_subagent,
        writer_subagent,
    )

    kwargs = _build_graph_capture_kwargs()
    main_tools = {_tool_name(t) for t in kwargs["tools"]}
    assert "place_order" in main_tools
    assert {"add_to_cart", "view_cart", "current_shopper"} <= main_tools

    specialists = [
        researcher_subagent(),
        writer_subagent(),
        deal_optimizer_subagent(data_tools=[]),
        loyalty_concierge_subagent(data_tools=[]),
        reorder_concierge_subagent(data_tools=[]),
        basket_cross_sell_subagent(data_tools=[]),
    ]
    for sub in specialists:
        names = {_tool_name(t) for t in sub["tools"]}
        assert "place_order" not in names, f"{sub['name']} must not carry place_order"


# --- agent kwargs + middleware + HITL ----


def test_TC_R501_032_name_set() -> None:
    """build_graph passes name='deep-agent' to create_deep_agent."""
    from deep_agent import graph as graph_mod

    captured: list[Any] = []

    def _capture(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return MagicMock()

    with patch("deep_agent.graph.create_deep_agent", side_effect=_capture), patch(
        "deep_agent.graph.build_checkpointer", return_value=MagicMock()
    ), patch("deep_agent.graph.build_store", return_value=MagicMock()), patch(
        "deep_agent.graph.MongoVfsBackend", return_value=MagicMock()
    ), patch("deep_agent.graph.get_llm", return_value=MagicMock()), patch(
        "deep_agent.graph.get_data_tools", return_value=[]
    ):
        graph_mod.build_graph()

    assert captured[0]["name"] == "deep-agent"


def test_TC_E_502_040_no_cache_kwarg() -> None:
    """build_graph does NOT pass a LangGraph node cache.

    Any semantic LLM cache is wired on the chat model
    instance via ChatModel.cache, not on the graph compile step.
    """
    from deep_agent import graph as graph_mod

    captured: list[Any] = []

    def _capture(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return MagicMock()

    with patch("deep_agent.graph.create_deep_agent", side_effect=_capture), patch(
        "deep_agent.graph.build_checkpointer", return_value=MagicMock()
    ), patch("deep_agent.graph.build_store", return_value=MagicMock()), patch(
        "deep_agent.graph.MongoVfsBackend", return_value=MagicMock()
    ), patch("deep_agent.graph.get_llm", return_value=MagicMock()), patch(
        "deep_agent.graph.get_data_tools", return_value=[]
    ):
        graph_mod.build_graph()

    assert captured[0].get("cache") is None or "cache" not in captured[0]


def test_TC_R501_034_permissions_passed() -> None:
    """permissions=[...] is passed."""
    from deep_agent import graph as graph_mod

    captured: list[Any] = []

    def _capture(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return MagicMock()

    with patch("deep_agent.graph.create_deep_agent", side_effect=_capture), patch(
        "deep_agent.graph.build_checkpointer", return_value=MagicMock()
    ), patch("deep_agent.graph.build_store", return_value=MagicMock()), patch(
        "deep_agent.graph.MongoVfsBackend", return_value=MagicMock()
    ), patch("deep_agent.graph.get_llm", return_value=MagicMock()), patch(
        "deep_agent.graph.get_data_tools", return_value=[]
    ):
        graph_mod.build_graph()

    perms = captured[0]["permissions"]
    assert isinstance(perms, list) and perms
    # At least one allow rule for /workspace/**, and a deny-all fallback.
    paths = [p for fp in perms for p in fp.paths]
    assert "/workspace/**" in paths
    assert "/**" in paths


def test_TC_R501_036_bedrock_registers_patch_dangling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PatchDanglingToolCallsMiddleware on Bedrock only."""
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")
    from deep_agent import config
    from deep_agent import graph as graph_mod
    from deep_agent.middleware.patch_dangling import (
        PatchDanglingToolCallsMiddleware,
    )

    config.get_settings.cache_clear()
    captured: list[Any] = []

    def _capture(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return MagicMock()

    with patch("deep_agent.graph.create_deep_agent", side_effect=_capture), patch(
        "deep_agent.graph.build_checkpointer", return_value=MagicMock()
    ), patch("deep_agent.graph.build_store", return_value=MagicMock()), patch(
        "deep_agent.graph.MongoVfsBackend", return_value=MagicMock()
    ), patch("deep_agent.graph.get_llm", return_value=MagicMock()), patch(
        "deep_agent.graph.get_data_tools", return_value=[]
    ):
        graph_mod.build_graph()

    mw = captured[0]["middleware"]
    assert any(isinstance(m, PatchDanglingToolCallsMiddleware) for m in mw)


def test_TC_R501_036_openai_skips_patch_dangling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_MODEL", "gpt-4o-mini")
    from deep_agent import config
    from deep_agent import graph as graph_mod
    from deep_agent.middleware.patch_dangling import (
        PatchDanglingToolCallsMiddleware,
    )

    config.get_settings.cache_clear()
    captured: list[Any] = []

    def _capture(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return MagicMock()

    with patch("deep_agent.graph.create_deep_agent", side_effect=_capture), patch(
        "deep_agent.graph.build_checkpointer", return_value=MagicMock()
    ), patch("deep_agent.graph.build_store", return_value=MagicMock()), patch(
        "deep_agent.graph.MongoVfsBackend", return_value=MagicMock()
    ), patch("deep_agent.graph.get_llm", return_value=MagicMock()), patch(
        "deep_agent.graph.get_data_tools", return_value=[]
    ):
        graph_mod.build_graph()

    mw = captured[0]["middleware"]
    assert not any(isinstance(m, PatchDanglingToolCallsMiddleware) for m in mw)


def test_TC_R501_035_no_interrupt_when_unset() -> None:
    """HITL_TOOLS empty → interrupt_on not passed (or None)."""
    from deep_agent import graph as graph_mod

    captured: list[Any] = []

    def _capture(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return MagicMock()

    with patch("deep_agent.graph.create_deep_agent", side_effect=_capture), patch(
        "deep_agent.graph.build_checkpointer", return_value=MagicMock()
    ), patch("deep_agent.graph.build_store", return_value=MagicMock()), patch(
        "deep_agent.graph.MongoVfsBackend", return_value=MagicMock()
    ), patch("deep_agent.graph.get_llm", return_value=MagicMock()), patch(
        "deep_agent.graph.get_data_tools", return_value=[]
    ):
        graph_mod.build_graph()

    assert "interrupt_on" not in captured[0] or captured[0]["interrupt_on"] is None


def test_TC_R501_035_interrupt_set_for_listed_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HITL_TOOLS=tool_a,tool_b → interrupt_on includes both."""
    monkeypatch.setenv("HITL_TOOLS", "draft_reg_e_response,escalate_to_fraud")
    from deep_agent import config
    from deep_agent import graph as graph_mod

    config.get_settings.cache_clear()
    captured: list[Any] = []

    def _capture(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return MagicMock()

    with patch("deep_agent.graph.create_deep_agent", side_effect=_capture), patch(
        "deep_agent.graph.build_checkpointer", return_value=MagicMock()
    ), patch("deep_agent.graph.build_store", return_value=MagicMock()), patch(
        "deep_agent.graph.MongoVfsBackend", return_value=MagicMock()
    ), patch("deep_agent.graph.get_llm", return_value=MagicMock()), patch(
        "deep_agent.graph.get_data_tools", return_value=[]
    ):
        graph_mod.build_graph()

    interrupt_on = captured[0]["interrupt_on"]
    assert set(interrupt_on.keys()) == {"draft_reg_e_response", "escalate_to_fraud"}
    for v in interrupt_on.values():
        assert v == {"allowed_decisions": ["approve", "edit", "reject"]}


# ─── deep-agents skill alignment ────────────────────────────


def _patch_graph_deps(stack: ExitStack, captured: list[Any]) -> None:
    """Common patches for build_graph kwarg-capture tests."""

    def _capture(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return MagicMock()

    stack.enter_context(
        patch("deep_agent.graph.create_deep_agent", side_effect=_capture)
    )
    stack.enter_context(
        patch("deep_agent.graph.build_checkpointer", return_value=MagicMock())
    )
    stack.enter_context(
        patch("deep_agent.graph.build_store", return_value=MagicMock())
    )
    stack.enter_context(
        patch("deep_agent.graph.get_llm", return_value=MagicMock())
    )
    stack.enter_context(
        patch("deep_agent.graph.get_data_tools", return_value=[])
    )


def test_TC_E_503_010_skill_files_exist() -> None:
    """Three SKILL.md files copied into AgentSkills/."""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    base = repo_root / "AgentSkills"
    for name in ("deep-agents-core", "deep-agents-memory", "deep-agents-orchestration"):
        skill_md = base / name / "SKILL.md"
        assert skill_md.is_file(), f"missing {skill_md}"
        head = skill_md.read_text(encoding="utf-8").splitlines()[0]
        assert head.startswith("---"), f"{skill_md} missing YAML frontmatter"


def test_TC_E_503_011_dockerfile_copies_agentskills() -> None:
    """Dockerfile must copy AgentSkills/ into the runtime image."""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    body = (repo_root / "Dockerfile").read_text(encoding="utf-8")
    assert "AgentSkills" in body, "Dockerfile must COPY AgentSkills into the image"


def test_TC_E_503_020_skills_passed_to_create_deep_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """build_graph passes skills=[<resolved path>]."""
    skills_dir = tmp_path / "AgentSkills"
    (skills_dir / "deep-agents-core").mkdir(parents=True)
    (skills_dir / "deep-agents-core" / "SKILL.md").write_text(
        "---\nname: x\ndescription: y\n---\n"
    )
    monkeypatch.setenv("AGENT_SKILLS_DIR", str(skills_dir))

    from deep_agent import config
    from deep_agent import graph as graph_mod

    config.get_settings.cache_clear()
    captured: list[Any] = []
    with ExitStack() as stack:
        _patch_graph_deps(stack, captured)
        stack.enter_context(
            patch("deep_agent.graph.MongoVfsBackend", return_value=MagicMock())
        )
        graph_mod.build_graph()

    assert captured[0].get("skills") == [str(skills_dir)]


def test_TC_E_503_021_subagent_has_skills(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """researcher SubAgent declares skills=[<dir>]."""
    skills_dir = tmp_path / "AgentSkills"
    skills_dir.mkdir()
    monkeypatch.setenv("AGENT_SKILLS_DIR", str(skills_dir))

    from deep_agent import config
    from deep_agent.agents.subagents import researcher_subagent

    config.get_settings.cache_clear()
    sub = researcher_subagent()
    assert sub.get("skills") == [str(skills_dir)]


def test_TC_E_503_022_relative_path_resolves_to_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """relative AGENT_SKILLS_DIR resolves under cwd."""
    (tmp_path / "AgentSkills").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_SKILLS_DIR", "AgentSkills")

    from deep_agent import config
    from deep_agent.graph import _resolve_skills_dir

    config.get_settings.cache_clear()
    resolved = _resolve_skills_dir()
    assert resolved == [str(tmp_path / "AgentSkills")]


def test_TC_E_503_023_missing_dir_falls_back_to_empty(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """missing AGENT_SKILLS_DIR -> [] and a warning logged."""
    monkeypatch.setenv("AGENT_SKILLS_DIR", "/nonexistent/spec503/path")

    from deep_agent import config
    from deep_agent.graph import _resolve_skills_dir

    config.get_settings.cache_clear()
    import logging

    with caplog.at_level(logging.WARNING, logger="deep_agent.graph"):
        resolved = _resolve_skills_dir()
    assert resolved == []
    assert any(
        "agent skills" in rec.message.lower() for rec in caplog.records
    ), "expected a warning log about the missing skills dir"


def test_TC_E_503_030_backend_is_composite() -> None:
    """build_graph passes a CompositeBackend with /memories/ route."""
    from deepagents.backends.composite import CompositeBackend

    from deep_agent import graph as graph_mod

    captured: list[Any] = []
    with ExitStack() as stack:
        _patch_graph_deps(stack, captured)
        graph_mod.build_graph()

    backend = captured[0]["backend"]
    assert isinstance(backend, CompositeBackend), (
        f"backend must be CompositeBackend, got {type(backend).__name__}"
    )
    assert "/memories/" in backend.routes


def test_TC_E_503_031_composite_uses_build_store_result() -> None:
    """StoreBackend on /memories/ uses the build_store() result."""
    from deepagents.backends.store import StoreBackend

    from deep_agent import graph as graph_mod

    sentinel_store = MagicMock(name="store_sentinel")
    captured: list[Any] = []
    with ExitStack() as stack:
        _patch_graph_deps(stack, captured)
        # Override build_store to return our sentinel.
        stack.enter_context(
            patch("deep_agent.graph.build_store", return_value=sentinel_store)
        )
        graph_mod.build_graph()

    backend = captured[0]["backend"]
    mem_backend = backend.routes.get("/memories/")
    assert isinstance(mem_backend, StoreBackend)
    # StoreBackend stores its explicit store in `_store`.
    assert mem_backend._store is sentinel_store


def test_TC_E_503_033_workspace_routes_to_default() -> None:
    """/workspace/** stays on the default MongoVfsBackend."""
    from deep_agent import graph as graph_mod
    from deep_agent.backends.mongo_backend import MongoVfsBackend

    captured: list[Any] = []
    with ExitStack() as stack:
        _patch_graph_deps(stack, captured)
        graph_mod.build_graph()

    backend = captured[0]["backend"]
    assert isinstance(backend.default, MongoVfsBackend)


def test_TC_E_503_040_uncheckpointed_passes_backend() -> None:
    """build_graph_uncheckpointed passes a backend."""
    from deep_agent import graph as graph_mod

    captured: list[Any] = []

    def _capture(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return MagicMock()

    with patch(
        "deep_agent.graph.create_deep_agent", side_effect=_capture
    ), patch("deep_agent.graph.get_llm", return_value=MagicMock()), patch(
        "deep_agent.graph.get_data_tools", return_value=[]
    ):
        graph_mod.build_graph_uncheckpointed()

    assert "backend" in captured[0] and captured[0]["backend"] is not None


def test_TC_E_503_050_main_prompt_mentions_stateless_subagents() -> None:
    """MAIN_PROMPT instructs one-shot subagent calls."""
    from deep_agent.prompts import MAIN_PROMPT

    lower = MAIN_PROMPT.lower()
    assert "stateless" in lower or "no memory of previous calls" in lower


def test_TC_E_503_032_cross_thread_memory_roundtrip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """write under T1+user, read under T2+same user — same content.

    This exercises the CompositeBackend wiring directly. We construct the
    composite the same way build_graph does, with mongomock as the store
    backing, and verify a /memories/ write under one thread is visible to
    a read under a different thread for the same user.
    """
    pytest.importorskip("mongomock")
    from deepagents.backends.composite import CompositeBackend
    from deepagents.backends.store import StoreBackend
    from langgraph.store.memory import InMemoryStore

    from deep_agent.backends.mongo_backend import MongoVfsBackend

    store = InMemoryStore()

    # Stub get_config so MongoVfsBackend default doesn't crash, and so the
    # namespace factory can read user_id when StoreBackend.read/write call.
    current_cfg: dict[str, Any] = {"configurable": {"user_id": "alice", "thread_id": "T1"}}

    monkeypatch.setattr(
        "deep_agent.backends.mongo_backend.get_config",
        lambda: current_cfg,
    )

    def _ns(_rt: Any) -> tuple[str, ...]:
        cfg = current_cfg
        uid = cfg["configurable"]["user_id"]
        return ("user", uid, "memories")

    composite = CompositeBackend(
        default=MongoVfsBackend(),
        routes={"/memories/": StoreBackend(store=store, namespace=_ns)},
    )

    composite.write("/memories/note.md", "hello cross-thread")

    # Switch the configurable thread; user stays the same.
    current_cfg["configurable"]["thread_id"] = "T2"
    result = composite.read("/memories/note.md")
    assert result.error is None, f"unexpected read error: {result.error}"
    file_data = result.file_data or {}
    content = file_data.get("content")
    if isinstance(content, list):
        content = "\n".join(content)
    assert "hello cross-thread" in (content or "")


# ─── writer subagent ────────────────────────────────────────


def test_TC_E_504_010_writer_factory_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """writer_subagent returns name=writer, WRITER_PROMPT bound."""
    skills_dir = tmp_path / "AgentSkills"
    skills_dir.mkdir()
    monkeypatch.setenv("AGENT_SKILLS_DIR", str(skills_dir))

    from deep_agent import config
    from deep_agent.agents.subagents import writer_subagent
    from deep_agent.prompts import WRITER_PROMPT

    config.get_settings.cache_clear()
    sub = writer_subagent()

    assert sub["name"] == "writer"
    assert sub["description"]
    assert sub["system_prompt"] == WRITER_PROMPT


def test_TC_E_504_011_writer_has_no_extra_tools() -> None:
    """writer declares tools=[] so it gets only default FS tools."""
    from deep_agent.agents.subagents import writer_subagent

    sub = writer_subagent()
    assert sub.get("tools") == []


def test_TC_E_504_012_writer_carries_skills(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """writer SubAgent declares skills=[<resolved AgentSkills>]."""
    skills_dir = tmp_path / "AgentSkills"
    skills_dir.mkdir()
    monkeypatch.setenv("AGENT_SKILLS_DIR", str(skills_dir))

    from deep_agent import config
    from deep_agent.agents.subagents import writer_subagent

    config.get_settings.cache_clear()
    sub = writer_subagent()
    assert sub.get("skills") == [str(skills_dir)]


def test_TC_E_504_013_writer_has_patch_dangling_middleware() -> None:
    """writer's middleware includes PatchDanglingToolCallsMiddleware."""
    from deep_agent.agents.subagents import writer_subagent
    from deep_agent.middleware.patch_dangling import (
        PatchDanglingToolCallsMiddleware,
    )

    sub = writer_subagent()
    mw = sub.get("middleware") or []
    assert sum(isinstance(m, PatchDanglingToolCallsMiddleware) for m in mw) == 1


def test_TC_E_504_020_writer_prompt_mentions_disciplines() -> None:
    """WRITER_PROMPT covers bundle, write_file, verbatim cites.

    The 'at most one tool_use' sub-clause was dropped — see
    test_TC_E_506_012 for the inverse assertion.
    """
    from deep_agent.prompts import WRITER_PROMPT

    body = WRITER_PROMPT.lower()
    assert "research bundle" in body
    assert "write_file" in body
    assert "verbatim" in body


def test_TC_E_504_022_writer_prompt_routes_to_workspace() -> None:
    """WRITER_PROMPT names /workspace/ as the artifact prefix."""
    from deep_agent.prompts import WRITER_PROMPT

    assert "/workspace/" in WRITER_PROMPT


def test_TC_E_504_030_build_graph_registers_both_subagents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """build_graph subagents= contains researcher AND writer."""
    skills_dir = tmp_path / "AgentSkills"
    skills_dir.mkdir()
    monkeypatch.setenv("AGENT_SKILLS_DIR", str(skills_dir))

    from deep_agent import config
    from deep_agent import graph as graph_mod

    config.get_settings.cache_clear()
    captured: list[Any] = []
    with ExitStack() as stack:
        _patch_graph_deps(stack, captured)
        graph_mod.build_graph()

    subagents = captured[0]["subagents"]
    names = {s["name"] for s in subagents}
    assert "researcher" in names
    assert "writer" in names


def test_TC_E_504_040_main_prompt_mentions_writer_and_workspace() -> None:
    """MAIN_PROMPT names the writer subagent and /workspace/."""
    from deep_agent.prompts import MAIN_PROMPT

    assert "writer" in MAIN_PROMPT.lower()
    assert "/workspace/" in MAIN_PROMPT


# ─── parallel tool calls ────────────────────────────────────


def test_TC_E_506_010_main_prompt_does_not_ban_parallel() -> None:
    """MAIN_PROMPT must NOT contain 'at most one tool_use'."""
    from deep_agent.prompts import MAIN_PROMPT

    assert "at most one tool_use" not in MAIN_PROMPT.lower()


def test_TC_E_506_011_researcher_prompt_does_not_ban_parallel() -> None:
    """RESEARCHER_PROMPT must NOT contain 'at most one tool_use'."""
    from deep_agent.prompts import RESEARCHER_PROMPT

    assert "at most one tool_use" not in RESEARCHER_PROMPT.lower()


def test_TC_E_506_012_writer_prompt_does_not_ban_parallel() -> None:
    """WRITER_PROMPT must NOT contain 'at most one tool_use'."""
    from deep_agent.prompts import WRITER_PROMPT

    assert "at most one tool_use" not in WRITER_PROMPT.lower()


def test_TC_E_506_020_main_prompt_enables_parallel() -> None:
    """MAIN_PROMPT explicitly invites parallel tool calls when independent."""
    from deep_agent.prompts import MAIN_PROMPT

    body = MAIN_PROMPT.lower()
    assert "parallel" in body
    assert "independent" in body


def test_TC_E_506_021_researcher_prompt_enables_parallel() -> None:
    """RESEARCHER_PROMPT invites parallel tool calls when independent."""
    from deep_agent.prompts import RESEARCHER_PROMPT

    body = RESEARCHER_PROMPT.lower()
    assert "parallel" in body
    assert "independent" in body


def test_TC_E_506_022_writer_prompt_enables_parallel() -> None:
    """WRITER_PROMPT invites parallel tool calls when independent."""
    from deep_agent.prompts import WRITER_PROMPT

    body = WRITER_PROMPT.lower()
    assert "parallel" in body
    assert "independent" in body


def test_TC_520_agent_log_single_build_under_thread_race() -> None:
    """_agent_log builds exactly one AgentLog even when many
    threads race the first call — lru_cache wasn't atomic; the double-checked
    lock is. cache_clear() is preserved for tests."""
    import threading

    from deep_agent import graph as graph_mod

    graph_mod._agent_log.cache_clear()
    calls = {"n": 0}
    built = MagicMock()

    def _one_build() -> Any:
        calls["n"] += 1
        return built

    with patch("deep_agent.graph._build_agent_log", side_effect=_one_build):
        results: list[Any] = []
        barrier = threading.Barrier(8)

        def _worker() -> None:
            barrier.wait()
            results.append(graph_mod._agent_log())

        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert calls["n"] == 1, "AgentLog built more than once under a thread race"
    assert all(r is built for r in results)
    graph_mod._agent_log.cache_clear()
