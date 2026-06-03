# Developer Guide

## Setup

```bash
uv sync --extra dev
cp .env.example .env
```

## Layout

```
src/deep_agent/
├── agents/         # 6 subagent factories (researcher, writer, deal_optimizer,
│                   #   loyalty_concierge, reorder_concierge, basket_cross_sell)
├── backends/       # deepagents BackendProtocol adapter -> VirtualFilesystem
├── ingestion/      # seed loader, change-stream worker, ASP helpers
├── middleware/     # custom AgentMiddleware (AgentLogMiddleware, ...)
├── persistence/    # MongoDB surfaces: saver, store, indexes, vector/graph stores
├── server/         # FastAPI app
├── tools/          # LangChain tools (KB + KG + web + fetch + safety-wrapped toolkit)
├── vfs/            # BlobStore protocol + S3 backend (GridFS removed)
├── cli.py
├── config.py
├── evals.py
├── graph.py        # build_graph() via deepagents.create_deep_agent
└── models.py       # get_llm / get_embeddings / get_reranker

examples/
└── retail_assistant/
    └── seeds/      # KB/KG/operational data fixtures (loaded by seed)

tests/
├── fixtures/       # evals_dataset.jsonl + other static fixtures
├── integration/    # gated on ATLAS_URI
└── unit/           # hermetic (405 tests; run `uv run pytest --collect-only`)

scripts/            # operator helpers (e.g. create_evals_dataset.py)
streaming/          # Kafka + producer + ASP pipeline
docs/               # this directory (+ docs/operators/ runbooks)
```

## Everyday commands

```bash
uv run pytest                                    # unit suite
uv run ruff check src tests streaming/producer.py
uv run mypy --strict src
uv run deep-agent chat --once "hi" --user me     # smoke-test the graph
uv run deep-agent serve --port 8000 --reload     # FastAPI with hot reload
uv run langgraph dev                             # Studio playground
```

## Quality gates (enforced in CI)

- `ruff check` clean across `src`, `tests`, `streaming/producer.py`
- `mypy --strict src` clean
- `test_TC_15_050_domain_isolated` - no industry vocabulary in core
- `test_TC_R501_no_gridfs_imports` - GridFS cannot return to `src/`
- `test_TC_R501_no_chat_history_or_plans_modules` -
  `persistence.chat_history`, `persistence.plans`, `middleware.plan` must
  remain unimportable
- `test_TC_R501_no_set_llm_cache_outside_comments` - the deprecated
  process-global LLM-cache swap is forbidden across `src/`
- `test_TC_R501_no_max_hops_arithmetic` - `max_hops` cannot return as a
  recursion-budget formula

All structural gates live in `tests/unit/test_quality_gates.py` so
`uv run pytest` fails on regression.

## Adding a tool

1. Put the tool in `src/deep_agent/tools/<name>.py`, decorate with
   `@langchain_core.tools.tool`.
2. Wrap any expensive backing client in `@lru_cache(maxsize=1)` and expose a
   module-level indirection so tests can monkeypatch.
3. Bind it into the main agent or the researcher subagent.
4. Write unit tests that patch the backing store.

## Adding a middleware

1. Subclass `langchain.agents.middleware.AgentMiddleware` under
   `src/deep_agent/middleware/<name>.py`.
2. Implement one or more hooks - `before_model`, `after_model`,
   `wrap_model_call`, `before_agent`, `after_agent`. Hooks must return
   state-update dicts or `None`; never raise.
3. Wrap external side effects (MongoDB writes, HTTP calls) in
   exception handlers so middleware failures never fail the user turn.
   `AgentLogMiddleware` logs + swallows `PyMongoError` as precedent.
4. Register the middleware in `build_graph()`'s `middleware=[...]` list.
5. Unit-test directly by constructing the middleware and calling its hook
   with a mock state dict and `runtime.config`. See
   `tests/unit/test_agent_log_integration.py` for the pattern (the
   in-tree `AgentLogMiddleware` was moved to the
   `langchain-mongodb-agent-log` package — read its tests
   for the worker-thread internals).

## Adding a VFS backend

S3 is the only shipping backend, but the abstraction is intentional —
adding e.g. Azure Blob or GCS is one new module + one parametrized test:

1. Implement `BlobStore` (`put` / `get` / `delete`) in
   `src/deep_agent/vfs/<name>_backend.py`.
2. Add the branch in `vfs/__init__.py:get_vfs()`.
3. Widen `Settings.vfs_backend` from `Literal["s3"]` to include the new
   tag, and add the matching env-var validation.
4. Parameterise the contract suite (`tests/unit/vfs_contract.py`) against
   the new backend.

## Switching LLM providers

`get_llm()` delegates to `init_chat_model(model_provider=..., ...)`. Flip via
env:

```bash
LLM_PROVIDER=openai
LLM_MODEL=gpt-4.1-mini
OPENAI_API_KEY=sk-...
```

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `ValueError: MONGODB_URI must enforce TLS` | Plain `mongodb://` without `tls=true`; set `ALLOW_INSECURE=true` in dev |
| `ValueError: VOYAGE_API_KEY is required` | `VOYAGE_API_KEY` not set; mandatory and the app fails fast at startup since `build_store()` always embeds via Voyage (not only when caching/search is on) |
| `OperationFailure: ... index ... exists` | Benign; `_safe_create_search_index` swallows duplicate-index errors |
| `web_search` raises `ToolDisabledError` | `TAVILY_API_KEY` not set |
| Data tool returns `QUERY REFUSED` | Pipeline violates one of the four safety rules |
| `VfsQuotaExceededError` | File larger than `VFS_MAX_BYTES` (50 MiB default) |
| Deep-agent stops early | Hit `RECURSION_LIMIT` (default 50); raise it or shorten prompts |
