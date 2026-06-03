# LangSmith walkthrough (tracing + feedback + evals)

A guide to the app's LangSmith integration on the **Docker** deployment.
LangSmith is used purely for **observability and evals** — the app itself runs
locally / in Docker (there is no LangGraph Platform deployment).

Three capabilities, in order: a **trace**, a **feedback** score landing on that
trace, and an **eval experiment** (including a model A/B comparison).

## 0. One-time setup

Set these in `.env` (the LangSmith SDK reads them directly from the process
environment):

```bash
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=lsv2_...
LANGSMITH_PROJECT=agent-cartsmith-retail-demo
```

Tracing is **env-only** — no decorators or code changes. With the key set,
every `/chat` turn is auto-traced. Start the stack:

```bash
uv run deep-agent serve --port 8000
cd frontend && npm run dev            # storefront at the proxied UI
```

## 1. A nested trace

1. In the storefront, run preset #1 (the pasta query) or type:
   *"Find pasta ingredients for 4 and their prices."*
2. Open the run in LangSmith (**Projects → `agent-cartsmith-retail-demo`**,
   newest run). The nested span tree shows:
   - the **planner / deep-agent** root,
   - the **subagent** hops (`researcher`, `deal_optimizer`, …),
   - the **tool** spans — `knowledge_base_hybrid_search`, `mongodb_query` on
     `products`, `write_file`.
3. Each run is also tagged `correlation_id:<uuid>` — the same id the chat stream
   emits on its leading `correlation` frame, so you can find a user's exact turn
   by correlation id.

## 2. Feedback → `user_score` on the trace

A rating in the UI lands on the **same** run you just opened.

1. On the assistant message, click 👎 (or 👍) and add a comment, or type
   `/feedback down too verbose`.
2. The backend does two things (`POST /api/feedback`):
   - **always** writes the rating to the Mongo `feedback` collection (durable,
     independent of LangSmith);
   - when `LANGSMITH_TRACING=true`, mirrors a `user_score` feedback onto the
     LangSmith run.
3. Refresh the run in LangSmith → the **Feedback** panel now shows `user_score`
   with your comment.

> Why this works: the turn's `run_id` is pinned to the `correlation_id` the
> frontend round-trips (`RunnableConfig["run_id"] = uuid.UUID(cid)`), so the
> mirrored feedback attaches to the real trace instead of a phantom run.
> *(Cache note: a turn served from the semantic response cache bypasses the
> graph and produces no new trace, so feedback on a cached reply won't mirror —
> use a fresh query, or clear the response cache.)*

## 3. An eval experiment

Upload the dataset (idempotent; `agent-cartsmith-retail-demo` is the default
name and fixture):

```bash
uv run python scripts/create_evals_dataset.py
```

Run the graph against it:

```bash
uv run deep-agent-evals --dataset agent-cartsmith-retail-demo
```

Open the experiment in LangSmith. Three metrics per row:

- **`correctness`** — deterministic substring gate (cheap, CI-safe).
- **`correctness_judge`** — LLM-as-judge grade (0–1) on the Bedrock model.
- **`tool_trajectory`** — did the agent call the row's `expected_tools`
  (e.g. `add_to_cart`, `savings_calculator`, `knowledge_base_hybrid_search`)?

A row like the cart or savings case shows the trajectory metric confirming the
agent actually used the right tool — the *deep-agent behavior*, not just the
final prose.

## 4. A/B model comparison

Run the same dataset under two models:

```bash
uv run deep-agent-evals --dataset agent-cartsmith-retail-demo --model global.anthropic.claude-haiku-4-5-20251001-v1:0
uv run deep-agent-evals --dataset agent-cartsmith-retail-demo --model global.anthropic.claude-sonnet-4-6
```

Each run is tagged `agent-cartsmith-retail-demo-<model>`. Open the LangSmith
**Experiments** tab and select both to get the side-by-side comparison —
correctness, judge score, tool-trajectory, and latency per model.

## Online evals (optional)

Once feedback lands on real traces (step 2), you can attach a LangSmith **online
evaluator** (e.g. an automatic LLM-judge) to the live
`agent-cartsmith-retail-demo` project from the LangSmith UI to score production
turns continuously. No code change is required.

See [evals.md](evals.md) for dataset schema, the evaluator implementations, and
how to add your own.
