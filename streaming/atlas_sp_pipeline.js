// Atlas Stream Processing pipeline
//
// Run from mongosh connected to your Stream Processing instance:
//
//     mongosh <sp-connection-uri> atlas_sp_pipeline.js
//
// Required connection registry entries:
//   - "kafka_conn": Kafka broker (matches streaming/docker-compose.yml)
//   - "atlas_conn": your Atlas cluster, database `deep_agent`
//
// Pipeline semantics:
//   1. $source          — read from the Kafka topic `events`
//   2. $tumblingWindow  — 10-second window grouping by event_type, retains latest payload
//   3. $merge           — upsert the rolled-up events into deep_agent.stream_events
//
// The change-stream worker in src/deep_agent/ingestion/stream_worker.py watches
// deep_agent.stream_events and feeds new rows into the knowledge_base vector store.

sp.createStreamProcessor(
  "deep_agent_events_to_atlas",
  [
    {
      $source: {
        connectionName: "kafka_conn",
        topic: "events",
        timeField: { $dateFromString: { dateString: "$ts" } }
      }
    },
    {
      $tumblingWindow: {
        interval: { size: NumberInt(10), unit: "second" },
        pipeline: [
          {
            $group: {
              _id: "$event_type",
              count: { $sum: 1 },
              last: { $last: "$$ROOT" }
            }
          }
        ]
      }
    },
    {
      $merge: {
        into: {
          connectionName: "atlas_conn",
          db: "deep_agent",
          coll: "stream_events"
        },
        on: "_id",
        whenMatched: "merge",
        whenNotMatched: "insert"
      }
    }
  ]
);

sp.deep_agent_events_to_atlas.start();
print("Started stream processor: deep_agent_events_to_atlas");
