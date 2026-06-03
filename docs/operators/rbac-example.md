# RBAC example - production-ready least-privilege setup

Real deployments should split privileges by role so the runtime cannot
exfiltrate or destroy data beyond its scope.

## Atlas: four database roles

### 1. `deep_agent_app` - runtime read/write

```json
{
  "roleName": "deep_agent_app",
  "actions": [
    {"action": "FIND",      "resources": [{"db": "deep_agent", "collection": ""}]},
    {"action": "INSERT",    "resources": [{"db": "deep_agent", "collection": ""}]},
    {"action": "UPDATE",    "resources": [{"db": "deep_agent", "collection": ""}]},
    {"action": "REMOVE",    "resources": [{"db": "deep_agent", "collection": "checkpoints"}]},
    {"action": "REMOVE",    "resources": [{"db": "deep_agent", "collection": "checkpoint_writes"}]},
    {"action": "REMOVE",    "resources": [{"db": "deep_agent", "collection": "vfs_files"}]},
    {"action": "CHANGE_STREAM","resources": [{"db": "deep_agent", "collection": ""}]}
  ]
}
```

No `DROP_COLLECTION`, no `CREATE_INDEX`. `REMOVE` scoped to collections that
actually need deletes (checkpoint compaction, VFS metadata deletes). There is
no `plans` collection — planner todos live in `agent_log`, which is
append-only and needs no `REMOVE` grant.

### 2. `deep_agent_dataagent` - read-only on operational collections

```json
{
  "roleName": "deep_agent_dataagent",
  "actions": [
    {"action": "FIND", "resources": [
      {"db": "deep_agent", "collection": "customers"},
      {"db": "deep_agent", "collection": "orders"},
      {"db": "deep_agent", "collection": "products"},
      {"db": "deep_agent", "collection": "promotions"}
    ]}
  ]
}
```

Mirror the `DATA_AGENT_ALLOW_LIST` (`products,customers,orders,promotions`):
`promotions` carries the structured coupon terms the NL->MQL data tools read
for deal/savings queries. `carts` is deliberately absent — it is written only
by the dedicated cart tools, never via NL->MQL, so it gets no `FIND` grant here.

Set `DATA_AGENT_MONGODB_URI` to a user bearing only this role. Combined with
`database_toolkit.enforce_safety()` this gives defense in depth against
prompt-injection destructive ops.

### 3. `deep_agent_ingest` - seed + stream worker

```json
{
  "roleName": "deep_agent_ingest",
  "actions": [
    {"action": "INSERT",      "resources": [{"db": "deep_agent", "collection": "knowledge_base"}]},
    {"action": "INSERT",      "resources": [{"db": "deep_agent", "collection": "knowledge_graph"}]},
    {"action": "INSERT",      "resources": [{"db": "deep_agent", "collection": "stream_events"}]},
    {"action": "FIND",        "resources": [{"db": "deep_agent", "collection": "stream_events"}]},
    {"action": "CHANGE_STREAM","resources": [{"db": "deep_agent", "collection": "stream_events"}]}
  ]
}
```

### 4. `deep_agent_admin` - DDL: index creation

```json
{
  "roleName": "deep_agent_admin",
  "inheritedRoles": [{"role": "dbAdmin", "db": "deep_agent"}]
}
```

Use only for `ensure_indexes()` bootstrap, then rotate the credential.

## Atlas: IP access list

- Runtime VPC CIDR(s)
- CI runner egress (for `ensure_indexes()`)
- Operator jump hosts only (not laptops)

## VFS S3 IAM

When `VFS_BACKEND=s3`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow",
     "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
     "Resource": "arn:aws:s3:::deep-agent-artifacts/deep-agent/*"},
    {"Effect": "Allow",
     "Action": "s3:ListBucket",
     "Resource": "arn:aws:s3:::deep-agent-artifacts",
     "Condition": {"StringLike": {"s3:prefix": "deep-agent/*"}}}
  ]
}
```

## Bedrock IAM

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow",
     "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
     "Resource": [
       "arn:aws:bedrock:us-east-1:*:foundation-model/anthropic.claude-sonnet-4-6",
       "arn:aws:bedrock:us-east-1:*:inference-profile/global.anthropic.claude-sonnet-4-6"
     ]}
  ]
}
```

## Summary

| Layer | Credential | Role |
|---|---|---|
| FastAPI/CLI/langgraph dev | `MONGODB_URI` #1 | `deep_agent_app` |
| Data agent | `DATA_AGENT_MONGODB_URI` | `deep_agent_dataagent` |
| Seed / stream worker | `MONGODB_URI` #2 | `deep_agent_ingest` |
| `ensure_indexes()` bootstrap | `MONGODB_URI` #3 | `deep_agent_admin` |
| Bedrock | AWS IAM role | narrow `bedrock:InvokeModel` |
| Voyage | `VOYAGE_API_KEY` | n/a |
| Tavily | `TAVILY_API_KEY` | n/a |
| LangSmith | `LANGSMITH_API_KEY` (per env) | Developer (runtime), Admin (CI) |
| S3 VFS (if used) | IAM role | prefix-scoped policy above |
