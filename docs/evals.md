# Evals

The repo ships LangSmith-compatible eval datasets plus an idempotent uploader.
After uploading, `deep-agent-evals --dataset agent-cartsmith-retail-demo` runs
the compiled graph against every example and scores three evaluators per run:
a deterministic substring **correctness** gate, an **LLM-as-judge** correctness
grade, and a **tool-trajectory** check.

## What ships

| Path | Purpose |
|---|---|
| `tests/fixtures/retail_evals.jsonl` | The Agent Cartsmith showcase set (≥12 rows): coupon-stacking, catalog/price lookup, recipe ingredients, loyalty tiers/points, cart ops, savings math, memory recall, return policy, and a safety-refusal case. Tool-driven rows carry `expected_tools`. |
| `tests/fixtures/evals_dataset.jsonl` | A generic, domain-agnostic deep-agent smoke set (8 rows: KB, VFS, plan, safety-wrapper stories). |
| `scripts/create_evals_dataset.py` | Idempotent LangSmith uploader. Dedupes by `message`; uploads `expected_tools` when present. |
| `src/deep_agent/evals.py` | `run_evaluation()` + the `deep-agent-evals` console script (also `python -m deep_agent.evals`). |

`deep-agent-evals` is a **separate entry point** from the `deep-agent` CLI —
there is no `deep-agent evals` subcommand.

Row schema: `{"message": str, "answer": str, "expected_tools"?: [str]}`. The
`answer` is a substring a correct response must contain (the substring gate);
`expected_tools` (optional) lists tools the agent should invoke (the trajectory
evaluator). Rows without `expected_tools` skip the trajectory metric.

## Prerequisites

- `LANGSMITH_API_KEY` set in the environment (or `.env`)
- A LangSmith workspace the key can write to

## Upload

The default uploads the retail showcase set under `agent-cartsmith-retail-demo`:

```bash
uv run python scripts/create_evals_dataset.py
```

Output:

```json
{
  "dataset_id": "...",
  "dataset_name": "agent-cartsmith-retail-demo",
  "already_present": 0,
  "created": 13
}
```

Re-running is safe — already-uploaded rows (matched by `message`) are skipped.

Upload the generic smoke set instead, or any custom fixture:

```bash
uv run python scripts/create_evals_dataset.py \
  --name deep_agent_starter \
  --fixture tests/fixtures/evals_dataset.jsonl \
  --description "Deep-agent smoke-test Q&A set"
```

## Run an evaluation

```bash
uv run deep-agent-evals --dataset agent-cartsmith-retail-demo
# or: uv run python -m deep_agent.evals --dataset agent-cartsmith-retail-demo
```

Flags:

| Flag | Default |
|---|---|
| `--dataset NAME` | _(required)_ — LangSmith dataset name |
| `--prefix STR` | `agent-cartsmith-retail-demo` — experiment name prefix |
| `--model ID` | _(none)_ — model override; reruns under a model-tagged prefix for A/B comparison |

`run_evaluation` builds the graph via `build_graph()`, so MongoDBSaver +
MongoDBStore + the response cache are all active during the run. The
response cache can short-circuit the LLM on repeated identical queries; clear it
between runs if you need deterministic LLM invocation timing:

```bash
uv run python -c "
from deep_agent.persistence.mongo import get_db
from deep_agent.config import get_settings
s = get_settings()
get_db()[s.response_cache_collection].delete_many({})
"
```

### A/B experiment comparison

Run the same dataset under two models to get LangSmith's side-by-side
experiment view:

```bash
uv run deep-agent-evals --dataset agent-cartsmith-retail-demo --model global.anthropic.claude-haiku-4-5-20251001-v1:0
uv run deep-agent-evals --dataset agent-cartsmith-retail-demo --model global.anthropic.claude-sonnet-4-6
```

Each run is tagged `agent-cartsmith-retail-demo-<model>`; compare them in the
LangSmith **Experiments** tab.

## The evaluators

`src/deep_agent/evals.py` wires three evaluators into `client.evaluate`:

| Key | What it scores |
|---|---|
| `correctness` | Deterministic, case-insensitive substring match of the example `answer` in the agent output. Cheap CI gate. |
| `correctness_judge` | LLM-as-judge (0.0–1.0) grading the answer against the reference using the project's configured Bedrock model. No extra dependency. |
| `tool_trajectory` | Fraction of an example's `expected_tools` that were actually invoked during the run. Skipped (no score) when a row has no `expected_tools`. |

> **Scope:** `tool_trajectory` observes only **main-agent** tool calls (the
> tools on the planner: `knowledge_base_*`, `mongodb_query`, the cart tools,
> `place_order`, `remember_fact`/`recall_memories`). Tools that live only on a
> subagent (e.g. `savings_calculator` on `deal_optimizer`) are invoked behind
> the `task` delegation and are **not** visible in the parent run's messages, so
> don't list them in `expected_tools` — let `correctness_judge` grade those rows.

The substring gate stays deterministic for CI; the judge and trajectory
evaluators add depth for the showcase. To swap in your own evaluator, pass a
custom list to `Client.evaluate`:

```python
from langsmith import Client
from langsmith.evaluation import EvaluationResult

from deep_agent.evals import _default_target

def custom_eval(run, example) -> EvaluationResult:
    ...  # your scoring here
    return EvaluationResult(key="my_metric", score=...)

Client().evaluate(
    _default_target(),
    data="agent-cartsmith-retail-demo",
    evaluators=[custom_eval],
    experiment_prefix="agent-cartsmith-retail-demo-custom",
)
```

`_default_target()` reads `message` (or `question`) from inputs, invokes the
graph, and returns `{"answer": <last AIMessage content>, "tools": [<tool names>]}`.

## Tests

`tests/unit/test_evals_dataset.py`:

- `TC-18-010`: the generic fixture is valid JSONL with `message` + `answer` on every row
- `TC-540-C05`: the retail fixture has ≥12 valid rows and well-formed `expected_tools`; the uploader passes `expected_tools` into example outputs
- `TC-18-020` / `TC-18-021`: uploader creates a dataset when missing; dedupes on `message`
- `TC-18-030`: `main()` parses `--name` / `--fixture` and prints the JSON summary

`tests/unit/test_evals.py`:

- `TC-15-010..014`: `run_evaluation` wires to `Client.evaluate`; the target accepts `message`/`question`; the substring evaluator handles hits, misses, missing outputs
- `TC-540-C01`: the LLM judge returns a clamped 0–1 score (mocked LLM) and tolerates unparseable output
- `TC-540-C02`: the target reports observed tool calls alongside the answer
- `TC-540-C03`: the trajectory evaluator scores coverage and skips rows without `expected_tools`
- `TC-540-C04`: `--model` threads through and tags the experiment prefix

## Extending a dataset

Append JSONL rows (only new `message` values create examples on re-upload):

```jsonl
{"message": "What does AgentLogMiddleware do?", "answer": "after_model"}
{"message": "Add a gallon of milk to my cart", "answer": "cart", "expected_tools": ["add_to_cart"]}
```

Keep `answer` fields as substrings a correct response must contain. Over-specific
answers (exact prose) score poorly; too-generic answers (a single common word)
false-positive. Add `expected_tools` only when a specific tool should fire.
