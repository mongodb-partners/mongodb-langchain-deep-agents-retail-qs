# Security Model

## TLS enforcement

`Settings._enforce_tls` rejects any `MONGODB_URI` that is not
`mongodb+srv://` and does not contain `tls=true` or `ssl=true`. Override:
`ALLOW_INSECURE=true` (dev only).

## Secret redaction

`Settings.__repr__` / `__str__` emit no secrets. Connection errors in
`persistence/mongo.py:get_client` re-raise with the URI redacted.

## Per-user memory scoping

Every graph entrypoint threads `user_id` into the config. `MongoDBStore`
namespaces are `("user", user_id, "memories")` - users cannot read each
other's long-term memory. `POST /chat` validates `user_id` at the boundary
(HTTP 422 when missing).

## VFS thread scoping

Every metadata query in `VfsMetadataStore` carries `thread_id` as a top-level
filter. A thread cannot see another thread's files. The S3 backend (the only
shipping VFS backend) encodes `thread_id` into the object key.
`(thread_id, path)` is unique at the index level.

## Data-agent safety wrapper

`tools/database_toolkit.py` layers four protections on `MongoDBDatabaseToolkit`:

1. Underscore-prefixed collections refused.
2. Non-allow-listed collections refused.
3. Destructive stages (`$out` / `$merge` / `$function` / `$where` /
   `$accumulator`) and keywords (`drop` / `deleteMany` / etc.) refused.
4. Implicit `$limit: 1000` injected when missing.

Refusals return `"QUERY REFUSED: <reason>"` to the LLM so it can reroute.

`DATA_AGENT_ALLOW_LIST` defaults to **EMPTY** in code (`config.py`), which
fails CLOSED - `enforce_safety` refuses *every* collection until the list is
configured. The `.env.example` value `products,customers,orders,promotions`
is a reference example, not a code default. `carts` is intentionally never
allow-listed: it is written only by the dedicated cart tools, so the NL→MQL
agent can neither read nor mutate it. `DATA_AGENT_ALLOW_ALL=true` opens all
non-underscore collections (dev/demo only).

`DATA_AGENT_MONGODB_URI` routes data-agent calls through a least-privilege
Atlas role (read-only on allow-listed collections). Defense in depth: the
wrapper blocks at the Python layer AND the role blocks at the DB layer.

The allow-list is sourced from `DATA_AGENT_ALLOW_LIST` (CSV). For this retail
reference it is `{products, customers, orders, promotions}`. When a fork
retargets the reference at a vertical, update both the env var and the matching
`deep_agent_dataagent` role grants — e.g. `{patients, encounters, medications}`
for healthcare, `{accounts, transactions, customers}` for finance.

## RBAC

Four Atlas roles in [operators/rbac-example.md](operators/rbac-example.md):

| Role | Used by | Privileges |
|---|---|---|
| `deep_agent_app` | FastAPI/CLI/langgraph dev | Read/write app collections; no DDL |
| `deep_agent_dataagent` | Data agent | Read-only on `customers`, `orders`, `products`, `promotions` |
| `deep_agent_ingest` | Seed + stream worker | Insert on KB, KG, `stream_events`; change stream |
| `deep_agent_admin` | `ensure_indexes()` bootstrap | `dbAdmin` |

## VFS S3 IAM policy

When `VFS_BACKEND=s3`, scope the IAM policy to the prefix only. Do not allow
`s3:ListBucket` at the bucket root - a compromised runtime could enumerate
other tenants' prefixes. Example in [vfs-backends.md](vfs-backends.md).

## LangSmith & Voyage & Tavily keys

Treat all API keys as high-sensitivity secrets. Rotate; store in AWS Secrets
Manager / Vault. Per-environment LangSmith keys; never checked into source.
