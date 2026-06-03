# Verification Runbook — Agent Cartsmith Retail Shopping Assistant

Run top to bottom against a fresh `MONGODB_DB=agent_cartsmith_retail_demo` to
confirm an end-to-end deployment is healthy.

## Data + indexes

- [ ] `PROVISION_INDEXES_ON_BOOT=true uv run python -c "from deep_agent.persistence.indexes import ensure_indexes; ensure_indexes()"`
      completes; in Atlas the `knowledge_base` Vector + Search indexes and the
      `agent_log` Vector/Search indexes report **READY**.
- [ ] `uv run deep-agent seed` completes. In Compass:
  - `products` (1505), `customers` (10), `orders` (18) populated;
  - `knowledge_base` (9 docs); `knowledge_graph` populated.
- [ ] Seeding is idempotent — re-running does not duplicate KB/KG rows or raise
      on operational `_id`s.

## Backend

- [ ] `uv run deep-agent chat --once "Find pasta ingredients for 4 and their prices" --user demo`
      returns a sensible retail answer (KB recipe + `products` prices, calls out
      any sale items).
- [ ] `uv run deep-agent serve --port 8000` boots. (Index DDL does **not** run on
      boot unless `PROVISION_INDEXES_ON_BOOT=true` — the runtime role needs no
      CREATE_INDEX.)
- [ ] `POST /api/chat` returns an SSE stream (`event: token` … `event: done`).
- [ ] Data-agent safety: with `DATA_AGENT_ALLOW_LIST=products,customers,orders`,
      a query on `products` works; an unset allow-list **refuses every
      collection** (fail-closed); `mongodb_schema` on `long_term_memory` is
      refused.
- [ ] After a `write_file` turn,
      `GET /api/files?user_id=demo&thread_id=<sub>` returns the saved file with
      a `size` field.

## Frontend

- [ ] `cd frontend && npm install && npm run build` passes (tsc + vite).
- [ ] `npm run dev`: the storefront loads — hero "Shop smarter with Agent Cartsmith",
      25/6/7 stat counters (intentional display constants, not seed counts),
      11 preset cards, the model badge, the floating
      **Ask your assistant** launcher.
- [ ] Clicking a preset card opens the chat panel and streams a response (the
      storefront streams immediately — there is no domain-selection gate).
- [ ] Pasta query: the panel shows the agent calling
      `knowledge_base_hybrid_search` + `mongodb_query` + `write_file`; **Files
      Saved** lists the shopping list.
- [ ] A dropped/truncated stream surfaces an error (not a silent success); the
      chat aborts cleanly on **New** conversation.

## Observability + evals

- [ ] A LangSmith trace appears in project `agent-cartsmith-retail-demo` within ~30 s
      of a chat turn (nested planner → tool → subagent → LLM spans).
- [ ] `uv run python scripts/create_evals_dataset.py --name agent-cartsmith-retail-demo --fixture tests/fixtures/retail_evals.jsonl` succeeds.
- [ ] `uv run python -m deep_agent.evals --dataset agent-cartsmith-retail-demo`
      completes; results visible under the dataset in LangSmith.

## CI / quality

- [ ] `uv run pytest` green (integration tier skipped by default; confirm the
      exact count via `uv run pytest --collect-only`).
- [ ] `uv run ruff check src tests streaming/producer.py` clean; `uv run mypy --strict src` clean.
