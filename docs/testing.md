# Testing

Two-tier suite: hermetic unit tests + an Atlas-gated integration tier.

## Quick reference

| Tier | Command | Requires |
|---|---|---|
| Unit | `uv run pytest` | Nothing - `mongomock`, `moto`, fakes |
| Integration | `ATLAS_URI=... uv run pytest -m integration` | Live Atlas cluster |
| Lint | `uv run ruff check src tests streaming/producer.py` | ruff |
| Type check | `uv run mypy --strict src` | mypy |

## pytest configuration

From `pyproject.toml`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
markers = [
    "integration: requires ATLAS_URI; skipped by default",
    "slow: long-running test",
]
addopts = "-m 'not integration' --strict-markers -ra"
```

Running `pytest` excludes the integration marker by default: `uv run pytest`
runs 405 tests (410 collected, 5 integration tests deselected without
`ATLAS_URI`). Use `uv run pytest --collect-only` to see the current count
without executing anything.

## Unit tier

Location: `tests/unit/`. Isolation primitives:

- Autouse env pinning in `tests/unit/conftest.py` - unit tests stay
  hermetic regardless of the local `.env`. The same conftest also points
  `DEEP_AGENT_ENV_FILE` at `/dev/null` at import time and clears the
  settings/cache-builder `lru_cache`s between tests so pinned env and
  cached singletons never leak across cases.
- `mongomock.MongoClient()` constructed inline in each Mongo-backed test
  (e.g. `tests/unit/test_tools_cart.py`, `test_agent_log_integration.py`)
  - there is no shared `mongomock_client` fixture.
- `moto.mock_aws` for S3 code (`tests/unit/test_vfs_s3.py`).
- Deterministic embedding stubs are defined locally where needed (e.g.
  `_FakeEmbeddings` in `tests/unit/test_response_cache.py`); the shared
  `tests/conftest.py` only documents that unit-only env pinning lives in
  `tests/unit/conftest.py`.

## Quality gates

Structural tests in `tests/unit/test_quality_gates.py`:

- `test_TC_15_040_ruff_clean`
- `test_TC_15_041_mypy_strict_clean`
- `test_TC_15_050_domain_isolated` — greps core for forbidden industry terms
- `test_TC_R501_no_gridfs_imports` — GridFS cannot return to `src/`
- `test_TC_R501_no_chat_history_or_plans_modules` — enforces the absence
  of the `persistence.chat_history`, `persistence.plans`, and
  `middleware.plan` *modules* (they must stay unimportable). This guards
  the legacy module surface only; it does not touch the `agent_log`
  collection, which is where per-super-step logs now persist via the
  external `langchain-mongodb-agent-log` `AgentLogMiddleware`.
- `test_TC_R501_no_set_llm_cache_outside_comments` — the deprecated
  process-global LLM-cache swap is forbidden across `src/`
- `test_TC_R501_no_max_hops_arithmetic` — `max_hops` cannot return as a
  recursion-budget formula
- The prompt-level LLM cache has been retired entirely. Three gates keep it
  out: `test_TC_540_B05_no_llm_cache_identifiers_in_src` asserts no
  `llm_cache` identifiers remain in `src/`, while
  `test_TC_540_B05_no_set_llm_cache_in_graph` and
  `test_TC_540_B05_no_inmemory_cache_in_graph` ban the `set_llm_cache(...)`
  process-global swap and `InMemoryCache` from the graph.

These run in the unit tier so a failing gate fails `uv run pytest`.

## VFS contract

`tests/unit/vfs_contract.py` defines assertions every backend must
satisfy (round trip, thread scoping, upsert, size limit, glob). The
contract runs against a dict stub and against `S3Backend` under moto.
Adding a future backend means adding one test file that parameterises
the fixture.

## Integration tier

Location: `tests/integration/`. Every test is marked `pytest.mark.integration`
and each fixture calls `pytest.skip` when its env var is unset, so
`uv run pytest -m integration` is a clean no-op without live infrastructure.

| File | Test IDs | Env vars |
|---|---|---|
| `test_e2e.py` | `test_TC_INT_010_checkpoint_resume`, `test_TC_INT_020_plan_round_trip`, `test_TC_INT_030_langsmith_trace_emitted` | `ATLAS_URI`; `LANGSMITH_API_KEY` for the trace test |
| `test_vfs_backends.py` | S3 contract suite | `ATLAS_URI`, `VFS_S3_BUCKET` |
| `test_research_turn.py` | full research flow | `ATLAS_URI`, `VOYAGE_API_KEY`, `TAVILY_API_KEY` (or the tool raises `ToolDisabledError` — test exercises the KB-only path) |

Each test calls `ensure_indexes()` at setup so you do not bootstrap DDL
separately, and each creates a uniquely-named database or prefix so parallel
runs do not collide. Cleanup runs in `finally` blocks.

Minimum command:

```bash
ATLAS_URI="mongodb+srv://..." VOYAGE_API_KEY=pa-... uv run pytest -m integration
```

Full coverage including streaming also exports `ASP_URI` and
`KAFKA_BOOTSTRAP_SERVERS`.

## Evals dataset + uploader

`tests/fixtures/evals_dataset.jsonl` is the starter Q&A set; the uploader at
`scripts/create_evals_dataset.py` is exercised by four unit tests
(`tests/unit/test_evals_dataset.py`). See [evals.md](evals.md).
