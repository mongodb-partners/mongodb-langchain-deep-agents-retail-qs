#!/usr/bin/env python3
"""Refresh the pre-extracted knowledge-graph artifact (maintainer tool).

Why
---
``examples/retail_assistant/seeds/knowledge_graph.json`` is human-editable PROSE.
Turning it into a graph requires an LLM (entity/relationship extraction), which
is slow and needs Bedrock credentials. We do NOT want that on every fresh deploy.

So we extract ONCE here and commit the result to
``knowledge_graph.entities.json`` — raw :class:`MongoDBGraphStore` entity
documents (``_id``/``type``/``attributes``/``relationships``). ``seed_all`` then
loads that artifact directly (``seed_knowledge_graph_entities``), so fresh seeds
need no LLM and no model credentials. See ``src/deep_agent/ingestion/seed.py``.

Run this only when the prose ``knowledge_graph.json`` changes (e.g. after
``scripts/generate_retail_catalog.py`` regenerates it for a new catalog).

Requirements
------------
- A reachable MongoDB (``MONGODB_URI`` / ``MONGODB_DB`` from ``.env``).
- Valid LLM credentials. AWS Bedrock keys are read from the process env; this
  script loads ``AWS_*`` from ``.env`` automatically (boto3 does not read ``.env``).

Run
---
    python scripts/extract_knowledge_graph.py

It clears the live ``knowledge_graph`` collection + its seed-ledger, re-extracts
from the prose, then writes the artifact. Clearing keeps the artifact a faithful
1:1 image of the current prose (no stale entities from earlier extractions).
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SEEDS = REPO / "examples" / "retail_assistant" / "seeds"
ARTIFACT = SEEDS / "knowledge_graph.entities.json"


def _load_aws_creds_from_env_file() -> list[str]:
    """boto3 reads creds from the process env, not ``.env`` — bridge them over."""
    import os

    env_path = REPO / ".env"
    if not env_path.exists():
        return []
    loaded: list[str] = []
    pat = re.compile(
        r"^(AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN"
        r"|AWS_DEFAULT_REGION|AWS_REGION)=(.*)$"
    )
    for line in env_path.read_text().splitlines():
        m = pat.match(line.strip())
        if m:
            os.environ[m.group(1)] = m.group(2).strip().strip('"').strip("'")
            loaded.append(m.group(1))
    return loaded


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    loaded = _load_aws_creds_from_env_file()
    if loaded:
        logging.info("loaded %s from .env for the extraction LLM", loaded)

    # Imported after creds are in the env so the Bedrock client picks them up.
    from deep_agent.config import get_settings
    from deep_agent.ingestion.seed import seed_knowledge_graph
    from deep_agent.persistence.mongo import get_db

    s = get_settings()
    db = get_db()
    coll = db[s.knowledge_graph_collection]

    # Clear so the artifact mirrors the current prose exactly (1:1, no leftovers).
    coll.delete_many({})
    db["knowledge_graph_seed_log"].delete_many({})

    n_docs = seed_knowledge_graph()  # LLM extraction → writes entities to `coll`
    entities = list(coll.find({}))

    # Normalise key order + stable sort for clean, reviewable diffs.
    def norm(d: dict) -> dict:
        return {
            "_id": d["_id"],
            "type": d.get("type"),
            "attributes": d.get("attributes", {}),
            "relationships": d.get("relationships", {}),
        }

    entities = sorted((norm(d) for d in entities), key=lambda d: d["_id"])
    ARTIFACT.write_text(
        json.dumps(entities, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # Sanity: graph closure (every relationship target has a node) + neutrality.
    ids = {e["_id"] for e in entities}
    dangling = sorted(
        {t for e in entities for t in e["relationships"].get("target_ids", []) if t not in ids}
    )
    ban = re.compile(r"\b(telco|gep|finance|healthcare|phi)\b", re.IGNORECASE)
    banned = ban.findall(ARTIFACT.read_text())

    print(f"extracted {n_docs} prose docs -> {len(entities)} entities")
    print(f"wrote {ARTIFACT.relative_to(REPO)}")
    print(f"dangling target_ids: {dangling or 'none'}")
    print(f"banned-term hits: {banned or 'none'}")
    if dangling or banned:
        raise SystemExit("artifact failed validation (see above)")


if __name__ == "__main__":
    main()
