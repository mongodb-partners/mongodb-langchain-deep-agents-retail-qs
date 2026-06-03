"""LangSmith-powered evaluation runner.

Usage:

    deep-agent-evals --dataset my-dataset [--model <id>] [--prefix <prefix>]

The graph is wrapped in a thin callable that accepts ``{"message": str}``
inputs and returns ``{"answer": str, "tools": list[str]}`` — the answer is the
last AI message and ``tools`` are the tool-call names observed during the run.

Three evaluators run by default:

* ``correctness`` — a cheap, deterministic case-insensitive substring gate.
* ``correctness_judge`` — an LLM-as-judge grade (0..1) using the configured
  Bedrock model (no extra dependency; mockable in tests).
* ``tool_trajectory`` — scores whether an example's optional ``expected_tools``
  were invoked. Examples without ``expected_tools`` are skipped.

``--model`` reruns the same dataset under a model override (and a model-tagged
experiment prefix) so LangSmith's side-by-side experiment comparison lights up.
"""
from __future__ import annotations

import argparse
import json
import re
import uuid
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langsmith import Client
from langsmith.evaluation import EvaluationResult

from .graph import build_graph


def _observed_tools(messages: list[Any]) -> list[str]:
    """Collect tool-call names from a run's messages, in invocation order."""
    names: list[str] = []
    for m in messages:
        for tc in getattr(m, "tool_calls", None) or []:
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
            if name:
                names.append(str(name))
    return names


def _default_target(model: str | None = None) -> Any:
    """Return a dataset-compatible target callable.

    ``{"message" | "question": str} -> {"answer": str, "tools": list[str]}``.
    """
    graph = build_graph(model=model)

    def target(inputs: dict[str, Any]) -> dict[str, Any]:
        message = inputs.get("message") or inputs.get("question") or ""
        # Isolate every example on its own checkpoint thread so the shared
        # MongoDBSaver doesn't leak one example's conversation into the next.
        thread_id = f"evals-{uuid.uuid4().hex}"
        state = graph.invoke(
            {
                "messages": [HumanMessage(content=str(message))],
                "user_id": "evals",
            },
            config={"configurable": {"thread_id": thread_id, "user_id": "evals"}},
        )
        msgs = state.get("messages", []) if isinstance(state, dict) else []
        answer = ""
        for m in reversed(msgs):
            if isinstance(m, AIMessage):
                answer = str(m.content)
                break
        return {"answer": answer, "tools": _observed_tools(msgs)}

    return target


def _correctness_evaluator(run: Any, example: Any) -> EvaluationResult:
    """Cheap deterministic gate: case-insensitive substring match on ``answer``."""
    expected = (example.outputs or {}).get("answer", "") if example.outputs else ""
    actual = (run.outputs or {}).get("answer", "") if run.outputs else ""
    score = 1.0 if expected and expected.strip().lower() in actual.lower() else 0.0
    return EvaluationResult(key="correctness", score=score)


def _trajectory_evaluator(run: Any, example: Any) -> EvaluationResult:
    """Score whether an example's ``expected_tools`` were all invoked.

    Rows without ``expected_tools`` are skipped (``score=None``) so answer-only
    examples don't drag the metric down."""
    expected = (
        list((example.outputs or {}).get("expected_tools") or []) if example.outputs else []
    )
    if not expected:
        return EvaluationResult(key="tool_trajectory", score=None, comment="no expected_tools")
    observed = set((run.outputs or {}).get("tools") or []) if run.outputs else set()
    hits = sum(1 for t in expected if t in observed)
    return EvaluationResult(
        key="tool_trajectory",
        score=hits / len(expected),
        comment=f"expected {expected}; observed {sorted(observed)}",
    )


_JUDGE_PROMPT = """You are grading a retail shopping assistant's answer.

User question:
{question}

Reference answer (key facts a correct response must convey):
{reference}

Assistant answer:
{answer}

Score how well the assistant answer matches the reference on a 0.0-1.0 scale
(1.0 = fully correct and grounded; 0.0 = wrong or missing the key facts).
Respond with ONLY a JSON object of the form
{{"score": <float between 0 and 1>, "reasoning": "<one short sentence>"}}."""


def _parse_judge_score(text: str) -> tuple[float, str]:
    """Extract ``{score, reasoning}`` from a judge response; clamp to [0, 1]."""
    try:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        data = json.loads(match.group(0)) if match else {}
        score = float(data.get("score", 0.0))
        reasoning = str(data.get("reasoning", ""))
    except (ValueError, TypeError, AttributeError):
        return 0.0, f"unparseable judge response: {text[:120]!r}"
    return max(0.0, min(1.0, score)), reasoning


def _llm_judge_evaluator(run: Any, example: Any) -> EvaluationResult:
    """LLM-as-judge correctness grade using the project's configured Bedrock model.

    Lazy-imports ``get_llm`` so importing this module stays cheap; unit tests
    patch ``deep_agent.models.get_llm`` for determinism.
    """
    from .models import get_llm

    inputs = example.inputs or {}
    question = inputs.get("message") or inputs.get("question") or ""
    reference = (example.outputs or {}).get("answer", "") if example.outputs else ""
    answer = (run.outputs or {}).get("answer", "") if run.outputs else ""
    prompt = _JUDGE_PROMPT.format(question=question, reference=reference, answer=answer)
    resp = get_llm().invoke(prompt)
    text = str(getattr(resp, "content", resp))
    score, reasoning = _parse_judge_score(text)
    return EvaluationResult(key="correctness_judge", score=score, comment=reasoning)


# Evaluators applied to every run. The substring gate is deterministic (CI
# safe); the judge and trajectory evaluators add depth for the showcase.
DEFAULT_EVALUATORS = [_correctness_evaluator, _llm_judge_evaluator, _trajectory_evaluator]


def run_evaluation(
    *,
    dataset_name: str,
    experiment_prefix: str = "agent-cartsmith-retail-demo",
    model: str | None = None,
) -> Any:
    """Run the graph against a LangSmith dataset and return the experiment results.

    When ``model`` is given, the graph uses that model override and the
    experiment prefix is suffixed with the model id so two runs are easy to
    compare side-by-side in LangSmith.
    """
    client = Client()
    prefix = f"{experiment_prefix}-{model}" if model else experiment_prefix
    return client.evaluate(
        _default_target(model=model),
        data=dataset_name,
        evaluators=DEFAULT_EVALUATORS,
        experiment_prefix=prefix,
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="deep-agent-evals")
    p.add_argument("--dataset", required=True, help="LangSmith dataset name or ID")
    p.add_argument(
        "--prefix", default="agent-cartsmith-retail-demo", help="Experiment name prefix"
    )
    p.add_argument(
        "--model",
        default=None,
        help="Model id override (e.g. a Bedrock profile) for A/B experiment comparison",
    )
    args = p.parse_args(argv)
    run_evaluation(
        dataset_name=args.dataset, experiment_prefix=args.prefix, model=args.model
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
