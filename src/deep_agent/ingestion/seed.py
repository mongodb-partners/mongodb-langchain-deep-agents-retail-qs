"""Seed loader - idempotently populates the knowledge base, knowledge graph,
and operational data.

Reads JSON fixtures from ``Settings.seeds_dir`` (default
``examples/research_assistant/seeds``) and writes to ``Settings.mongodb_db``.
Vertical apps fork the repo, swap the seed directory, and rerun this loader.

Every seeder is safe to re-run on a populated database. Operational rows
upsert by their NATURAL key (product_id/customer_id/order_id/code) — MongoDB
owns the ObjectId ``_id``; KB chunks dedupe by content hash; graph triples
retry per-doc and skip on parse failure.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from langchain_core.documents import Document

from ..config import get_settings
from ..persistence.graph_store import build_graph_store
from ..persistence.mongo import get_db
from ..persistence.vector_store import build_vector_store

log = logging.getLogger(__name__)


class SeedIncompleteError(RuntimeError):
    """A seed run left a collection with fewer documents than its fixture.

    The realistic trigger is a transient Atlas failover mid-seed: writes acked
    by a primary that then steps down can roll back, and ``deep-agent seed``
    still exits 0 — so ``deploy.sh`` reports success over a half-loaded catalog.
    :func:`seed_all` reads every deterministic fixture back from the database
    after writing and raises this, so the non-zero exit trips the deploy's
    cleanup trap instead of leaving partial data behind a success message.
    """


def _default_seeds_dir() -> Path:
    return get_settings().seeds_dir


def _read_json(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON array")
    return data


def _kb_chunk_hash(text: str, metadata: dict[str, Any]) -> str:
    """Stable content hash used to dedupe seeded chunks across re-runs."""
    payload = json.dumps(
        {"text": text, "metadata": metadata}, sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def seed_knowledge_base(*, seeds_dir: Path | None = None) -> int:
    """Load ``knowledge_base.json`` and write each doc as an embedded chunk.

    Idempotent. Each chunk's content hash is stamped into
    ``metadata.seed_hash`` and we skip chunks whose hash is already
    present in the collection — so re-running the seeder never produces
    duplicates and never re-pays for embeddings.
    """
    seeds_dir = seeds_dir or _default_seeds_dir()
    path = seeds_dir / "knowledge_base.json"
    if not path.exists():
        log.info("no knowledge_base.json at %s; skipping", path)
        return 0
    docs = _read_json(path)

    s = get_settings()
    coll = get_db()[s.knowledge_base_collection]

    texts: list[str] = []
    metadatas: list[dict[str, Any]] = []
    skipped = 0
    for d in docs:
        text = d["text"]
        md = dict(d.get("metadata", {}))
        h = _kb_chunk_hash(text, md)
        md["seed_hash"] = h
        # langchain-mongodb's MongoDBAtlasVectorSearch.add_texts flattens
        # metadata as top-level fields rather than nesting under
        # ``metadata`` — the dedupe predicate looks at the top-level
        # ``seed_hash`` field directly.
        if coll.count_documents({"seed_hash": h}, limit=1):
            skipped += 1
            continue
        texts.append(text)
        metadatas.append(md)

    if not texts:
        log.info(
            "knowledge_base seed: 0 new chunks (%d already present)", skipped
        )
        return 0

    vs = build_vector_store()
    vs.add_texts(texts, metadatas=metadatas)
    log.info(
        "seeded %d new knowledge_base chunks (%d already present)",
        len(texts),
        skipped,
    )
    return len(texts)


def seed_knowledge_graph(*, seeds_dir: Path | None = None) -> int:
    """Load ``knowledge_graph.json`` and extract triples via :class:`MongoDBGraphStore`.

    Triple extraction runs the LLM; if the LLM output cannot be parsed as
    JSON for a given document, that document is skipped and seeding
    continues. This keeps the pipeline resilient to model-output drift.

    Idempotent. We stamp ``metadata.seed_hash`` per source row and persist a
    tiny ledger doc in ``knowledge_graph_seed_log`` so
    re-runs skip rows already extracted. Skipping early avoids re-paying
    the LLM-extraction cost on every deploy.
    """
    seeds_dir = seeds_dir or _default_seeds_dir()
    path = seeds_dir / "knowledge_graph.json"
    if not path.exists():
        log.info("no knowledge_graph.json at %s; skipping", path)
        return 0
    rows = _read_json(path)

    db = get_db()
    ledger = db["knowledge_graph_seed_log"]

    gs = build_graph_store()
    ok = 0
    skipped = 0
    for r in rows:
        text = r["text"]
        md = dict(r.get("metadata", {}))
        h = _kb_chunk_hash(text, md)
        if ledger.count_documents({"_id": h}, limit=1):
            skipped += 1
            continue
        md["seed_hash"] = h
        try:
            gs.add_documents([Document(page_content=text, metadata=md)])
            ledger.replace_one({"_id": h}, {"_id": h}, upsert=True)
            ok += 1
        except Exception as exc:
            log.warning("skipping knowledge_graph doc (%s): %s", md, exc)
    log.info(
        "seeded %d/%d knowledge_graph docs (%d already present)",
        ok,
        len(rows),
        skipped,
    )
    return ok


def seed_knowledge_graph_entities(*, seeds_dir: Path | None = None) -> int | None:
    """Bulk-load a PRE-EXTRACTED knowledge graph from ``knowledge_graph.entities.json``.

    The LLM entity-extraction in :func:`seed_knowledge_graph` is expensive and
    needs model credentials at deploy time. To keep fresh seeds fast and
    LLM-free, we commit the already-extracted entity documents (produced by
    ``scripts/extract_knowledge_graph.py``) and insert them verbatim. Each
    document is a
    :class:`MongoDBGraphStore` entity — ``_id`` is the entity name, plus
    ``type`` / ``attributes`` / ``relationships`` — and graph traversal
    (``$graphLookup`` on ``relationships.target_ids`` → ``_id``) reads exactly
    this shape, so a direct insert reproduces what extraction would have written.

    Idempotent — ``replace_one({_id}, upsert=True)`` per entity, so re-running
    never duplicates. Returns the number of entities written, or
    ``None`` when the artifact is absent (signalling the caller to fall back to
    live LLM extraction from ``knowledge_graph.json``).
    """
    seeds_dir = seeds_dir or _default_seeds_dir()
    path = seeds_dir / "knowledge_graph.entities.json"
    if not path.exists():
        return None
    entities = _read_json(path)
    coll = get_db()[get_settings().knowledge_graph_collection]
    for ent in entities:
        coll.replace_one({"_id": ent["_id"]}, ent, upsert=True)
    log.info(
        "seeded %d pre-extracted knowledge_graph entities (no LLM)", len(entities)
    )
    return len(entities)


_NATURAL_KEYS = {
    "products": "product_id",
    "customers": "customer_id",
    "orders": "order_id",
    "promotions": "code",
}


def seed_operational_data(*, seeds_dir: Path | None = None) -> dict[str, int]:
    """Upsert each ``operational/<name>.json`` row into the collection ``<name>``.

    Seed rows no longer carry an explicit ``_id`` — that field is MongoDB's
    autogenerated ObjectId. Each collection has a NATURAL key
    (``products.product_id``, ``customers.customer_id``, ``orders.order_id``,
    ``promotions.code``); we ``ReplaceOne({key: value}, upsert=True)`` on it so
    re-running the seeder is idempotent (no duplicates, no ``E11000``) while
    MongoDB owns ``_id``. Files without a known natural key dedupe by a
    content-hash stored under ``seed_key`` (never ``_id``). Returns the number
    of rows touched per collection.
    """
    seeds_dir = seeds_dir or _default_seeds_dir()
    op_dir = seeds_dir / "operational"
    if not op_dir.exists():
        log.info("no operational seeds at %s; skipping", op_dir)
        return {}
    counts: dict[str, int] = {}
    db = get_db()
    for fp in sorted(op_dir.glob("*.json")):
        rows = _read_json(fp)
        if not rows:
            counts[fp.stem] = 0
            continue
        coll = db[fp.stem]
        key = _NATURAL_KEYS.get(fp.stem)
        for row in rows:
            if key is not None and row.get(key) is not None:
                coll.replace_one({key: row[key]}, row, upsert=True)
            else:
                # No known natural key: dedupe by a content hash under
                # ``seed_key`` so re-runs stay idempotent without setting _id.
                payload = json.dumps(row, sort_keys=True, ensure_ascii=False)
                seed_key = hashlib.sha256(payload.encode("utf-8")).hexdigest()
                coll.replace_one(
                    {"seed_key": seed_key}, {**row, "seed_key": seed_key}, upsert=True
                )
        counts[fp.stem] = len(rows)
    log.info("seeded operational collections: %s", counts)
    return counts


def _verify_seeded(seeds_dir: Path) -> None:
    """Read every deterministic fixture back from the DB; raise on a shortfall.

    This is independent of the upsert acknowledgements — a ``count_documents``
    read-back catches writes that were acked but lost to a failover rollback,
    which is the exact way a partial seed slips past a 0 exit code. Lossy paths
    (the LLM extraction in :func:`seed_knowledge_graph`, which legitimately
    skips unparseable rows) are excluded; only fixtures with an exact expected
    row count are checked. Each check uses ``>=`` so pre-existing or extra rows
    never cause a false failure.
    """
    db = get_db()
    s = get_settings()
    shortfalls: list[str] = []

    def _check(coll_name: str, field: str, ids: list[Any]) -> None:
        if not ids:
            return
        present = db[coll_name].count_documents({field: {"$in": ids}})
        if present < len(ids):
            shortfalls.append(f"{coll_name}: {present}/{len(ids)} present")

    # Operational collections — exact match on the natural key (or, for files
    # without one, the same content hash the seeder dedupes on).
    op_dir = seeds_dir / "operational"
    if op_dir.exists():
        for fp in sorted(op_dir.glob("*.json")):
            rows = _read_json(fp)
            key = _NATURAL_KEYS.get(fp.stem)
            if key is not None:
                _check(fp.stem, key, [r[key] for r in rows if r.get(key) is not None])
            else:
                _check(
                    fp.stem,
                    "seed_key",
                    [
                        hashlib.sha256(
                            json.dumps(r, sort_keys=True, ensure_ascii=False).encode(
                                "utf-8"
                            )
                        ).hexdigest()
                        for r in rows
                    ],
                )

    # Knowledge base — one chunk per fixture row, deduped on top-level seed_hash.
    kb_path = seeds_dir / "knowledge_base.json"
    if kb_path.exists():
        _check(
            s.knowledge_base_collection,
            "seed_hash",
            [_kb_chunk_hash(d["text"], dict(d.get("metadata", {}))) for d in _read_json(kb_path)],
        )

    # Knowledge graph — only the pre-extracted entity artifact is exact (keyed
    # by entity name in ``_id``); the LLM fallback is intentionally not checked.
    ent_path = seeds_dir / "knowledge_graph.entities.json"
    if ent_path.exists():
        _check(s.knowledge_graph_collection, "_id", [e["_id"] for e in _read_json(ent_path)])

    if shortfalls:
        raise SeedIncompleteError(
            "seed incomplete (likely interrupted by an Atlas failover) — "
            + "; ".join(shortfalls)
        )


def seed_all(*, seeds_dir: Path | None = None) -> dict[str, Any]:
    """Run the three seeders in order and return a summary.

    The knowledge graph prefers the committed pre-extracted artifact
    (``knowledge_graph.entities.json``) so fresh deploys need no LLM and no
    model credentials; it falls back to live LLM extraction from
    ``knowledge_graph.json`` only when that artifact is absent.

    After writing, every deterministic fixture is read back from the database
    (:func:`_verify_seeded`); a shortfall raises :class:`SeedIncompleteError`
    so an interrupted seed exits non-zero instead of reporting success.
    """
    kb = seed_knowledge_base(seeds_dir=seeds_dir)
    kg = seed_knowledge_graph_entities(seeds_dir=seeds_dir)
    if kg is None:
        kg = seed_knowledge_graph(seeds_dir=seeds_dir)
    op = seed_operational_data(seeds_dir=seeds_dir)
    _verify_seeded(seeds_dir or _default_seeds_dir())
    return {"knowledge_base": kb, "knowledge_graph": kg, "operational": op}


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(seed_all())
