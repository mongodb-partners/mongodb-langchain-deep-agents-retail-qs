# Atlas CLI - one-shot cluster + indexes setup

Provision an Atlas cluster, load seed data, and create every Atlas Vector
Search / Search index the reference needs - without the UI.

## Prerequisites

```bash
brew install mongodb-atlas    # macOS (or: https://www.mongodb.com/docs/atlas/cli/stable/install/)
atlas auth login
```

## 1. Provision an M10 cluster

```bash
ATLAS_PROJECT_ID="$(atlas projects list --limit 1 -o json | jq -r '.results[0].id')"

atlas clusters create deep_agent \
  --projectId "$ATLAS_PROJECT_ID" \
  --provider AWS \
  --region US_EAST_1 \
  --tier M10 \
  --mdbVersion 8.0

atlas clusters connectionStrings describe deep_agent \
  --projectId "$ATLAS_PROJECT_ID" \
  -o json | jq -r '.standardSrv'
# → set this as MONGODB_URI in .env
```

## 2. Seed collections

```bash
export MONGODB_URI="mongodb+srv://<user>:<pass>@deep-agent.xxx.mongodb.net/"
uv run deep-agent seed
```

## 3. Indexes (idempotent)

```bash
uv run python -c "from deep_agent.persistence.indexes import ensure_indexes; ensure_indexes()"
```

This creates:

| Collection | Index | Shape |
|---|---|---|
| `knowledge_base` | `vector_index` | 1024-dim cosine + filter on `metadata.source` |
| `knowledge_base` | `search_index` | dynamic text index |
| `semantic_response_cache` | `response_cache_semantic_index` | 1024-dim cosine over `query_embedding` + filters `user_id`, `model` — **only when `ENABLE_RESPONSE_CACHE=true`** (default `true`) |
| `semantic_response_cache` | `response_cache_ttl_idx` | TTL on `created_at`, `RESPONSE_CACHE_TTL_DAYS` (default 7) — same `ENABLE_RESPONSE_CACHE` gate |
| `long_term_memory` | `memory_semantic_index` | 1024-dim cosine + filter on `namespace_prefix` |
| `knowledge_graph` | `kg_type_idx` | `(type, 1)` — fast entity-type traversal filters |
| `feedback` | `feedback_thread_ts_idx` | `(thread_id, 1), (ts, -1)` |
| `vfs_files` | `vfs_thread_path_unique` | unique `(thread_id, path)` |
| `stream_events` | `ts_ttl_idx` | TTL, 30 days |
| `carts` | `carts_user_thread_uniq` | unique `(user_id, thread_id)` — one cart per conversation; backs the upsert |
| `promotions` | `promotions_applies_idx` | `(applies_to.product_id, 1)` — coupon→SKU coverage lookups |
| `promotions` | `promotions_code_uniq` | unique `(code, 1)` |
| `products` | `products_product_id_uniq` | unique `(product_id, 1)` |
| `customers` | `customers_customer_id_uniq` | unique `(customer_id, 1)` |
| `orders` | `orders_order_id_uniq` | unique `(order_id, 1)` |

The `agent_log` indexes (vector + `$search` + TTL on `ts`, `AGENT_LOG_RETENTION_DAYS` default 90)
are created by `ensure_indexes()` too, but their DDL and index names are owned by the external
`langchain-mongodb-agent-log` package; the `$search`/vector pair is created only when
`ENABLE_AGENT_LOG_SEARCH` is on.

## 4. Migrating from the legacy `checkpoint_mirror` collection (existing deployments only)

Skip this section on a fresh deploy. The agent will populate
`agent_log` from the first turn.

There is **no automated migration helper**. From the first turn,
`AgentLogMiddleware` writes fresh history straight to `agent_log`,
so new conversations need nothing. Legacy `checkpoint_mirror`
documents are not carried over — once you no longer need that history, drop the
old collection (in Compass, or with `mongosh`):

```bash
mongosh "$MONGODB_URI/$MONGODB_DB" --eval 'db.checkpoint_mirror.drop()'
```

## 5. Stream Processing Instance (optional)

```bash
atlas streams instances create deep_agent-sp \
  --projectId "$ATLAS_PROJECT_ID" \
  --provider AWS --region us-east-1 --tier SP30
# Register Kafka + Atlas connections (follow the SP instance URL)
mongosh "<SP-URI>" streaming/atlas_sp_pipeline.js
```
