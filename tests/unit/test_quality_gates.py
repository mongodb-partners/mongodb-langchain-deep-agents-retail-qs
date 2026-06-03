"""Lint, typecheck, and domain-isolation quality gates."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_TC_15_040_ruff_clean() -> None:
    if shutil.which("ruff") is None:
        pytest.skip("ruff not installed")
    r = subprocess.run(
        ["ruff", "check", "src", "tests", "streaming/producer.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"ruff failed:\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"


def test_TC_15_041_mypy_strict_clean() -> None:
    """``mypy --strict`` must be clean across ``src/``."""
    import sys

    r = subprocess.run(
        [sys.executable, "-m", "mypy", "--strict", "src"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if "No module named mypy" in r.stderr:
        pytest.skip("mypy not installed in venv")
    assert r.returncode == 0, f"mypy failed:\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}"


def test_TC_15_050_domain_isolated() -> None:
    """Core modules must not import industry-specific terms.

    Domains are gone from the reference repo. This gate keeps the rule
    in place so vertical-specific vocab can't sneak back into
    ``src/deep_agent/``.
    """
    forbidden = [r"\btelco\b", r"\bgep\b", r"\bfinance\b", r"\bhealthcare\b", r"\bphi\b"]
    core_roots = [REPO_ROOT / "src" / "deep_agent"]
    offenders: list[str] = []
    for root in core_roots:
        for py in root.rglob("*.py"):
            rel = py.relative_to(REPO_ROOT)
            text = py.read_text(encoding="utf-8").lower()
            for pat in forbidden:
                if re.search(pat, text):
                    offenders.append(f"{rel}: matched /{pat}/")
    assert not offenders, "core code leaked industry-specific terms:\n" + "\n".join(offenders)


def test_TC_R501_no_domains_directory() -> None:
    """The ``domains/`` directory is gone."""
    assert not (REPO_ROOT / "domains").exists()


def test_TC_R501_no_domain_pack_module() -> None:
    """domain_pack and agents/_config are deleted."""
    import importlib

    for name in (
        "deep_agent.domain_pack",
        "deep_agent.agents._config",
        "deep_agent.server.domain",
    ):
        with pytest.raises(ImportError):
            importlib.import_module(name)


def test_TC_R501_build_graph_no_params() -> None:
    """build_graph() takes no domain parameter.

    A single optional ``model`` kwarg is carved out, used by the
    server's per-request graph cache. The original "no parameters"
    invariant was about preventing a ``domain`` parameter — that ban
    still holds; the gate now enforces parameters are at most
    ``{"model"}`` and ``model`` is optional.
    """
    import inspect

    from deep_agent.graph import build_graph

    params = inspect.signature(build_graph).parameters
    allowed = {"model"}
    assert set(params.keys()) <= allowed, (
        f"build_graph must only accept {allowed}; got {set(params.keys())}"
    )
    if "model" in params:
        # Must be optional so existing callers (build_graph()) still work.
        assert params["model"].default is None


def test_TC_R501_build_graph_no_lru_cache() -> None:
    """build_graph is not lru_cache-decorated."""
    from deep_agent.graph import build_graph

    assert not hasattr(build_graph, "cache_info")


def test_TC_R501_no_x_domain_string() -> None:
    """The X-Domain header is gone from src/."""
    src_root = REPO_ROOT / "src" / "deep_agent"
    for py in src_root.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        assert "X-Domain" not in text, f"{py}: still references X-Domain"


def test_TC_R501_settings_no_max_hops() -> None:
    """Settings.max_hops is deleted."""
    from deep_agent.config import Settings

    assert not hasattr(Settings(), "max_hops")






# --- LLM cache retired: structural gates ----------


def test_TC_540_B05_no_set_llm_cache_in_graph() -> None:
    """The deprecated set_llm_cache process-global swap must not creep back into
    graph.py (it caused a ~5x wall-time regression under concurrent traffic).
    It remains banned after the legacy prompt-level cache was removed."""
    src = (REPO_ROOT / "src" / "deep_agent" / "graph.py").read_text(encoding="utf-8")
    assert "set_llm_cache" not in src


def test_TC_540_B05_no_inmemory_cache_in_graph() -> None:
    """build_graph must not wire LangGraph's InMemoryCache."""
    src = (REPO_ROOT / "src" / "deep_agent" / "graph.py").read_text(encoding="utf-8")
    assert "InMemoryCache" not in src
    assert "from langgraph.cache" not in src


def test_TC_540_B05_no_llm_cache_identifiers_in_src() -> None:
    """The legacy prompt-level cache is fully retired — no related identifier
    remains anywhere under src/.

    Targets the cache's distinctive identifiers (not the bare substring
    ``llm_cache``, which also appears inside the still-banned-but-mentionable
    ``set_llm_cache`` anti-pattern, guarded separately below)."""
    banned = (
        "enable_llm_cache",
        "semantic_cache_threshold",
        "llm_cache_collection",
        "llm_cache_vector_index",
        "llm_cache_semantic_index",
        "MongoDBAtlasSemanticCache",
        "build_llm_cache",
        '"llm_cache"',
    )
    offenders: list[str] = []
    for path in _iter_src_py():
        text = path.read_text(encoding="utf-8")
        for token in banned:
            if token in text:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {token}")
    assert not offenders, "Spec-502 llm_cache references remain in src/:\n" + "\n".join(offenders)


# --- structural gates against deleted infrastructure -----------


def _iter_src_py() -> list[Path]:
    return sorted((REPO_ROOT / "src" / "deep_agent").rglob("*.py"))


def test_TC_R501_no_gridfs_imports() -> None:
    """GridFS is gone. No module under src/ may import it (an audit found
    GridFS could re-enter through a pymongo helper if a future refactor
    re-introduced it).
    """
    offenders: list[str] = []
    for py in _iter_src_py():
        for line in py.read_text(encoding="utf-8").splitlines():
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if (
                stripped.startswith("import gridfs")
                or stripped.startswith("from gridfs")
                or "pymongo.gridfs" in stripped
            ):
                offenders.append(f"{py.relative_to(REPO_ROOT)}: {line.strip()}")
    assert not offenders, "GridFS leaked back into src/:\n" + "\n".join(offenders)


def test_TC_R501_no_chat_history_or_plans_modules() -> None:
    """persistence/chat_history.py and persistence/plans.py and
    middleware/plan.py were replaced by CheckpointMirrorMiddleware.
    They must not return as importable modules.
    """
    import importlib

    for name in (
        "deep_agent.persistence.chat_history",
        "deep_agent.persistence.plans",
        "deep_agent.middleware.plan",
    ):
        with pytest.raises(ImportError):
            importlib.import_module(name)


def test_TC_510_008a_no_checkpoint_mirror_middleware_module() -> None:
    """The custom ``CheckpointMirrorMiddleware`` was extracted
    to the standalone ``langchain-mongodb-agent-log`` package. The
    in-repo module must not return.
    """
    import importlib

    with pytest.raises(ImportError):
        importlib.import_module("deep_agent.middleware.checkpoint_mirror")


def test_TC_510_008b_no_custom_search_past_conversations_module() -> None:
    """The custom search-past-conversations tool was replaced
    by ``langchain_mongodb_agent_log.retrieval.tool.build_tool``. The
    in-repo module must not return.
    """
    import importlib

    with pytest.raises(ImportError):
        importlib.import_module("deep_agent.tools.search_past_conversations")


def test_TC_R501_no_set_llm_cache_outside_comments() -> None:
    """The deprecated process-global ``set_llm_cache`` swap is
    forbidden across src/, not just graph.py. Comment / docstring
    mentions explaining *why* it was removed are allowed; an actual
    callable reference is not.
    """
    import re

    call_pattern = re.compile(r"\bset_llm_cache\s*\(")
    offenders: list[str] = []
    for py in _iter_src_py():
        text = py.read_text(encoding="utf-8")
        if call_pattern.search(text):
            offenders.append(str(py.relative_to(REPO_ROOT)))
    assert not offenders, (
        "set_llm_cache(...) call leaked into src/:\n" + "\n".join(offenders)
    )


def test_TC_R501_no_max_hops_arithmetic() -> None:
    """``max_hops`` is gone; nothing may compute
    a recursion budget from it (e.g. ``4 * max_hops``). Catches a stale
    formula re-emerging in a refactor. Comment-only mentions explaining
    the removal are allowed.
    """
    offenders: list[str] = []
    for py in _iter_src_py():
        for line in py.read_text(encoding="utf-8").splitlines():
            stripped = line.lstrip()
            if stripped.startswith("#") or stripped.startswith('"""'):
                continue
            if "max_hops" in stripped:
                offenders.append(f"{py.relative_to(REPO_ROOT)}: {line.strip()}")
    assert not offenders, "max_hops re-introduced in src/:\n" + "\n".join(offenders)


def test_TC_510_main_prompt_documents_episodic_vs_semantic() -> None:
    """MAIN_PROMPT must teach the model the difference
    between search_past_conversations (episodic recall over the agent_log) and
    recall_memories (durable per-user semantic facts). Guards the guidance from
    silently dropping."""
    from deep_agent.prompts import MAIN_PROMPT

    assert "search_past_conversations" in MAIN_PROMPT
    assert "recall_memories" in MAIN_PROMPT


def test_TC_520_agent_skills_corpus_is_tracked() -> None:
    """The SKILL.md corpus the Dockerfile COPYs must be tracked in
    git so a fresh clone / docker build succeeds (a bare AgentSkills/ ignore
    tracked zero files)."""
    out = subprocess.run(
        ["git", "ls-files", "AgentSkills"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    tracked = [line for line in out.stdout.splitlines() if line.endswith("SKILL.md")]
    assert tracked, "no AgentSkills/**/SKILL.md tracked — docker build fails on a fresh clone"


def test_TC_520_deep_agent_evals_console_script_registered() -> None:
    """Docs reference `deep-agent-evals`; it must be a real console script
    (not command-not-found)."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "deep-agent-evals" in pyproject
    from deep_agent.evals import main  # the entry point target must import

    assert callable(main)


def test_TC_520_claude_md_no_deleted_spec510_symbols() -> None:
    """CLAUDE.md must not point contributors at the deleted in-repo surfaces
    as if they were live (the exact class/collection names).

    CLAUDE.md is gitignored (local workspace guidance), so skip when absent —
    the gate still guards it from regressing wherever it exists."""
    claude_md = REPO_ROOT / "CLAUDE.md"
    if not claude_md.exists():
        pytest.skip("CLAUDE.md not present (gitignored local guidance)")
    text = claude_md.read_text(encoding="utf-8")
    assert "CheckpointMirrorMiddleware" not in text
    assert "checkpoint_mirror" not in text


def test_TC_520_eval_dataset_no_deleted_feature_answers() -> None:
    """The eval golden answers must not reward citing removed features
    (GridFS, the `plans` collection)."""
    ds = (REPO_ROOT / "tests/fixtures/evals_dataset.jsonl").read_text(encoding="utf-8")
    assert "GridFS" not in ds
    assert '"answer": "plans"' not in ds
