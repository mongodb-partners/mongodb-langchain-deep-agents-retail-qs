# API Reference

Reference for the three entry surfaces of **Agent Cartsmith** — a grocery /
recipe / savings concierge built on LangChain deepagents + MongoDB Atlas.

- [HTTP API](#http-api) — FastAPI with SSE streaming
- [CLI](#cli) — `deep-agent` command
- [Python API](#python-api) — factories, tools, subagents

Related: [configuration.md](configuration.md), [mongodb-backend.md](mongodb-backend.md),
[evals.md](evals.md).

---

## HTTP API

`create_app()` (`src/deep_agent/server/app.py`) returns the FastAPI instance.
uvicorn factory: `deep_agent.server.app:get_asgi_app`.

The client sends `thread_id` as a per-conversation **sub**. The server composes
the checkpoint key `f"{user_id}:{sub}"` and threads it through
`RunnableConfig.configurable.thread_id`. `user_id` is an untrusted scoping key
(trust-on-input), not an authentication boundary.

### Endpoint summary

| Method | Path | Notes |
|---|---|---|
| GET | `/live` | Liveness. Never touches dependencies. |
| GET | `/ready` | Readiness. 503 while starting or draining. |
| GET | `/health` | Back-compat probe; reuses the readiness cache. |
| GET | `/models` | Model dropdown contents. |
| GET | `/plans?user_id&thread_id` | Latest planner todo snapshot. |
| GET | `/messages?user_id&thread_id` | Reconstructed message list. |
| GET | `/threads/latest?user_id` | Sub of the user's most recent conversation. |
| GET | `/files?user_id&thread_id` | VFS files written in a conversation. |
| GET | `/cart?user_id&thread_id` | Current cart for the conversation. |
| POST | `/chat` | SSE token stream for one turn. |
| POST | `/feedback` | Persist run feedback. |
| GET | `/interrupts?thread_id` | HITL-only. Registered iff `HITL_TOOLS` non-empty. |
| POST | `/interrupts/resume` | HITL-only. Registered iff `HITL_TOOLS` non-empty. |

### `GET /live`

```json
{"status": "live"}
```

### `GET /ready`

200 with `{"status":"ready","checks":{"mongo":"ok"}}` once the lifespan has
built the graph and a cached MongoDB ping succeeded within
`READINESS_CACHE_TTL_S`. Returns 503 with `{"status":"starting"}` before the
graph is built, `{"status":"draining"}` during shutdown, and
`{"status":"not_ready","checks":{"mongo":<error>}}` on a failed ping.

### `GET /health`

```json
{"status": "ok", "mongo": "ok", "ts": "<iso>"}
```

`status` is `"ok"` or `"degraded"`; `mongo` is `"ok"` or `"error: <name>"`.
Never 5xx's. There is **no `db` field** — the Atlas DB name and bound model id
are deliberately not exposed on this unauthenticated endpoint.

### `GET /models`

```json
{"default": "<id>", "models": [{"id": "<id>", "label": "<label>"}]}
```

Drives the UI model dropdown. `models` is parsed from `AVAILABLE_MODELS`; the
default (`LLM_MODEL`) is prepended if not already listed. `label` strips the
`us.` / `global.` region prefix.

### `GET /plans?user_id&thread_id`

Always 200 with `{"todos": [...], "updated_at": <iso | null>}`. Reads the
newest `agent_log` doc for the composite `f"{user_id}:{sub}"` (ordered by `ts`
desc, restart-robust). Each todo is `{id, text, status}` where `status` is one
of `pending` / `in_progress` / `completed`.

### `GET /messages?user_id&thread_id`

Always 200 with `{"messages": [...]}` (empty list for an unknown thread).
Returns `messages` from the newest `agent_log` doc for the composite key.

### `GET /threads/latest?user_id`

`{"thread_id": <sub | null>}`. Returns the bare per-conversation **sub** of the
user's most recent `agent_log` doc (the `f"{user_id}:"` prefix is stripped), so
the frontend can rehydrate the last chat on load.

### `GET /files?user_id&thread_id`

Always 200 with `{"files": [{"path", "size", "created_at"}, ...]}`. Lists VFS
files written under the composite thread id (`vfs_files.thread_id`), ordered by
`created_at` asc.

### `GET /cart?user_id&thread_id`

Always 200 with `{"lines": [...], "subtotal", "total_savings", "updated_at"}`
(empty cart for an unknown thread). The cart is keyed by the natural
`(user_id, thread_id)` — the same key the cart tools write under.

### `POST /chat`

Request body:

| Field | Type | Required | Description |
|---|---|---|---|
| `user_id` | string | yes | Scopes memory + the thread namespace (trust-on-input). |
| `thread_id` | string | no | Per-conversation sub. Defaults to `"default"`. |
| `message` | string | yes | User request. |
| `model` | string | no | Optional model override; must be in `AVAILABLE_MODELS` (else 400). |

Response: `text/event-stream`. Returns 503 if the server is draining. A per-turn
wall-clock timeout (`CHAT_TURN_TIMEOUT_S`) bounds the stream.

**SSE frames** (in `event:` order over a turn):

| Event | Data | When |
|---|---|---|
| `correlation` | the correlation id (string) | Always, leading frame. |
| `token` | text slice (string) | Streamed answer text. |
| `status` | `{"phase": "tool_start"\|"tool_end", "name": <tool>}` | Tool boundaries. |
| `plan` | `{"todos": [{id,text,status}], "updated_at": <iso>}` | On planner todo change. |
| `interrupt` | `{"thread_id", "action": {name, args, description}, "allowed_decisions": [...]}` | HITL pause (see below). |
| `done` | `[DONE]` | Clean completion. |
| `error` | `turn_timeout` / `shutdown` / `internal_error cid=<id>` | Failure. |

The `interrupt` frame is emitted only when `HITL_TOOLS` is non-empty and the
graph paused on a HITL tool (`place_order`). An interrupted turn does not emit
`done` and is never response-cached.

**Response cache.** Only **fresh-conversation** turns (no prior
messages in the checkpoint) are eligible. A hit streams the stored answer and
persists the exchange to the checkpoint. A turn that invokes any mutating tool
(`add_to_cart`, `update_cart_item`, `remove_from_cart`, `clear_cart`,
`place_order`, `savings_calculator`) is **never** cached — a cache replay would
skip the mutation.

### `POST /feedback`

```json
{"run_id": "...", "score": 1.0, "comment": "...", "user_id": "..."}
```

Inserts into `feedback`. When `LANGSMITH_TRACING=true`, mirrors to LangSmith
(mirror failures are swallowed). 500 on `PyMongoError` during insert; returns
`{"ok": true}` otherwise.

### HITL endpoints (registered only when `HITL_TOOLS` is non-empty)

HITL is **opt-in** via the `HITL_TOOLS` env var (comma-separated tool names);
the reference and `.env.example` ship it commented out (HITL **off** by
default). When `HITL_TOOLS=place_order`, `create_deep_agent` receives
`interrupt_on={place_order: {allowed_decisions: ["approve","edit","reject"]}}`,
and these two routes are registered.

`place_order` is **main-agent-only** and is the sole HITL checkout target.
Subagents run via synchronous `invoke()` under **no checkpointer**, so an
`interrupt()` raised inside a subagent cannot be resumed; only a main-agent
`place_order` yields a durable, resumable interrupt under `MongoDBSaver`.

#### `GET /interrupts?thread_id`

`{"thread_id": <id>, "next": [...]}`. Inspects pending interrupts for a thread.
404 if the state can't be read.

#### `POST /interrupts/resume`

Request body:

| Field | Type | Description |
|---|---|---|
| `thread_id` | string | The composite `f"{user_id}:{sub}"` the interrupt frame echoed back. |
| `decision` | `"approve"` / `"edit"` / `"reject"` | The HITL decision. |
| `edited_action` | object | Required for `edit`; must include `name` (and `args`). |
| `message` | string | Optional rejection note. |

Resumes the interrupted graph and **streams** the resumed turn exactly like
`/chat` (`correlation` → `token` → `done`). Decision shapes sent to the
deepagents HITL middleware:

- `approve` → `{"type": "approve"}`
- `reject` → `{"type": "reject", "message"?: str}`
- `edit` → `{"type": "edit", "edited_action": {"name", "args"}}`

`edit` without `edited_action.name` returns 400.

---

## CLI

Entry points (`pyproject.toml`):

- `deep-agent` → `deep_agent.cli:main`
- `deep-agent-evals` → `deep_agent.evals:main`

```
deep-agent {chat,seed,serve} ...
```

There is **no `deep-agent evals` subcommand** — evals are a separate entry
point (`deep-agent-evals`, or `python -m deep_agent.evals`).

### `deep-agent chat`

| Flag | Default |
|---|---|
| `--once MESSAGE` | _(none — REPL mode)_ |
| `--user USER_ID` | `demo-user` |
| `--thread THREAD_ID` | `cli-default` |

Builds the graph per process and invokes it, printing the last AI message.
REPL mode exits on `/quit`, `/exit`, or EOF.

### `deep-agent seed`

Runs `deep_agent.ingestion.seed.seed_all()` (operational data + knowledge base
+ knowledge graph) and prints the summary. Idempotent; safe to re-run. After
writing, `seed_all()` reads every deterministic fixture back from the database
(`_verify_seeded`) and raises `SeedIncompleteError` on a shortfall — the CLI
surfaces that as a non-zero exit (`seed failed: …`) so a partial seed (e.g. an
Atlas failover mid-load) fails loudly instead of exiting `0`.

### `deep-agent serve`

| Flag | Default |
|---|---|
| `--host` | `0.0.0.0` |
| `--port` | `8000` |
| `--reload` | _(off)_ |

Boots FastAPI via uvicorn using the ASGI factory (`factory=True`).

### `deep-agent-evals`

| Flag | Default |
|---|---|
| `--dataset` | _(required)_ — LangSmith dataset name or id |
| `--prefix` | `agent-cartsmith-retail-demo` |

Runs the graph against a LangSmith dataset. See [evals.md](evals.md).

---

## Python API

### `deep_agent.graph`

- `build_graph(model: str | None = None) -> CompiledStateGraph` — builds and
  compiles the deep agent via `deepagents.create_deep_agent`. Wires:
  `get_llm(model)`, the main-agent tool list, `MAIN_PROMPT`, the **6
  subagents**, the middleware chain (`AgentLogMiddleware`, plus
  `PatchDanglingToolCallsMiddleware` on Bedrock), `MongoDBSaver`
  (checkpointer), `MongoDBStore` (store), a `CompositeBackend` **instance**
  (backend), the filesystem write permissions, and on-demand `skills`. Passes
  `interrupt_on=` only when `HITL_TOOLS` is non-empty. One graph per process;
  the server caches one compiled graph per model.
- `build_graph_uncheckpointed() -> CompiledStateGraph` — same agent + subagents
  without persistence (`backend=MongoVfsBackend()`, no checkpointer/store);
  used by unit tests and linters.

### `deep_agent.models`

- `get_llm(model: str | None = None) -> BaseChatModel` — `init_chat_model` over
  `LLM_PROVIDER` / `LLM_MODEL`. Per-model `max_tokens` lookup (default 4096;
  verified Bedrock profiles get their real ceiling). `lru_cache` keyed on `model`.
- `get_embeddings() -> Embeddings` — `AsymmetricVoyageEmbeddings`: documents
  embedded with `VOYAGE_DOCUMENT_MODEL` (default `voyage-4`), queries with
  `VOYAGE_QUERY_MODEL` (default `voyage-4-lite`). The two share a common
  embedding space, so asymmetric routing is correct. Output dimension =
  `VOYAGE_DIMENSIONS` (default 1024).
- `get_reranker() -> VoyageAIRerank` — `VOYAGE_RERANK_MODEL` (default
  `rerank-2.5`).

`VOYAGE_API_KEY` is **mandatory**: `build_store()` always embeds, so the app
fails fast at startup without it (not only when caching / search is enabled).

### `deep_agent.config`

- `get_settings() -> Settings` (`lru_cache`).
- `Settings` — pydantic-settings model; single source of truth for secrets and
  tunables. `MONGODB_URI` and `MONGODB_DB` are required. See
  [configuration.md](configuration.md).

Default model: `global.anthropic.claude-haiku-4-5-20251001-v1:0`.
`AVAILABLE_MODELS` lists the per-request selectable Bedrock profiles
(Opus 4.7/4.6/4.5/4.1, Sonnet 4.6/4.5, Haiku 4.5/3.5, Nova premier/pro/lite,
Llama 4 Maverick, Mistral Pixtral). Sonnet 4.6 is an optional upgrade; Sonnet
4.5 is the known-bad model on Bedrock (orphan `tool_use`) — kept for
completeness, not the default.

### `deep_agent.persistence`

- `mongo.get_client() / get_db() / reset_for_tests()` — singleton
  `MongoClient` bound to `Settings.mongodb_db`.
- `indexes.ensure_indexes()` — admin DDL, gated by `PROVISION_INDEXES_ON_BOOT`
  (default `False`). Creates the KB vector (`vector_index`) + lexical
  (`search_index`) indexes, the long-term-memory vector index
  (`memory_semantic_index`), the `carts` unique compound index
  (`carts_user_thread_uniq`), the `vfs_files` unique index
  (`vfs_thread_path_unique`), the natural-key unique indexes on
  products/customers/orders/promotions, and TTL indexes on `stream_events` and
  the semantic response cache. The `semantic_response_cache` vector index is provisioned
  only when its feature flag is on. `agent_log`
  indexes are owned by the external `langchain-mongodb-agent-log` package
  (delegated, not created directly here). Confirm exact specs in
  `persistence/indexes.py`.
- `checkpointer.build_checkpointer() -> MongoDBSaver`.
- `store.build_store() -> MongoDBStore` — semantic-search store over
  `long_term_memory`; `store.build_namespace(user_id) -> ("user", user_id,
  "memories")`.
- `vector_store.build_vector_store() -> MongoDBAtlasVectorSearch` — KB store
  (`text` key, `embedding` key, cosine, `auto_create_index=False`).
- `graph_store.build_graph_store() -> MongoDBGraphStore`.
- `response_cache.build_response_cache() -> ResponseCache | None` —
  query-keyed turn-level **response** cache. Embeds ONLY the user
  query (`embed_query`) and stores the final answer scoped by `(user_id,
  model)`, with a TTL. Collection `semantic_response_cache`, vector index
  `response_cache_semantic_index`. On by default
  (`ENABLE_RESPONSE_CACHE=true`, `RESPONSE_CACHE_THRESHOLD=0.9`,
  `RESPONSE_CACHE_TTL_DAYS=7`). `None` when disabled or no Voyage key.

This is the sole semantic cache: `semantic_response_cache` is query-keyed at
the turn level.

### `deep_agent.persistence` naming note

Legacy "mirror" / "CheckpointMirrorMiddleware" / "mirror persistence" terms are
gone. The current `AgentLogMiddleware` (from the external
`langchain-mongodb-agent-log` package) writes one decoded log doc per
super-step into the `agent_log` collection, with hybrid (vector +
`$search` RRF) recall over it. There is no `persistence.plans` /
`chat_history` / `middleware.plan` module (a test quality gate enforces their
absence as modules).

### `deep_agent.backends`

- `MongoVfsBackend(vfs=None, thread_id=None)` — implements the deepagents
  `BackendProtocol` (`ls`, `read`, `write`, `edit`, `glob`, `grep`) on top of
  `VirtualFilesystem`. Resolves `thread_id` lazily per call via
  `langgraph.config.get_config()`. Blobs in S3, metadata in MongoDB. See
  [mongodb-backend.md](mongodb-backend.md).
- `mongo_backend_factory(runtime) -> MongoVfsBackend` — back-compat shim
  (argument ignored). `build_graph()` passes a `CompositeBackend` **instance**,
  not this factory: `/memories/**` routes to a per-user `StoreBackend`
  (`MongoDBStore`); everything else falls through to `MongoVfsBackend`.

Write permissions (`graph.py`): `/workspace/**`, `/scratch/**`, `/web_cache/**`
are allowed; all other paths default-deny writes (reads stay permitted).
`/memories/` is reserved for `remember_fact` via the StoreBackend route.

### `deep_agent.vfs`

S3-only (`VFS_BACKEND=s3`, requires `VFS_S3_BUCKET`); GridFS was removed.

- `get_vfs() -> VirtualFilesystem` — S3-backed singleton; raises if
  `VFS_S3_BUCKET` is unset.
- `VirtualFilesystem(backend, metadata, max_bytes)` — `write_file` /
  `read_file` / `delete_file` / `list_files` / `glob_files`. Rejects `..`
  segments, null bytes, oversize paths.
- `S3Backend(bucket, prefix, client)` — object key
  `<prefix>/<thread_id>/<path>`.
- `VfsMetadataStore(collection)` — metadata in `vfs_files` (thread-scoped,
  `(thread_id, path)` unique).
- Exceptions: `VfsError`, `VfsFileNotFoundError`, `VfsQuotaExceededError`.

### `deep_agent.ingestion`

- `seed.seed_all / seed_knowledge_base / seed_knowledge_graph /
  seed_knowledge_graph_entities / seed_operational_data` — idempotent loaders.
  `seed_all` prefers the committed pre-extracted `knowledge_graph.entities.json`
  (LLM-free) and falls back to live LLM extraction only when that artifact is
  absent.
- `stream_worker.run_once / run_with_backoff / resume_token_path` — change-stream
  consumer that pipes new `stream_events` docs into the KB.
- `asp.default_pipeline_spec / register_pipeline / stop_pipeline` — Atlas Stream
  Processing helpers.

Seeds live in `examples/retail_assistant/seeds` (the directory contains only
`seeds/` — no README). `deep-agent seed` loads `operational/*.json` + KB + KG.
Verified counts: **products=1505** (sourced from Open Food Facts),
**customers=10**, **orders=18**, **promotions=11**, **knowledge_base=9** docs.
The knowledge-graph seed is LLM-free; `carts` is not seeded.

### `deep_agent.evals`

- `run_evaluation(*, dataset_name, experiment_prefix="agent-cartsmith-retail-demo") -> Any`
- `main([...])` — argparse CLI (`--dataset`, `--prefix`)

The domain fixture is `tests/fixtures/retail_evals.jsonl` (6 retail Q&A rows),
uploaded via `scripts/create_evals_dataset.py`. See [evals.md](evals.md).

---

## Tools

All tools are LangChain `@tool`s. Most degrade gracefully (return a sentinel
string rather than raising) so a raised exception never orphans a Bedrock
`tool_use` block.

### Main-agent tools (`graph.py` `_main_agent_tools`)

The main agent binds: `knowledge_base_search`, `knowledge_base_hybrid_search`,
`knowledge_graph_search`, `fetch_and_cache`, `remember_fact`, `recall_memories`,
`current_shopper`, the `CART_TOOLS`, `place_order`, the NL→MQL data tools, and
`search_past_conversations` (only when `ENABLE_AGENT_LOG_SEARCH` is on).

### Retrieval

- `knowledge_base_search(query, k=4, source=None)` — Atlas `$vectorSearch` over
  `knowledge_base`. `source` is forwarded as a `pre_filter` on
  `metadata.source`. Over-fetches `max(k*3, k)`, then reranks with Voyage
  `rerank-2.5` to the top `k`. Returns a JSON-serialized string of
  `{text, metadata}` hits.
- `knowledge_base_hybrid_search(query)` — Reciprocal Rank Fusion of vector +
  lexical `$search` via `MongoDBAtlasHybridSearchRetriever` (`top_k=4`), then
  Voyage rerank to the top 4. Prefer this for queries with distinctive keywords
  / proper nouns.
- `knowledge_graph_search(query)` — GraphRAG traversal via
  `MongoDBGraphStore.chat_response`; returns a sentinel string on
  `OperationFailure` / LLM error so the turn stays valid.

### Memory

- `remember_fact(fact)` — persists a short atom into `long_term_memory` under
  `("user", user_id, "memories")`. Scoped to the runtime `user_id`.
- `recall_memories(query, limit=5)` — semantic search over the same namespace
  (`limit` capped 1–20).

### Identity & web

- `current_shopper()` — resolves the runtime `user_id` to the `customers`
  profile (id, name, loyalty tier/points, dietary preferences, household size).
  Call first for "my orders / loyalty / reorders" requests.
- `fetch_and_cache(url, thread_id=None)` — fetches a URL (SSRF-guarded, DNS
  checked pre-GET, body capped at `FETCH_MAX_BYTES`), caches the raw page in the
  VFS under `web_cache/<hash>.html` when `thread_id` is given, chunks and
  ingests into the KB. Dedupes by SHA-256 of the body (`{cached: true,
  chunks_added: 0}` on a repeat).
- `web_search(query, max_results=5)` — Tavily. Returns a sentinel note string
  when `TAVILY_API_KEY` is unset or the call fails.
- `search_past_conversations(query)` — package-provided hybrid (vector +
  `$search` RRF) search over `agent_log`; registered only when
  `ENABLE_AGENT_LOG_SEARCH` is on. `top_k` from `AGENT_LOG_SEARCH_TOP_K`
  (default 5; the package caps at 20).

### Cart, checkout, savings

`CART_TOOLS = [add_to_cart, update_cart_item, remove_from_cart, view_cart,
clear_cart]` (`tools/cart.py`). The cart is one document per conversation, keyed
by the natural `(user_id, thread_id)` (MongoDB owns the `_id`). Writes use
atomic `$inc` / `$push` / `$pull` so parallel adds don't clobber each other.

- `add_to_cart(product_id, qty=1)` — add / increase a line.
- `update_cart_item(product_id, qty)` — set absolute quantity (`qty<=0`
  removes).
- `remove_from_cart(product_id)` — remove a line.
- `view_cart()` — render lines, quantities, sale prices, subtotal.
- `clear_cart()` — empty the cart.
- `place_order()` — **separate from `CART_TOOLS`**. Main-agent-only HITL
  checkout target: when `HITL_TOOLS` lists it, the graph pauses for approval
  before it runs. Writes a new `orders` document (keyed by the natural
  `order_id`) and empties the cart. Refuses an empty cart.
- `savings_calculator(coupons=None)` — deterministic penny-exact coupon
  optimizer (`tools/savings.py`). Reads the live cart + `promotions`, picks the
  optimal stack (≤1 manufacturer + ≤1 store coupon per item, sale price first,
  never below $0; 100 pts = $1), stamps the chosen coupons + per-line savings
  onto the cart, and returns a summary. A subagent tool, not on the main agent.

### NL→MQL data tools (`tools/database_toolkit.py`)

`get_data_tools()` returns the read-only `MongoDBDatabaseToolkit` tools
(`mongodb_query`, `mongodb_query_checker`, `mongodb_schema`,
`mongodb_list_collections`) wrapped with a defense-in-depth safety layer:

- **Allow-list, fail-closed**: only `DATA_AGENT_ALLOW_LIST` collections are
  queryable. An empty allow-list refuses **every** collection.
  `.env.example` sets `DATA_AGENT_ALLOW_LIST=products,customers,orders,promotions`.
- **Underscore block**: any `_`-prefixed collection is always refused.
- **Destructive refusal**: `$out` / `$merge` / `$function` / `$where` /
  `$accumulator` and insert/update/delete keywords are refused; cross-collection
  references (`$lookup.from`, `$unionWith`, etc.) are gated by the same
  allow-list.
- **Implicit `$limit`**: pipelines without an explicit `$limit` are capped at
  1000 documents.
- `DATA_AGENT_ALLOW_ALL=true` opens all non-underscore collections (dev/demo
  only).

`carts` is deliberately excluded from the allow-list (never NL→MQL).
`promotions` is included in the read allow-list.

---

## Subagents

Exactly **6** subagents, all registered in `build_graph()`. The main agent
dispatches each via the deepagents `task` tool. Each subagent's tool list is
**explicit** and is **not** inherited from the main agent. Below, "data tools"
means the shared NL→MQL toolkit.

| Subagent | Role | Tools |
|---|---|---|
| `researcher` | Deep-dives sub-questions via KB + web; ingests findings back into the KB. | `web_search`, `fetch_and_cache`, `knowledge_base_search`, `knowledge_base_hybrid_search`, `knowledge_graph_search` |
| `writer` | Composes long-form artifacts from the `/workspace` research bundle. | none (`tools=[]`; default filesystem tools only) |
| `deal_optimizer` | Maximizes cart savings by stacking coupons, penny-exact via `savings_calculator`. | `view_cart`, `update_cart_item`, `savings_calculator`, `knowledge_graph_search`, + data tools |
| `loyalty_concierge` | Personalized loyalty briefing (tier perks, 100 pts = $1, spend-to-next-tier, YTD savings). | `current_shopper`, `recall_memories`, `knowledge_base_search`, `knowledge_base_hybrid_search`, + data tools |
| `reorder_concierge` | Builds a reorder basket from order-history cadence. | `current_shopper`, `add_to_cart`, `view_cart`, + data tools |
| `basket_cross_sell` | Suggests complementary items via co-purchase affinity + recipe completion. | `add_to_cart`, `view_cart`, `knowledge_graph_search`, + data tools |

No subagent has `place_order` — checkout is main-agent-only because subagents
run under no checkpointer (an interrupt inside one cannot be resumed).

---

## Operational collections

`checkpoints`, `checkpoint_writes`, `long_term_memory`, `knowledge_base`,
`knowledge_graph`, `agent_log`, `vfs_files`, `feedback`, `stream_events`,
`semantic_response_cache`, `carts`, `promotions`.

`carts` is written only by the cart tools and is excluded from
`DATA_AGENT_ALLOW_LIST`. `promotions` holds structured coupon terms read by
`savings_calculator` and is included in the NL→MQL read allow-list.
