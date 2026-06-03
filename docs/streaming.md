# Streaming

Kafka → Atlas Stream Processing → `stream_events` → change-stream worker →
`knowledge_base`. Optional; the core graph works without it.

If you see `stream_events` empty in Atlas, that's expected whenever you
deploy without the Kafka producer + Atlas SP + `stream_worker` pieces
below. `ensure_indexes()` still provisions the collection and the 30-day
TTL so that turning the pipeline on later is a pure config change (no DDL).

## Flow

```
 streaming/producer.py (synthetic)
        │
        ▼
   Kafka topic "events"
        │
        ▼
   Atlas Stream Processing
     $source  - reads from Kafka
     $tumblingWindow  - 10s group-by event_type
     $merge  - upsert into deep_agent.stream_events
        │
        ▼
   stream_events collection
        │  change stream
        ▼
   deep_agent.ingestion.stream_worker.run_once
        │
        ▼
   knowledge_base (vector + $search)
```

## Components

| File | Purpose |
|---|---|
| `streaming/docker-compose.yml` | Local Kafka + producer |
| `streaming/producer.py` | Synthetic event emitter (`EVENT_RATE_HZ`, default 2 Hz) |
| `streaming/atlas_sp_pipeline.js` | Canonical `sp.createStreamProcessor` spec |
| `src/deep_agent/ingestion/asp.py` | Python helpers (`default_pipeline_spec`, `register_pipeline`, `stop_pipeline`) |
| `src/deep_agent/ingestion/stream_worker.py` | Change-stream consumer with resume tokens + backoff |

## Running the demo

### 1. Start local Kafka + producer

```bash
cd streaming
docker compose up -d
```

### 2. Register the stream processor

Either via mongosh:

```bash
mongosh "<SP-URI>" streaming/atlas_sp_pipeline.js
```

Or from Python:

```python
from deep_agent.ingestion import asp
spec = asp.default_pipeline_spec()
asp.register_pipeline("<SP-URI>", pipeline_name="deep_agent_events_to_atlas", pipeline_spec=spec)
```

`register_pipeline` is idempotent - re-registering the same processor is a no-op.

### 3. Run the change-stream worker

```bash
uv run python -m deep_agent.ingestion.stream_worker
```

Resume-token location precedence:

1. `$DEEP_AGENT_STATE_DIR/stream_resume_token.json`
2. `$XDG_STATE_HOME/deep_agent/stream_resume_token.json`
3. `~/.deep_agent/stream_resume_token.json`

## Tearing down

```bash
python -c "from deep_agent.ingestion.asp import stop_pipeline; stop_pipeline('<SP-URI>', 'deep_agent_events_to_atlas')"
cd streaming && docker compose down
```

`stop_pipeline` is idempotent.
