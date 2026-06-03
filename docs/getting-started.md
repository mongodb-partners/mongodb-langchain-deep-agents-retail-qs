# Getting Started

From clean checkout to a working Agent Cartsmith shopping turn in about ten minutes.

## Prerequisites

- Python 3.11 or later
- [`uv`](https://github.com/astral-sh/uv)
- Atlas cluster at tier **M10 or higher** (Vector Search dedicated nodes)
- [Voyage AI API key](https://docs.voyageai.com/docs/api-key-and-installation)
- AWS credentials with Bedrock access (default LLM)
- [Tavily API key](https://tavily.com) (optional; web research)
- S3 bucket + IAM creds (VFS blobs land in S3)

## 1. Install

```bash
git clone <this-repo>
cd mongodb-langchain-deep-agents
uv sync --extra dev
```

## 2. Configure

```bash
cp .env.example .env
```

Required keys:

```bash
MONGODB_URI=mongodb+srv://USER:PASS@cluster.mongodb.net/?retryWrites=true&w=majority
MONGODB_DB=agent_cartsmith_retail_demo
VOYAGE_API_KEY=pa-...
TAVILY_API_KEY=tvly-...   # optional; web research
```

See [configuration.md](configuration.md) for the full list.

## 3. Create indexes

```bash
uv run python -c "from deep_agent.persistence.indexes import ensure_indexes; ensure_indexes()"
```

Idempotent; Atlas Vector Search indexes take ~1 minute to become queryable.

## 4. Seed the knowledge base + operational data

```bash
uv run deep-agent seed
```

Fixtures under `examples/retail_assistant/seeds/` are the Agent
Cartsmith retail grocery corpora. To retarget at another vertical, fork
the repo and edit the prompts in `src/deep_agent/agents/` plus the seed
corpora here.

## 5. Run a shopping turn

```bash
uv run deep-agent chat --user alice --once "Plan a budget-friendly weeknight pasta dinner for four and add the ingredients to my cart."
```

The main agent will typically: write a todo list → delegate to the
`researcher` subagent (which searches the KB, then the web if needed) →
compose the final answer. `AgentLogMiddleware` writes a
denormalized snapshot of every super-step into `agent_log`.

Or start the HTTP server:

```bash
uv run deep-agent serve --port 8000
```

Endpoints:

- `POST /chat` - SSE stream (required `user_id`, optional `thread_id`)
- `POST /feedback` - persist user scores (optionally mirrored to LangSmith)
- `GET /health` - pings MongoDB

Or open LangGraph Studio:

```bash
uv run langgraph dev
```

## 6. Run the test suite

```bash
uv run pytest
```

~405 hermetic tests (run `uv run pytest --collect-only -q` for the exact
count). Integration tier (requires `ATLAS_URI`) lives
under `tests/integration/`; see [testing.md](testing.md) for the full matrix.

## 7. Upload the starter eval dataset (optional)

```bash
uv run python scripts/create_evals_dataset.py   # uploads agent-cartsmith-retail-demo by default
uv run deep-agent-evals --dataset agent-cartsmith-retail-demo
```

See [langsmith-showcase.md](langsmith-showcase.md) for the full tracing +
feedback + experiment walkthrough.

The uploader is idempotent; re-running it tops up the LangSmith dataset
with any new rows in the local fixture. See [evals.md](evals.md).

## What next

- [Architecture](architecture.md)
- [Configuration](configuration.md)
- [VFS Backends](vfs-backends.md) - S3 backend, IAM, contract suite
- [MongoDB Backend](mongodb-backend.md) - how the filesystem tools persist
- [Streaming](streaming.md) - Kafka + Atlas Stream Processing
- [Evals](evals.md) - starter dataset + custom evaluators
