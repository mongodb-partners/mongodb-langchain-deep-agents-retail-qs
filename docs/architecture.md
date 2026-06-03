# Architecture

This document explains the shape of the system: the deep-agent planning loop,
the six specialist subagents, the pluggable VFS, and how MongoDB backs every
persistent surface.

## System context

**Agent Cartsmith** is a retail shopping concierge — a grocery, recipe, and
savings assistant for shoppers — built on the `deepagents` planning pattern
over LangChain + MongoDB Atlas. A main planner agent maintains a todo list,
queries Atlas for live product / order / customer data, retrieves policies and
recipes from a knowledge base + knowledge graph, builds a shopping cart, and
checks out via a human-in-the-loop `place_order`. It writes shopping lists and
meal plans to a virtual filesystem.

Every persistent surface (checkpoints, cross-thread memory, the conversation
log, the response cache, the knowledge base, the knowledge graph, VFS metadata,
carts, promotions) lives in MongoDB Atlas; VFS blobs live in S3 (the only
backend supported).

```
                 ┌────────────────────────────┐
                 │  CLI / FastAPI / Frontend  │
                 └──────────────┬─────────────┘
                                │
                                ▼
                     ┌────────────────────────┐
                     │  build_graph(model)    │
                     │  deepagents            │
                     │  main planner          │
                     │   + 6 subagents        │
                     └──────┬─────────────────┘
                            │
   ┌────────────────────────┼──────────────────────────┐
   │                        │                          │
   ▼                        ▼                          ▼
┌──────────┐     ┌────────────────────┐       ┌──────────────────┐
│ Bedrock  │     │  Voyage embeddings │       │  MongoDB Atlas   │
│ Claude   │     │  + rerank-2.5      │       │  + S3 (VFS blobs)│
└──────────┘     └────────────────────┘       └──────────────────┘
```

## Agent topology

`build_graph()` (`src/deep_agent/graph.py`) compiles a single deep-agent graph
via `deepagents.create_deep_agent`. There is one graph per process — no
`lru_cache` at this layer — and the server caches one compiled graph per LLM
model (see [Model selection](#model-selection)).

### Main planner agent

The main agent owns the `write_todos` planning tool, the built-in filesystem
tools (`read_file` / `write_file` / `edit_file` / `ls` / `glob` / `grep`), and
an explicit tool belt (`graph.py:138-177` — see [Tool belt](#tool-belt)). It
decomposes a request into todos, routes deliberately to its own tools, and
delegates open-ended sub-tasks to a subagent through the deepagents `task`
tool.

### The six subagents

Exactly six subagents are registered in `build_graph()` (`graph.py:290-296`)
and dispatched by the planner via `task("<name>", ...)`. Subagent tool lists
are **explicit and NOT inherited** — each `SubAgent` (`agents/subagents.py`)
declares its own tools, so a subagent never silently gains a tool the planner
holds (most importantly `place_order`).

| Subagent | What it does | Tools |
|---|---|---|
| `researcher` | Deep-dives a sub-question via the KB + web; ingests new findings back into the KB. | `web_search`, `fetch_and_cache`, `knowledge_base_search`, `knowledge_base_hybrid_search`, `knowledge_graph_search` |
| `writer` | Composes long-form artifacts (reports / briefs / lists) from a `/workspace/**` research bundle and saves them with `write_file`. | none — `tools=[]`, so the harness provides ONLY the default filesystem tools |
| `deal_optimizer` | Maximizes cart savings by stacking coupons, penny-exact via `savings_calculator`. | `view_cart`, `update_cart_item`, `savings_calculator`, `knowledge_graph_search`, + NL→MQL data tools |
| `loyalty_concierge` | Personalized loyalty briefing (tier perks, points value at 100 pts = $1, spend-to-next-tier, YTD savings). | `current_shopper`, `recall_memories`, `knowledge_base_search`, `knowledge_base_hybrid_search`, + data tools |
| `reorder_concierge` | Builds a reorder basket from order-history cadence. | `current_shopper`, `add_to_cart`, `view_cart`, + data tools |
| `basket_cross_sell` | Suggests complementary items via co-purchase affinity + recipe completion. | `add_to_cart`, `view_cart`, `knowledge_graph_search`, + data tools |

The `writer` is intentionally bare: with `tools=[]` it gets only the default
filesystem tools and does NOT inherit the researcher's KB or web tools. If its
research bundle is incomplete it returns `INSUFFICIENT_BUNDLE: ...` and the
planner routes back to the researcher. The four data-driven retail specialists
(`deal_optimizer`, `loyalty_concierge`, `reorder_concierge`,
`basket_cross_sell`) accept a shared `data_tools` argument so the NL→MQL
toolkit is built once and reused by the main agent and the subagents instead of
three separate builds.

Subagents run via the harness's synchronous `invoke()` under **no
checkpointer**. This is the structural reason `place_order` must stay on the
main agent (see [`place_order` and HITL](#place_order-and-hitl)): an
`interrupt()` raised inside a subagent could not be durably resumed.

## Tool belt

The main agent's tools (`graph.py:138-177`):

- **`knowledge_base_search`** — Atlas `$vectorSearch` over `knowledge_base`,
  optionally narrowed by a `metadata.source` filter, then reranked with Voyage
  `rerank-2.5`.
- **`knowledge_base_hybrid_search`** — Reciprocal Rank Fusion (RRF) of vector
  + lexical `$search` via `MongoDBAtlasHybridSearchRetriever`, also reranked
  with Voyage. Both KB tools are best-effort: a retrieval error returns "No
  results." rather than raising, because a raised exception would orphan the
  parent `tool_use` block and Bedrock's strict validator would reject the next
  turn.
- **`knowledge_graph_search`** — GraphRAG traversal via `MongoDBGraphStore`
  (`chat_response`), for entity-relation questions (product → brand, recipe →
  ingredients, coupon → product). Also best-effort.
- **NL→MQL data tools** (`mongodb_query`, `mongodb_query_checker`,
  `mongodb_schema`, `mongodb_list_collections`) — read-only access to the
  operational collections, fail-closed. See [NL→MQL data agent](#nlmql-data-agent).
- **`CART_TOOLS`** = `[add_to_cart, update_cart_item, remove_from_cart,
  view_cart, clear_cart]` (`tools/cart.py`). These mutate the `carts`
  collection through pymongo directly using **atomic** update operators.
- **`place_order`** — checkout. It is SEPARATE from `CART_TOOLS` and is the
  HITL target (see below).
- **`savings_calculator`** — deterministic, penny-exact coupon-stack math.
- **`current_shopper`** — resolves the runtime `user_id` to the customer's
  profile so the planner can scope order/loyalty queries to a `customer_id`.
- **`remember_fact` / `recall_memories`** — cross-thread long-term memory,
  scoped per user.
- **`fetch_and_cache`** — downloads a URL (with an SSRF guard + body-size cap),
  writes the raw page to the VFS, and ingests deduped chunks into the KB.
- **`search_past_conversations`** — conditional. The package-provided
  hybrid-recall tool over `agent_log` is registered ONLY when
  `ENABLE_AGENT_LOG_SEARCH` is on (otherwise the Atlas indexes it depends on do
  not exist).

`web_search` (Tavily) is bound to the `researcher` subagent, not the main
agent.

### Atomic cart concurrency

Cart writes use atomic MongoDB update operators (`$inc` / `$push` / `$pull`),
not read-modify-write. The planner fires independent `add_to_cart` calls in
PARALLEL within one super-step; a `load → mutate → replace_one` would let those
concurrent writes clobber each other (last-write-wins would drop items). Atomic
operators let parallel adds of distinct products each `$push` their own line. A
unique compound index on `(user_id, thread_id)` enforces one cart per
conversation and backs the upsert; `add_to_cart` falls back to a no-upsert push
on a `DuplicateKeyError` create-race. When the runtime is unavailable the cart
tools return a sentinel string rather than raising, so Bedrock always pairs each
`tool_use` with a `tool_result`.

### `place_order` and HITL

`place_order` is MAIN-AGENT-ONLY and is the human-in-the-loop checkout target.
Because subagents run via synchronous `invoke()` with no checkpointer, an
`interrupt()` raised inside a subagent cannot be resumed — only a main-agent
`place_order` yields a durable, resumable interrupt under `MongoDBSaver`. On
execution, `place_order` writes a new `orders` document (keyed by a natural
`order_id`; MongoDB owns the ObjectId `_id`) and atomically empties the cart.

HITL is **opt-in** via the `HITL_TOOLS` env var (comma-separated tool names);
the reference app and `.env.example` ship it commented out, so HITL is **off by
default**. When `HITL_TOOLS=place_order`, `create_deep_agent` receives
`interrupt_on={place_order: {allowed_decisions: [approve, edit, reject]}}`,
`/chat` emits an `interrupt` SSE frame, and the `GET /interrupts` +
`POST /interrupts/resume` endpoints register. The non-HITL path stays
byte-identical when `HITL_TOOLS` is empty.

## Persistence surfaces

Collection names come from `Settings` (`config.py`); index names and specs from
`persistence/indexes.py`.

| Surface | Class / writer | Collection(s) | Index |
|---|---|---|---|
| Short-term checkpoints | `MongoDBSaver` | `checkpoints`, `checkpoint_writes` | managed by MongoDBSaver |
| Cross-thread memory | `MongoDBStore` | `long_term_memory` | `memory_semantic_index` (1024-dim cosine) + `namespace_prefix` filter |
| Conversation log + plans | `AgentLogMiddleware` (external `langchain-mongodb-agent-log`) | `agent_log` | `agent_log_vector_idx` + `agent_log_search_idx` + TTL on `ts` (owned by the package) |
| Semantic response cache | `ResponseCache` (query-keyed) | `semantic_response_cache` | `response_cache_semantic_index` (vector + `user_id`/`model` filters) + `response_cache_ttl_idx` |
| Vector KB | `MongoDBAtlasVectorSearch` | `knowledge_base` | `vector_index` (+ `metadata.source` filter) |
| Lexical KB | Atlas `$search` | `knowledge_base` | `search_index` (dynamic) |
| Knowledge graph | `MongoDBGraphStore` | `knowledge_graph` | `kg_type_idx` (+ managed by langchain-mongodb) |
| Cart | cart tools (pymongo, atomic) | `carts` | `carts_user_thread_uniq` (unique `(user_id, thread_id)`) |
| Promotions | seed / read-only | `promotions` | `promotions_applies_idx` + `promotions_code_uniq` |
| VFS metadata | `VfsMetadataStore` | `vfs_files` | unique `(thread_id, path)` |
| Stream sink | change stream | `stream_events` | TTL 30d on `ts` |
| Feedback | plain insert | `feedback` | `(thread_id, ts)` |

The operational collections (`products`, `customers`, `orders`, `promotions`)
key on a NATURAL field — `product_id`, `customer_id`, `order_id`, `code` — not
the opaque `_id`, each with a unique index so re-seeding is idempotent and the
tool/join lookups are fast.

### Semantic response cache

- **`semantic_response_cache`** — a query-keyed, turn-level
  *response* cache. It embeds ONLY the user query (with the asymmetric Voyage
  `embed_query`, so an identical query self-matches at cosine 1.0) and stores
  the final answer scoped by `(user_id, model)`, with a TTL. It is **on by
  default** (`ENABLE_RESPONSE_CACHE=true`, `RESPONSE_CACHE_THRESHOLD=0.9`,
  `RESPONSE_CACHE_TTL_DAYS=7`). Only FRESH-conversation turns are cached, and
  turns that invoke a mutating tool (`add_to_cart`, `update_cart_item`,
  `remove_from_cart`, `clear_cart`, `place_order`, `savings_calculator`) are
  NEVER cached. (An older prompt-level LLM cache — which embedded the whole
  ~15 KB shared prompt and collided across different queries — was retired in
  favor of this query-keyed cache.)

`VOYAGE_API_KEY` is mandatory: `build_store()` always constructs the embedder
for the long-term-memory vector index, so the app fails fast at startup without
the key — not only when a cache or search feature is on.

### NL→MQL data agent

The data tools wrap LangChain's read-only `MongoDBDatabaseToolkit`
(`tools/database_toolkit.py`) with a defense-in-depth safety layer that is
**fail-closed** via `DATA_AGENT_ALLOW_LIST`: an empty list refuses *every*
collection. `.env.example` sets
`DATA_AGENT_ALLOW_LIST=products,customers,orders,promotions`. The wrapper also:

- blocks any underscore-prefixed (internal) collection, always;
- refuses destructive aggregation stages (`$out` / `$merge` / `$function` /
  `$where` / `$accumulator`) and insert/update/delete keywords;
- walks the pipeline (JSON AST or mongosh string) for cross-collection
  references in `$lookup` / `$graphLookup` / `$unionWith` / `$merge` / `$out`
  and gates each against the allow-list;
- injects an implicit `$limit` (default 1000) when a query has none;
- gates the schema tool's `collection_names` by the same allow-list.

`DATA_AGENT_ALLOW_ALL=true` opens all non-underscore collections (dev/demo
only). `carts` is deliberately **excluded** from the allow-list — it is written
only by the cart tools and never reachable by NL→MQL — while `promotions` is
**included** so `savings_calculator` and the deal-optimizer can read structured
coupon terms.

## Request lifecycle

`POST /chat` returns a Server-Sent Events stream. The server composes the
checkpoint key `f"{user_id}:{sub}"` from the per-conversation `thread_id`
(`sub`) the client sends, and drives `astream_events`, mapping LangGraph events
to SSE frames:

| Frame | Payload |
|---|---|
| `correlation` | the correlation id (always the leading frame) |
| `token` | a plain-text slice of an LLM chunk |
| `status` | `{phase: tool_start \| tool_end, name}` |
| `plan` | `{todos, updated_at}` (emitted on change) |
| `interrupt` | `{thread_id, action: {name, args, description}, allowed_decisions}` (HITL only) |
| `done` | `[DONE]` |
| `error` | a code: `turn_timeout`, `shutdown`, or `internal_error cid=...` |

```
POST /chat {user_id, thread_id, message, model?}
      │
      ▼
  _graph_for(model)  (lifespan-built default, or per-model cached)
      │
      ▼
  response cache: fresh-conversation? (no prior messages in the checkpoint)
      │  hit  ──▶ stream stored answer, persist the exchange, return
      │  miss ──▶ run the agent
      ▼
  Main agent: write_todos ──▶ AgentLogMiddleware ──▶ agent_log (per super-step)
      │  routes to tools / dispatches task("<subagent>", ...)
      │  writes files (write_file) ──▶ CompositeBackend ──▶ S3 + vfs_files
      ▼
  Stream token / status / plan frames
      │
      ├─ HITL on + graph paused on place_order ──▶ interrupt frame ──▶ return
      │       (resume via POST /interrupts/resume, which streams the rest)
      │
      └─ clean miss, no mutating tool fired ──▶ save response by
              (query, user_id, model)
      │
  MongoDBSaver commits every super-step ──▶ checkpoints / checkpoint_writes
```

The response cache only acts on a fresh conversation (the opener has
no context to lose); a hit streams the stored answer, skips the agent entirely,
and writes the exchange back into the checkpoint so a follow-up in the same
thread still runs with coherent history. A turn that invoked a mutating tool is
never cached — a cache replay would stream the stored text WITHOUT re-running
the tool, so the cart/order mutation would silently not happen on a repeat of
the same query. A turn torn down by timeout / shutdown / error never reaches the
save call, so failed turns are never cached.

When HITL is enabled and the graph pauses on `place_order`, the server reads the
durable checkpoint (`get_state().interrupts`) and emits an `interrupt` frame
carrying the proposed action. `POST /interrupts/resume` resumes the graph with
an `approve` / `edit` / `reject` decision and streams the resumed turn through
the same machinery, so the post-approval order confirmation lands in the same
assistant message.

### HTTP endpoints

`GET /live` · `GET /ready` · `GET /health` · `GET /models` ·
`GET /plans?user_id&thread_id` · `GET /messages?user_id&thread_id` ·
`GET /threads/latest?user_id` · `GET /files?user_id&thread_id` ·
`GET /cart?user_id&thread_id` · `POST /chat` (SSE) · `POST /feedback`.
`GET /interrupts?thread_id` and `POST /interrupts/resume` register ONLY when
`HITL_TOOLS` is non-empty. `GET /health` returns
`{"status": "ok"|"degraded", "mongo": "ok"|"error: ...", "ts": <iso>}` — there
is no `db` field (an unauthenticated probe must not fingerprint the Atlas DB or
the bound model). `user_id` is a trust-on-input scoping key, not an
authentication boundary — add edge auth for cross-user isolation.

## Model selection

The default model is `global.anthropic.claude-haiku-4-5-20251001-v1:0`
— the fastest Anthropic model in the registry that passes both the
single-tool and parallel-tool harnesses. A per-request selector
exposes `AVAILABLE_MODELS` (Opus 4.7 / 4.6 / 4.5 / 4.1, Sonnet 4.6 / 4.5,
Haiku 4.5 / 3.5, Nova premier/pro/lite, Llama 4 Maverick, Mistral Pixtral);
`GET /models` drives the UI dropdown and the server caches one compiled graph
per model (`_GRAPHS_BY_MODEL`, serialized behind a build lock). Sonnet 4.6 is
an optional upgrade; Sonnet 4.5 is the known-bad model (orphan `tool_use` on
Bedrock interacting with the `task` tool) — kept in the dropdown for
completeness, not chosen as default. `max_tokens` defaults to 4096 (the
universal Bedrock floor), with a per-model lookup raising verified Anthropic /
Nova / Mistral profiles to their real ceilings.

Embeddings route asymmetrically through Voyage: documents use `voyage-4`,
queries use `voyage-4-lite` (a shared embedding space), reranking uses
`rerank-2.5`, at 1024 dimensions.

## VFS abstraction

Deepagents' built-in `read_file` / `write_file` / `edit_file` / `ls` / `glob` /
`grep` tools speak a `BackendProtocol` contract (absolute paths,
line-addressable reads, structured result dataclasses). `build_graph()` passes a
`CompositeBackend` **instance** that routes writes by prefix:

```
CompositeBackend
  ├── /memories/  ──▶ StoreBackend(store=MongoDBStore, namespace=per-user)
  └── (default)   ──▶ MongoVfsBackend  ──▶ S3 blobs + vfs_files metadata
```

`VFS_BACKEND=s3` is the only supported value (`VFS_S3_BUCKET` required); the
`BlobStore` protocol stays in place so a future driver — Azure Blob, GCS — is
one new file plus one parametrized contract test. The `/memories/**` route
converges semantic-memory tools and any `write_file('/memories/...')` on a
single user-scoped surface, so it is reserved for the typed `remember_fact`
tool — letting `write_file` land arbitrary Markdown there caused E11000
collisions on the `(namespace, key)` multikey index. `mongo_backend_factory` is
a back-compat shim; the live graph passes a `CompositeBackend` instance.
`MongoVfsBackend` resolves `thread_id` lazily per call from
`langgraph.config.get_config()`, so one instance serves every turn with the
correct scoping. See [vfs-backends.md](vfs-backends.md) and
[mongodb-backend.md](mongodb-backend.md).

Write permissions (`graph.py:71-84`): `/workspace/**`, `/scratch/**`,
`/web_cache/**` are allowed; everything else is default-deny for writes (reads
outside the allow-list remain permitted, since the agent often re-reads its own
outputs); `/memories/` is reserved as above.

## Conversation log + plan persistence

The conversation log is owned by the external
[`langchain-mongodb-agent-log`](https://github.com/mongodb-labs/langchain-mongodb-agent-log)
package. Its `AgentLogMiddleware` runs unconditionally (an
`after_model` hook) and writes one decoded log document per super-step into the
`agent_log` collection, attributed to the agent that produced it (the main
agent or a named subagent). A single process-wide `AgentLog` instance (guarded
by a double-checked lock, not `lru_cache`) preserves FIFO order across
concurrent super-steps through its daemon worker thread; the worker is flushed
and stopped before the MongoClient closes on shutdown.

There is **no** `persistence.plans`, `chat_history`, or `middleware.plan`
module — the legacy "mirror" / `CheckpointMirrorMiddleware` framing is gone, and
a quality-gate test enforces the absence of those modules (not the `agent_log`
data itself). Vector + lexical Atlas Search indexes on the log's text field make
it RRF-searchable; `search_past_conversations` wraps the package retriever over
those indexes and is registered only when `ENABLE_AGENT_LOG_SEARCH` is on
(default `true` in `.env.example`), so the per-super-step embedding cost can be
disabled.

**Truncation / TTL:** tool-result content larger than
`AGENT_LOG_MAX_CONTENT_BYTES` (default 15 MiB, sized below the 16 MiB BSON
ceiling) is replaced inline with a `<truncated …>` marker, on the assumption
that any content that large is already in the VFS or the knowledge base. Smaller
items are stored verbatim — there is no redaction. Documents carry a `ts` TTL
anchor and expire after `AGENT_LOG_RETENTION_DAYS` (default 90).

## Invariants

Enforced structurally and by tests:

1. **TLS required**: `Settings._enforce_tls` rejects non-TLS URIs unless
   `ALLOW_INSECURE=true`.
2. **Per-user memory**: the memory namespace is always
   `("user", user_id, "memories")` (`build_namespace`).
3. **VFS thread scoping**: listings for a thread contain only that thread's
   files; the metadata key `(thread_id, path)` is unique.
4. **Read-only data agent**: the NL→MQL toolkit is fail-closed and
   destructive pipeline stages / collections never reach the database; cart and
   order mutations flow through dedicated pymongo tools, never NL→MQL.
5. **Domain isolation of persistence**: legacy modules
   (`persistence.chat_history`, `persistence.plans`, `middleware.plan`), GridFS
   imports, the `set_llm_cache(...)` process-global swap, and `max_hops`
   references are all locked out by `tests/unit/test_quality_gates.py`.
6. **Checkout safety**: `place_order` is main-agent-only so its HITL interrupt
   is durable and resumable under the checkpointer; subagents cannot hold it.
7. **Chat resilience**: a `PyMongoError` on the agent-log write never 5xx's the
   `/chat` endpoint — the middleware swallows it after a single warn-level log
   so the LLM turn always returns to the client.

## Provisioning

`ensure_indexes()` (`persistence/indexes.py`) creates every Atlas Search /
Vector Search / compound index idempotently. It is admin DDL, gated by
`PROVISION_INDEXES_ON_BOOT` (default `False`) — the documented RBAC runtime
role has no `CREATE_INDEX`, so DDL is a one-shot bootstrap under the admin role,
not a per-boot self-heal. The `semantic_response_cache` index provisioning is
env-conditional (only when the feature is enabled).
The `agent_log` indexes are owned by the `langchain-mongodb-agent-log` package,
which `ensure_indexes()` delegates to so the package owns the doc shape and
index names end-to-end.
