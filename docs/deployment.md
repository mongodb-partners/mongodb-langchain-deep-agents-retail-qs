# Deployment

> **One-command deploys:** [DEPLOY.md](./DEPLOY.md) covers the Docker
> workflow and a vendor-neutral guide for deploying to a container
> platform. This page covers lower-level Atlas + Docker + LangGraph
> mechanics for operators who want to drive each step manually.

## Atlas bootstrap

See [operators/atlas-cli-setup.md](operators/atlas-cli-setup.md) for the full
`atlas` CLI sequence. Summary:

```bash
atlas clusters create deep_agent \
  --projectId "$ATLAS_PROJECT_ID" \
  --provider AWS --region US_EAST_1 \
  --tier M10 --mdbVersion 8.0
```

Then create indexes:

```bash
uv run python -c "from deep_agent.persistence.indexes import ensure_indexes; ensure_indexes()"
```

## Docker image

Two-stage uv build. Stage 1 uses `ghcr.io/astral-sh/uv:python3.11-bookworm-slim`
to hydrate a venv with runtime deps only (`--no-dev`). Stage 2 is a slim
Python runtime with a non-root `deep_agent` user on UID 1000.

```bash
docker build -t deep_agent:local .
docker run --rm -p 8010:8000 --env-file .env deep_agent:local
```

Default command: `deep-agent serve --host 0.0.0.0 --port 8000`. Override to
run the CLI:

```bash
docker run --rm --env-file .env deep_agent:local deep-agent seed
docker run --rm --env-file .env deep_agent:local python -m deep_agent.ingestion.stream_worker
```

## `docker-compose`

Two services are declared:

| Service | Image | Ports | Notes |
|---|---|---|---|
| `deep_agent` | `deep_agent:local` (built from `./Dockerfile`) | `${DEEP_AGENT_PORT:-8010}:8000` | FastAPI + SSE; bind-mounts `./examples` read-only so seed data is hot-reloadable |
| `frontend` | `deep_agent_frontend:local` (built from `./frontend/Dockerfile`) | `${FRONTEND_PORT:-3000}:3000` | nginx serving the Vite build; proxies `/api/*` to the backend |

```bash
docker compose up -d                 # both services
docker compose up -d deep_agent      # backend only (skip UI)
```

The frontend `depends_on` the backend's health check, so it won't start
until `GET /health` returns 200 inside the backend container.

## Deploying to a container platform

Both images (the FastAPI backend and the nginx-served frontend) are plain
containers, so they run on any container orchestrator. See
[DEPLOY.md](./DEPLOY.md#deploying-to-a-container-platform) for the
vendor-neutral guide.

## Streaming stack

Separate compose file under `streaming/`. See
[streaming.md](streaming.md).

## LangGraph Platform

`langgraph.json` at the repo root declares:

```json
{
  "dependencies": ["."],
  "graphs": { "deep_agent": "./src/deep_agent/graph.py:build_graph" },
  "env": ".env",
  "python_version": "3.11"
}
```

`uv run langgraph dev` opens Studio locally; the manifest is already
compatible with LangGraph Platform.

## Production checklist

- Atlas cluster M10+ (Vector Search dedicated nodes)
- `ensure_indexes()` run once from `deep_agent_admin` role, then rotate
- RBAC split per [operators/rbac-example.md](operators/rbac-example.md)
- IP access list restricted to runtime VPC + CI
- Secrets via AWS Secrets Manager / Vault, not `env_file`
- `LANGSMITH_TRACING=true` in non-dev environments
- Never set `ALLOW_INSECURE=true` outside local dev
- Alert on `max_steps` warning logs - a deep-agent planning loop that hits
  the step bound usually means prompt drift
