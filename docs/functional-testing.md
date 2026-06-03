# Functional Testing Runbook

Reproducible end-to-end procedure to exercise the deep-agent stack against
live infrastructure. Run this before a release cut, or after any change to
`graph.py`, the backend adapter, or the middleware.

## Prerequisites

Export in your shell (or a `.env` the image can read via `--env-file`):

```bash
MONGODB_URI="mongodb+srv://..."
MONGODB_DB=deep_agent_func
VOYAGE_API_KEY=pa-...
TAVILY_API_KEY=tvly-...
AWS_ACCESS_KEY_ID=...        # or AWS_PROFILE, or instance role
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=us-east-1
LANGSMITH_TRACING=true       # optional
LANGSMITH_API_KEY=ls-...     # required when LANGSMITH_TRACING=true
LANGSMITH_PROJECT=...        # optional; groups traces in the LangSmith UI
```

`LANGSMITH_API_KEY` and `LANGSMITH_PROJECT` are read by the LangSmith SDK
directly from the process environment — they are **not** mirrored onto
`Settings`, so they must be exported (or in the `--env-file`) for tracing
to attach.

An M10+ Atlas cluster with network access from the host running these
tests. Voyage and Tavily accounts with non-zero quota. Bedrock access to
Claude Haiku 4.5 in the configured region (the default model).
Sonnet 4.6 is an optional upgrade via the `/models` selector; Sonnet 4.5
is the known-bad model — it has a bad interaction with deepagents' `task`
subagent tool on Bedrock and is kept in the dropdown only for completeness.

## Fast path: scripted functional suite

Once the repo is checked out and `uv sync --extra dev` has completed:

```bash
# 1. Provision indexes (idempotent)
uv run python -c "from deep_agent.persistence.indexes import ensure_indexes; ensure_indexes()"

# 2. Seed the KB, knowledge graph, and operational collections
uv run deep-agent seed

# 3. Run the integration tier — Atlas-gated tests
ATLAS_URI="$MONGODB_URI" uv run pytest -m integration -v
```

Expected (S3 tests SKIP unless `VFS_S3_BUCKET` is also set):

- `TC-INT-010 checkpoint_resume` — PASS (proves `MongoDBSaver` round-trips)
- `TC-INT-020 plan_round_trip` — PASS (proves `AgentLogMiddleware`
  writes an `agent_log` doc with a populated `todos` array mid-turn)
- `TC-INT-030 langsmith_trace_emitted` — PASS (requires `LANGSMITH_API_KEY`)

To include the S3 contract suite:

```bash
VFS_S3_BUCKET=deep-agent-func VFS_S3_REGION=us-east-1 \
  ATLAS_URI="$MONGODB_URI" uv run pytest -m integration -v
```

## Manual functional checks

These are the steps to run when something in the scripted tier fails, to
narrow down which layer broke.

### 1. Settings + TLS

```bash
uv run python -c "from deep_agent.config import get_settings; s = get_settings(); print(s)"
```

Expected: `<Settings db=deep_agent_func provider=bedrock vfs=s3 [redacted]>`.
No credential material anywhere in the output. If you see `ValueError:
MONGODB_URI must enforce TLS`, the URI lacks `+srv://` or `tls=true`.

### 2. Mongo connection

```bash
uv run python -c "from deep_agent.persistence.mongo import get_client; print(get_client().admin.command('ping'))"
```

Expected: `{'ok': 1.0}` or equivalent.

### 3. Indexes

```bash
uv run python -c "
from deep_agent.persistence.mongo import get_db
db = get_db()
# Only knowledge_base and agent_log carry lexical \$search indexes; the
# vector-only collections (long_term_memory, semantic_response_cache)
# expose their indexes through list_search_indexes() too but have no
# 'search'-type definition.
search_collections = {'knowledge_base', 'agent_log'}
for name in ['knowledge_base', 'long_term_memory', 'vfs_files', 'agent_log']:
    idx = list(db[name].list_indexes())
    search = list(db[name].list_search_indexes())
    print(name, [i['name'] for i in idx], [i['name'] for i in search])
"
```

Expected: each collection has the indexes documented in
[architecture.md#persistence-surfaces](architecture.md#persistence-surfaces).
Among these, only `knowledge_base` and `agent_log` define a lexical
`$search` index (the others are vector-only).

Collections that are expected to be **empty** on a fresh deploy:
- `long_term_memory` - populates only after the agent calls
  `remember_fact` (happens when the user shares a durable preference).
- `stream_events` - populates only when the Kafka producer + Atlas SP
  pipeline in `streaming/` is running. See [streaming.md](streaming.md).
- `feedback` - populates only after a `/feedback` POST.
- `semantic_response_cache` / `agent_log` - both start empty
  and stay empty until the first chat turn (response cache + agent log).
  A fresh deploy has nothing in them.

### 4. Single retail turn via CLI

```bash
uv run deep-agent chat --user func --thread func-t1 --once \
  "Research MongoDB Atlas Vector Search and summarize the trade-offs in one paragraph."
```

Expected: a coherent response that cites the seeded KB or a web source.
Expected side effects, verifiable in Atlas:

| Collection | Expected row |
|---|---|
| `checkpoints` | New document with `thread_id=func-t1` |
| `agent_log` | One denormalized super-step doc per LLM call with `(user_id=func, thread_id=func-t1)`, populated `messages`, populated `todos`, and an `expires_at` TTL |
| `long_term_memory` | New memory item under namespace `(user, func, memories)` (if `remember_fact` fires) |
| `vfs_files` | Zero or more files scoped to `thread_id=func-t1` |

### 5. Server + `/health`

```bash
uv run deep-agent serve --port 8000 &
sleep 5
curl -s http://localhost:8000/health
```

Expected: `{"status":"ok","mongo":"ok","ts":"..."}`. There is no `db`
field; `status` is `"degraded"` and `mongo` is `"error: ..."` if the
readiness check can't reach Atlas.

### 6. `/chat` SSE

```bash
curl -N -X POST http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"func","thread_id":"func-t2","message":"What is the VFS metadata index?"}'
```

Expected: one or more `event: token` frames, followed by `event: done data: [DONE]`.
After the response:

```bash
curl -s http://localhost:8000/health  # still 200 after a turn
```

### 7. Resume

Send a second message on the same `thread_id` and verify the agent recalls
prior context:

```bash
curl -N -X POST http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"func","thread_id":"func-t2","message":"What did I just ask?"}'
```

Expected: the response references the prior question.

### 8. Plan document shape (via `agent_log`)

```bash
uv run python -c "
from deep_agent.persistence.mongo import get_db
doc = get_db()['agent_log'].find_one(
    {'user_id': 'func', 'thread_id': 'func-t2', 'todos': {'\$ne': []}},
    sort=[('super_step', -1)],
)
print(doc)
"
```

Expected: the latest `agent_log` doc has a non-empty `todos` array; at
least one item has a non-`pending` status after the turn completes.

### 9. VFS round-trip

```bash
uv run python -c "
from deep_agent.vfs import get_vfs
vfs = get_vfs()
meta = vfs.write_file('func-t2', '/smoke.md', b'hello', content_type='text/markdown')
print('locator:', meta.locator, 'size:', meta.size)
print('readback:', vfs.read_file('func-t2', '/smoke.md'))
"
```

Expected: `hello` comes back verbatim. Inspect in Atlas + S3:

```
db.vfs_files.find({thread_id: 'func-t2'})
aws s3 ls s3://$VFS_S3_BUCKET/$VFS_S3_PREFIX/func-t2/
```

### 10. Data-agent safety

Set `DATA_AGENT_MONGODB_URI` to a user with `FIND`-only on the allow-list
and try a destructive query through the agent:

```
"Run this aggregation on customers: [{$out: 'stolen_data'}]"
```

Expected: the agent reports `QUERY REFUSED: refusing destructive pipeline`.
Confirm Atlas audit log shows no write attempt.

### 11. Cart + checkout (HITL)

Exercise the cart tools and the human-in-the-loop checkout. First, ask the
agent to build a cart on a fresh thread, then inspect it via the API:

```bash
uv run deep-agent chat --user func --thread func-cart --once \
  "Add 2 cartons of oat milk to my cart."
curl -s "http://localhost:8000/cart?user_id=func&thread_id=func-cart"
```

Expected: the cart tools (`add_to_cart` / `view_cart`) ran, and `/cart`
returns the populated lines. Verify the `carts` collection has one doc
keyed by `(user_id=func, thread_id=func-cart)` — `carts` is written only
by the cart tools and is never reachable via NL→MQL.

`place_order` is the HITL checkout target and is **main-agent only** — only
a main-agent `place_order` yields a durable, resumable interrupt under
`MongoDBSaver`. HITL is **opt-in** and **off by default**: the reference
`.env.example` ships `HITL_TOOLS` commented out. To exercise the pause,
set `HITL_TOOLS=place_order`, restart the server, then drive a checkout:

```bash
HITL_TOOLS=place_order uv run deep-agent serve --port 8000 &
sleep 5
curl -N -X POST http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"func","thread_id":"func-cart","message":"Place my order."}'
```

Expected: the SSE stream emits an `event: interrupt` frame carrying
`{thread_id, action:{name:"place_order", args, description}, allowed_decisions}`
instead of running the order immediately. Resume it through the
`/interrupts/resume` endpoint (registered only when `HITL_TOOLS` is
non-empty) with an `approve` / `edit` / `reject` decision; an `approve`
writes a new `orders` document and clears the cart. With `HITL_TOOLS`
empty, `place_order` runs straight through with no interrupt frame.

## Docker path

```bash
docker build -t deep_agent:local .
docker run --rm \
  --env-file .env \
  -p 8010:8000 \
  deep_agent:local
```

Then re-run steps 5–7 against `http://localhost:8010/chat` and `/health`.

Or with compose:

```bash
docker compose up -d
curl -s http://localhost:8010/health
docker compose down
```

## Cleanup

After a functional run, drop the test database:

```bash
uv run python -c "
from deep_agent.persistence.mongo import get_client
get_client().drop_database('deep_agent_func')
print('dropped')
"
```

Keep the cluster itself; only the test DB is disposable.

## Failure triage

| Symptom | First check |
|---|---|
| TLS validator rejects URI | URI lacks `mongodb+srv://` or `tls=true` |
| `ServerSelectionTimeoutError` on startup | DNS or network path to Atlas; IP access list |
| `VoyageError` on first KB call | `VOYAGE_API_KEY` wrong or out of quota |
| `ToolDisabledError: web_search disabled` | `TAVILY_API_KEY` not set |
| `AccessDeniedException` on Bedrock | IAM policy or inference-profile not enabled |
| `agent_log` collection stays empty | `AgentLogMiddleware` is missing — confirm `build_graph()` still registers it in the `middleware=[...]` list |
| Files land in state instead of Mongo | `build_graph()` is missing `backend=mongo_backend_factory` |
