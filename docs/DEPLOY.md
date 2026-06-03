# Deploying Deep Agents

One script — [`scripts/deploy.sh`](../scripts/deploy.sh) — brings up the full
local Docker stack (backend + frontend), provisions Atlas indexes, and seeds
reference data. The goal is zero manual configuration beyond `.env`.

If anything goes wrong mid-deploy, the script tears down the Docker stack so a
retry starts clean. Preflight checks fail fast when `.env` has placeholder
values or the Docker tooling is missing.

## Prerequisites

| Requirement | Why | Install |
|---|---|---|
| `docker` + `docker compose` v2 | Builds + runs the stack | [Docker Engine](https://docs.docker.com/engine/install/) |
| `curl` | Health-check probing | OS package manager |
| MongoDB Atlas cluster | Every stateful surface (checkpoints, long-term memory, knowledge base/graph, agent log, VFS metadata, etc.) | M10+ recommended for Vector Search dedicated nodes |
| Voyage AI key | Embeddings + rerank | <https://www.voyageai.com> |
| AWS credentials with Bedrock + S3 access | LLM (Claude Haiku 4.5) + VFS blobs | Set `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` in `.env`; bucket must exist |
| Tavily API key (optional) | `web_search` tool in the researcher subagent | Omit to keep the subagent KB-only |

## `.env` setup

```bash
cp .env.example .env
$EDITOR .env
```

Required:

```bash
MONGODB_URI=mongodb+srv://USER:PASS@CLUSTER.mongodb.net/?retryWrites=true&w=majority
VOYAGE_API_KEY=pa-...
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
VFS_S3_BUCKET=deep-agent-artifacts
```

Recommended (these are the exact knobs this reference deployment depends on):

```bash
LLM_PROVIDER=bedrock
LLM_MODEL=global.anthropic.claude-haiku-4-5-20251001-v1:0
AWS_DEFAULT_REGION=us-east-1
RECURSION_LIMIT=50
MAX_TOKENS=4096
```

> **Why Haiku 4.5?** Haiku 4.5 is the default model for this stack — fast and
> cheap while remaining stable with deepagents' `task` subagent tool on Bedrock.
> Sonnet 4.6 is an optional upgrade you can select per request (or set as
> `LLM_MODEL`) for harder prompts. Avoid Sonnet 4.5: it has a known bad
> interaction with the `task` tool on Bedrock (orphan `tool_use` blocks) and is
> kept in the model list only for completeness.

## Docker

```bash
scripts/deploy.sh
```

Does the following in order:

1. **Preflight**: `.env` exists with non-placeholder `MONGODB_URI` +
   `VOYAGE_API_KEY` + `VFS_S3_BUCKET`; Docker + curl on PATH.
2. **Build** images (skip with `NO_BUILD=1`).
3. `docker compose up -d` (backend + frontend; skip UI with `NO_FRONTEND=1`).
4. Wait up to `TIMEOUT=180`s for `GET /health` to return 200.
5. **Provision Atlas indexes** (skip with `NO_INDEXES=1`): runs
   `ensure_indexes()` inside the backend container so the KB vector/search
   indexes exist before the seed step writes data. The call is idempotent
   admin DDL; calling `ensure_indexes()` directly creates the indexes, so
   no `PROVISION_INDEXES_ON_BOOT` is needed here (that flag only gates the
   automatic call inside the server lifespan, kept off so request-serving
   boots don't attempt DDL under the locked-down runtime role).
6. **Seed reference data** (products/customers/orders/promotions + knowledge
   base + knowledge graph): `deep-agent seed` (idempotent), then `verify_seed`
   independently cross-checks live collection counts against the committed
   fixtures and aborts the deploy on a shortfall (skip both with `NO_SEED=1`).
7. Print URLs and a quick curl test.

On failure between steps 2 and 6 the compose stack is torn down so the
next run starts clean.

## Deploying to a container platform

The application is a standard two-container stack (a FastAPI backend image and
an nginx-served frontend image), so it runs on any container orchestrator —
Kubernetes, ECS, Cloud Run, Nomad, etc. The building blocks:

- **Images** — build from the root [`Dockerfile`](../Dockerfile) (backend) and
  [`frontend/Dockerfile`](../frontend/Dockerfile) (frontend), or adapt
  [`docker-compose.yml`](../docker-compose.yml) to your platform's manifest.
- **Configuration** — supply the same environment variables as `.env` (see
  [configuration.md](./configuration.md)). Inject secrets
  (`MONGODB_URI`, `VOYAGE_API_KEY`, AWS credentials, `LANGSMITH_API_KEY`)
  through your platform's secret store rather than baking them into images.
- **Health checks** — point readiness/liveness probes at `GET /health` and
  `GET /ready` on the backend (port 8000 in-container).
- **One-time bootstrap** — after the backend is reachable, provision indexes and
  seed data once:

  ```bash
  python -c 'from deep_agent.persistence.indexes import ensure_indexes; ensure_indexes()'
  deep-agent seed
  ```

  Run these inside (or against) the backend container — e.g. via
  `kubectl exec`, an ECS task, or a one-off job.

Index provisioning is admin DDL and is intentionally **not** run on every boot;
keep `PROVISION_INDEXES_ON_BOOT` unset for request-serving deployments so the
runtime role needs no `CREATE_INDEX` privilege.

## Environment overrides

Every flag below is also honoured by `scripts/deploy.sh` so you can tune
a deploy without editing the script:

| Variable | Default | Purpose |
|---|---|---|
| `DEEP_AGENT_PORT` | `8010` | Host port for the backend (container is always 8000). |
| `FRONTEND_PORT` | `3000` | Host port for the frontend nginx. |
| `TIMEOUT` | `180` | Seconds to wait for `GET /health` before failing. |
| `COMPOSE` | `docker compose` | Override the compose CLI (e.g. `podman compose`). |
| `NO_BUILD` | `0` | Skip image rebuild on deploy. |
| `NO_INDEXES` | `0` | Skip the Atlas index provisioning step (`ensure_indexes()`). |
| `NO_SEED` | `0` | Skip the seed step (`deep-agent seed`) and its post-seed count verification. |
| `NO_FRONTEND` | `0` | Skip the frontend container. |

## Utility commands

```bash
scripts/deploy.sh --status     # show running Docker services
scripts/deploy.sh --down       # tear down the compose stack
scripts/deploy.sh --help       # full usage
```

## Troubleshooting

### Preflight fails with "MONGODB_URI ... still has a placeholder value"

`.env` contains `USER:PASS` or `...` in the URI. Fill in the actual
Atlas connection string.

### "service did not become healthy within 180s"

Usually means the backend container can't reach Atlas. Check:

```bash
docker compose logs deep_agent | tail -60
```

Look for `connection refused`, DNS errors, or `Failed to connect to MongoDB`.
Most common causes:

- Atlas IP allowlist doesn't include your machine's egress IP
- `MONGODB_URI` missing `?retryWrites=true&w=majority`
- Atlas user doesn't have cluster-wide read/write

### "tool_use ids were found without tool_result blocks"

Should not happen on the default Haiku 4.5 (or the optional Sonnet 4.6)
with this codebase — it's the signature of the known-bad Sonnet 4.5. If
it appears, check you're not pinned to Sonnet 4.5: keep `LLM_MODEL` on
`global.anthropic.claude-haiku-4-5-20251001-v1:0` (or set
`global.anthropic.claude-sonnet-4-6`), and confirm
`src/deep_agent/middleware/patch_dangling.py` is being loaded on Bedrock.
`docker compose logs deep_agent | grep patch_dangling` will confirm the
middleware is running.

### Plan drawer stays empty

The planner only calls `write_todos` for multi-step prompts. For single
lookups ("list all X") it skips planning. Try a multi-step prompt like
the ones in the UI's starter-prompt chips.

### VFS writes fail with `s3:AccessDenied`

The IAM role / user doesn't have `s3:PutObject` on
`<bucket>/<VFS_S3_PREFIX>/*`. See [vfs-backends.md](./vfs-backends.md)
for the minimum policy.

## Related documents

- [architecture.md](./architecture.md) — system-level design
- [configuration.md](./configuration.md) — every env var the backend reads
- [deployment.md](./deployment.md) — lower-level Atlas + Docker mechanics
- [vfs-backends.md](./vfs-backends.md) — S3 VFS backend
- [security.md](./security.md) — TLS, RBAC, secret handling
