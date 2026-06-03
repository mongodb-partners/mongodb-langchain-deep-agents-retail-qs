"""Synthetic Kafka event producer — feeds the Atlas Stream Processing pipeline.

Emits JSON events onto topic ``events`` at a configurable rate. Run via
docker-compose (``streaming/docker-compose.yml``) or locally against a Kafka
broker.

Environment:
- ``KAFKA_BOOTSTRAP_SERVERS`` (default ``localhost:9092``)
- ``KAFKA_TOPIC``             (default ``events``)
- ``EVENT_RATE_HZ``           (default ``2``)
"""
from __future__ import annotations

import json
import os
import random
import time
import uuid
from datetime import UTC, datetime

from kafka import KafkaProducer

BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
TOPIC = os.environ.get("KAFKA_TOPIC", "events")
RATE = float(os.environ.get("EVENT_RATE_HZ", "2"))

EVENT_TYPES = ["research.finding.published", "kb.article.updated", "docs.release.announced"]
SAMPLE_TEXTS = [
    "New research note: agent planning loops benefit from persisted todo lists.",
    "Article update: benchmarking RRF hybrid search against pure vector retrieval.",
    "Release notes: MongoDBSaver checkpoint compaction landed in 0.3.1.",
    "Finding: Voyage-4-lite retains recall when paired with voyage-4 ingestion.",
]


def _build_event() -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": random.choice(EVENT_TYPES),
        "ts": datetime.now(UTC).isoformat(),
        "text": random.choice(SAMPLE_TEXTS),
        "source": "synthetic-producer",
    }


def produce(topic: str = TOPIC, bootstrap: str = BOOTSTRAP, rate_hz: float = RATE) -> None:
    """Produce events to ``topic`` forever at ``rate_hz`` messages per second."""
    producer = KafkaProducer(
        bootstrap_servers=bootstrap,
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        acks="all",
        linger_ms=50,
    )
    interval = 1.0 / max(rate_hz, 0.1)
    print(f"[producer] sending to {bootstrap} topic={topic} at {rate_hz} Hz")
    try:
        while True:
            event = _build_event()
            producer.send(topic, event)
            print(f"[producer] sent {event['event_id']} type={event['event_type']}")
            time.sleep(interval)
    finally:
        producer.flush()
        producer.close()


if __name__ == "__main__":  # pragma: no cover
    produce()
