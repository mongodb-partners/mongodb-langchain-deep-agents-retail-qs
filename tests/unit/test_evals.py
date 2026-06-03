"""Sub-phase 15: LangSmith evals runner."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clear_settings() -> None:
    from deep_agent import config

    config.get_settings.cache_clear()


def test_TC_15_010_evals_runs_client_evaluate() -> None:
    fake_client = MagicMock()
    fake_client.evaluate.return_value = MagicMock()
    fake_graph = MagicMock()

    with patch("deep_agent.evals.Client", return_value=fake_client), patch(
        "deep_agent.evals.build_graph", return_value=fake_graph
    ):
        from deep_agent.evals import run_evaluation

        run_evaluation(dataset_name="my-dataset")

    fake_client.evaluate.assert_called_once()
    _, kwargs = fake_client.evaluate.call_args
    assert kwargs["data"] == "my-dataset"


def test_TC_15_011_target_extracts_last_ai_message() -> None:
    from langchain_core.messages import AIMessage

    fake_graph = MagicMock()
    fake_graph.invoke.return_value = {"messages": [AIMessage(content="answer-text")]}

    with patch("deep_agent.evals.build_graph", return_value=fake_graph):
        from deep_agent.evals import _default_target

        target = _default_target()
        out = target({"message": "what?"})

    # The target now also returns observed tool calls; the answer is preserved.
    assert out["answer"] == "answer-text"
    assert out["tools"] == []


def test_TC_15_012_target_accepts_question_alias() -> None:
    from langchain_core.messages import AIMessage

    fake_graph = MagicMock()
    fake_graph.invoke.return_value = {"messages": [AIMessage(content="ok")]}
    with patch("deep_agent.evals.build_graph", return_value=fake_graph):
        from deep_agent.evals import _default_target

        assert _default_target()({"question": "q?"})["answer"] == "ok"


def test_TC_15_013_correctness_substring_match() -> None:
    from deep_agent.evals import _correctness_evaluator

    hit = MagicMock(outputs={"answer": "The capital is Paris."})
    expected_hit = MagicMock(outputs={"answer": "Paris"})
    assert _correctness_evaluator(hit, expected_hit).score == 1.0

    miss = MagicMock(outputs={"answer": "London"})
    expected_miss = MagicMock(outputs={"answer": "Paris"})
    assert _correctness_evaluator(miss, expected_miss).score == 0.0

    empty = MagicMock(outputs=None)
    assert _correctness_evaluator(empty, empty).score == 0.0


def test_TC_15_014_main_wires_argparse() -> None:
    with patch("deep_agent.evals.run_evaluation") as re:
        from deep_agent.evals import main

        rc = main(["--dataset", "my-ds", "--prefix", "exp"])

    assert rc == 0
    _, kwargs = re.call_args
    assert kwargs["dataset_name"] == "my-ds"
    assert kwargs["experiment_prefix"] == "exp"


# ─── Richer evaluators + tool trajectory + --model A/B ───────────


def test_TC_540_C02_target_returns_observed_tools() -> None:
    """The target reports observed tool-call names alongside the
    answer, without breaking the answer contract."""
    from langchain_core.messages import AIMessage

    fake_graph = MagicMock()
    ai_tools = AIMessage(
        content="",
        tool_calls=[{"name": "knowledge_base_hybrid_search", "args": {}, "id": "1"}],
    )
    final = AIMessage(content="here is the answer")
    fake_graph.invoke.return_value = {"messages": [ai_tools, final]}

    with patch("deep_agent.evals.build_graph", return_value=fake_graph):
        from deep_agent.evals import _default_target

        out = _default_target()({"message": "q"})

    assert out["answer"] == "here is the answer"
    assert out["tools"] == ["knowledge_base_hybrid_search"]


def test_TC_540_C01_llm_judge_scores_with_mocked_llm() -> None:
    """LLM-as-judge returns a clamped 0..1 score, and tolerates
    an unparseable response (deterministic via mock)."""
    from deep_agent.evals import _llm_judge_evaluator

    fake_llm = MagicMock()
    fake_llm.invoke.return_value = MagicMock(
        content='{"score": 0.9, "reasoning": "covers the key facts"}'
    )
    run = MagicMock(outputs={"answer": "Gold tier gets free delivery"})
    example = MagicMock(inputs={"message": "gold perks?"}, outputs={"answer": "free delivery"})

    with patch("deep_agent.models.get_llm", return_value=fake_llm):
        res = _llm_judge_evaluator(run, example)
    assert res.key == "correctness_judge"
    assert res.score == 0.9

    # Clamp: an out-of-range judge score is clamped into [0, 1].
    fake_llm.invoke.return_value = MagicMock(content='{"score": 1.7, "reasoning": "x"}')
    with patch("deep_agent.models.get_llm", return_value=fake_llm):
        assert _llm_judge_evaluator(run, example).score == 1.0
    fake_llm.invoke.return_value = MagicMock(content='{"score": -2.0, "reasoning": "x"}')
    with patch("deep_agent.models.get_llm", return_value=fake_llm):
        assert _llm_judge_evaluator(run, example).score == 0.0

    # Genuine parse failure (JSON present but score non-numeric) → except branch.
    fake_llm.invoke.return_value = MagicMock(content='{"score": "high"}')
    with patch("deep_agent.models.get_llm", return_value=fake_llm):
        bad = _llm_judge_evaluator(run, example)
    assert bad.score == 0.0
    assert bad.comment.startswith("unparseable judge response")

    # No JSON object at all → 0.0 via the no-match happy path, no crash.
    fake_llm.invoke.return_value = MagicMock(content="not json at all")
    with patch("deep_agent.models.get_llm", return_value=fake_llm):
        res2 = _llm_judge_evaluator(MagicMock(outputs=None), MagicMock(inputs={}, outputs=None))
    assert res2.score == 0.0


def test_TC_540_C03_trajectory_evaluator() -> None:
    """Trajectory scores expected_tools coverage; rows without
    expected_tools are skipped (score=None)."""
    from deep_agent.evals import _trajectory_evaluator

    full = _trajectory_evaluator(
        MagicMock(outputs={"tools": ["a", "b", "c"]}),
        MagicMock(outputs={"expected_tools": ["a", "b"]}),
    )
    assert full.score == 1.0

    partial = _trajectory_evaluator(
        MagicMock(outputs={"tools": ["a"]}),
        MagicMock(outputs={"expected_tools": ["a", "b"]}),
    )
    assert partial.score == 0.5

    skipped = _trajectory_evaluator(
        MagicMock(outputs={"tools": []}),
        MagicMock(outputs={"answer": "x"}),
    )
    assert skipped.score is None


def test_TC_540_C04_main_threads_model_flag() -> None:
    """--model is plumbed into run_evaluation."""
    with patch("deep_agent.evals.run_evaluation") as re_mock:
        from deep_agent.evals import main

        rc = main(["--dataset", "ds", "--model", "my-model-id"])
    assert rc == 0
    _, kwargs = re_mock.call_args
    assert kwargs["dataset_name"] == "ds"
    assert kwargs["model"] == "my-model-id"


def test_TC_540_C04_run_evaluation_model_suffixes_prefix() -> None:
    """A model override tags the experiment prefix for A/B view."""
    fake_client = MagicMock()
    with patch("deep_agent.evals.Client", return_value=fake_client), patch(
        "deep_agent.evals.build_graph", return_value=MagicMock()
    ):
        from deep_agent.evals import run_evaluation

        run_evaluation(dataset_name="d", experiment_prefix="exp", model="m1")
    _, kwargs = fake_client.evaluate.call_args
    assert kwargs["experiment_prefix"] == "exp-m1"
