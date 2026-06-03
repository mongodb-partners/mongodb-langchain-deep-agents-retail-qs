# Configuration

All runtime configuration is centralised in `Settings`
(`src/deep_agent/config.py`). Fields read from environment variables, `.env`,
or defaults in that order. `get_settings()` caches the resolved instance.

## Environment variables

| Variable | Required | Default | Notes |
|---|---|---|---|
| `MONGODB_URI` | yes | - | Must be `mongodb+srv://` or include `tls=true` |
| `MONGODB_DB` | **yes** | (required, no default) | `Field(...)`; startup fails if unset |
| `DATA_AGENT_MONGODB_URI` | no | falls back to `MONGODB_URI` | Least-privilege URI for the data agent |
| `MONGODB_MAX_POOL_SIZE` | no | `100` | MongoClient pool ceiling |
| `MONGODB_MIN_POOL_SIZE` | no | `10` | MongoClient pool floor |
| `MONGODB_SERVER_SELECTION_TIMEOUT_MS` | no | `5000` | MongoClient server-selection timeout |
| `MONGODB_SOCKET_TIMEOUT_MS` | no | `30000` | MongoClient socket timeout |
| `LLM_PROVIDER` | no | `bedrock` | Any `init_chat_model` provider |
| `LLM_MODEL` | no | `global.anthropic.claude-haiku-4-5-20251001-v1:0` | Fast default. For an **optional** upgrade use Sonnet 4.6 — NOT Sonnet 4.5, which has a known deepagents `task` tool_use/tool_result pairing issue on Bedrock. |
| `AVAILABLE_MODELS` | no | _(13-model CSV; see below)_ | CSV of Bedrock inference-profile IDs offered in the `/models` dropdown (Opus 4.7/4.6/4.5/4.1, Sonnet 4.6/4.5, Haiku 4.5/3.5, Nova premier/pro/lite, Llama 4 Maverick, Mistral Pixtral) |
| `MAX_TOKENS` | no | `4096` | Output token ceiling per LLM call (universal Bedrock floor) |
| `AWS_DEFAULT_REGION` | no | `us-east-1` | Used when `LLM_PROVIDER=bedrock` |
| `VOYAGE_API_KEY` | **yes** | - | Mandatory. `build_store()` always embeds, so `Settings._enforce_voyage_key` fails fast at startup if unset (not only when caching/search is on) |
| `VOYAGE_DOCUMENT_MODEL` | no | `voyage-4` | High-capacity model used for ingestion |
| `VOYAGE_QUERY_MODEL` | no | `voyage-4-lite` | Latency-optimised model used for queries |
| `VOYAGE_DIMENSIONS` | no | `1024` | Must match the vector-index `numDimensions` |
| `VOYAGE_RERANK_MODEL` | no | `rerank-2.5` | |
| `VOYAGE_BASE_URL` | no | _(unset)_ | Gateway URL (e.g. MongoDB AI Gateway) |
| `TAVILY_API_KEY` | no | - | Optional. Enables `web_search`; absent → sentinel/disabled tool |
| `VFS_BACKEND` | no | `s3` | Only `s3` is supported (GridFS has been removed) |
| `VFS_MAX_BYTES` | no | `52428800` (50 MiB) | Per-file cap |
| `VFS_S3_BUCKET` | yes | - | Required (S3 is the only backend) |
| `VFS_S3_PREFIX` | no | `deep-agent` | S3 key prefix |
| `VFS_S3_REGION` | no | _(unset)_ | Inherits boto3 default chain |
| `LANGSMITH_TRACING` | no | `false` | Read by Settings; `LANGSMITH_API_KEY` / `LANGSMITH_PROJECT` are read directly by the LangSmith SDK from the process env |
| `ENABLE_RESPONSE_CACHE` | no | `true` | Query-keyed turn-level response cache; embeds only the user query and stores the final answer scoped by `(user_id, model)`. Mutating-tool turns are never cached |
| `RESPONSE_CACHE_THRESHOLD` | no | `0.9` | Cosine similarity threshold for response-cache hits |
| `RESPONSE_CACHE_TTL_DAYS` | no | `7` | TTL on `created_at` bounding cached-answer staleness |
| `ENABLE_AGENT_LOG_SEARCH` | no | `true` | When false, skips embedding the joint human+AI text per super-step (saves Voyage cost). Legacy alias: `ENABLE_MIRROR_SEARCH` |
| `AGENT_LOG_MAX_CONTENT_BYTES` | no | `15728640` (15 MiB) | Per-message truncation cap. Legacy alias: `MIRROR_TOOL_RESULT_MAX_BYTES` |
| `AGENT_LOG_RETENTION_DAYS` | no | `90` | TTL on `ts`. Legacy alias: `MIRROR_RETENTION_DAYS` |
| `AGENT_LOG_SEARCH_TEXT_MAX_BYTES` | no | `8192` | Cap on the joint text fed into the embedder. Legacy alias: `MIRROR_SEARCH_TEXT_MAX_BYTES` |
| `AGENT_LOG_SEARCH_TOP_K` | no | `5` | How many past-conversation hits `search_past_conversations` returns (package caps at 20). Legacy alias: `MIRROR_SEARCH_TOP_K` |
| `AGENT_LOG_COLLECTION` | no | `agent_log` | The collection the agent log writes to (replaces the previously hard-coded `checkpoint_mirror`) |
| `RECURSION_LIMIT` | no | `50` | LangGraph recursion bound |
| `CHAT_TURN_TIMEOUT_S` | no | `180` | Per-turn wall-clock cap |
| `READINESS_CACHE_TTL_S` | no | `5` | `/ready` cache TTL |
| `SHUTDOWN_GRACE_PERIOD_S` | no | `30` | Drain window for in-flight `/chat` streams |
| `FETCH_MAX_BYTES` | no | `2097152` (2 MiB) | `fetch_and_cache` per-page cap |
| `HITL_TOOLS` | no | _(empty)_ | CSV of tool names to interrupt before |
| `DATA_AGENT_ALLOW_LIST` | no | _(empty)_ | CSV of operational collections the data agent may read. Fail-closed: empty ⇒ refuse every collection |
| `DATA_AGENT_ALLOW_ALL` | no | `false` | Opt into open mode (every non-underscore collection queryable). Dev/demo only |
| `PROVISION_INDEXES_ON_BOOT` | no | `false` | Run `ensure_indexes()` admin DDL at startup; default off (operators bootstrap via the CLI/admin role) |
| `AGENT_SKILLS_DIR` | no | `/app/AgentSkills` | Directory of `SKILL.md` files; relative paths resolve against CWD at graph-build time |
| `SEEDS_DIR` | no | `examples/retail_assistant/seeds` | Seed source (operational JSON + KB + KG) |
| `ALLOW_INSECURE` | no | `false` | Bypass the TLS check (dev only) |

Unit tests pin this set via an autouse fixture in `tests/unit/conftest.py`.

### Stream-worker state

Resume-token file location (`src/deep_agent/ingestion/stream_worker.py`):

1. `DEEP_AGENT_STATE_DIR` - explicit override
2. `XDG_STATE_HOME/deep_agent`
3. `~/.deep_agent`

## Collection names

Override via attribute on `Settings`. The agent-log collection name is also
overridable via the `AGENT_LOG_COLLECTION` env var (the underlying writer is
provided by the `langchain-mongodb-agent-log` package).

| Attribute | Default |
|---|---|
| `checkpoints_collection` | `checkpoints` |
| `checkpoint_writes_collection` | `checkpoint_writes` |
| `long_term_memory_collection` | `long_term_memory` |
| `agent_log_collection` | `agent_log` |
| `response_cache_collection` | `semantic_response_cache` |
| `knowledge_base_collection` | `knowledge_base` |
| `knowledge_graph_collection` | `knowledge_graph` |
| `stream_events_collection` | `stream_events` |
| `feedback_collection` | `feedback` |
| `vfs_files_collection` | `vfs_files` |
| `carts_collection` | `carts` |
| `promotions_collection` | `promotions` |

## Index names

Mirror indexes are env-overridable; the others are Settings attributes.

| Attribute / env var | Default | Type |
|---|---|---|
| `knowledge_base_vector_index` | `vector_index` | vectorSearch + `metadata.source` filter |
| `knowledge_base_search_index` | `search_index` | Atlas `$search` dynamic |
| `response_cache_vector_index` | `response_cache_semantic_index` | vectorSearch over `query_embedding` + `user_id`/`model` filters |
| `AGENT_LOG_VECTOR_INDEX` | `agent_log_vector_idx` | vectorSearch over `agent_log.agent_log_embedding` (legacy alias: `MIRROR_VECTOR_INDEX`) |
| `AGENT_LOG_SEARCH_INDEX` | `agent_log_search_idx` | Atlas `$search` over `agent_log.agent_log_text` (legacy alias: `MIRROR_SEARCH_INDEX`) |
| `long_term_memory_vector_index` | `memory_semantic_index` | vector |

## TLS enforcement

`Settings._enforce_tls` rejects any `MONGODB_URI` that does not start with
`mongodb+srv://` and does not contain `tls=true`/`ssl=true`. `ALLOW_INSECURE=true`
is the dev escape hatch.

## Secret redaction

`__repr__` / `__str__` return
`<Settings db=<name> provider=<name> vfs=<backend> [redacted]>`. No secret
material is ever interpolated.
