"""Sub-phase 14: seed loader tests."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SEED_ROOT = (
    Path(__file__).resolve().parents[2]
    / "examples"
    / "retail_assistant"
    / "seeds"
)


@pytest.fixture(autouse=True)
def _clear_settings() -> None:
    from deep_agent import config

    config.get_settings.cache_clear()


def test_TC_14_010_seed_knowledge_base_writes_via_add_texts() -> None:
    import mongomock

    from deep_agent.ingestion import seed as seeder

    fake_vs = MagicMock()
    db = mongomock.MongoClient()["deep_agent_test"]
    with patch(
        "deep_agent.ingestion.seed.build_vector_store", return_value=fake_vs
    ), patch("deep_agent.ingestion.seed.get_db", return_value=db):
        count = seeder.seed_knowledge_base()

    assert count == len(json.loads((SEED_ROOT / "knowledge_base.json").read_text()))
    args, kwargs = fake_vs.add_texts.call_args
    # Positional or keyword — tolerate both shapes
    texts = args[0] if args else kwargs.get("texts", [])
    assert len(texts) == count


def test_TC_14_011_seed_knowledge_graph_writes_documents() -> None:
    import mongomock
    from langchain_core.documents import Document

    from deep_agent.ingestion import seed as seeder

    fake_gs = MagicMock()
    db = mongomock.MongoClient()["deep_agent_test"]
    with patch(
        "deep_agent.ingestion.seed.build_graph_store", return_value=fake_gs
    ), patch("deep_agent.ingestion.seed.get_db", return_value=db):
        count = seeder.seed_knowledge_graph()

    assert count == len(json.loads((SEED_ROOT / "knowledge_graph.json").read_text()))
    args, _ = fake_gs.add_documents.call_args
    docs = args[0]
    assert all(isinstance(d, Document) for d in docs)


def test_TC_14_012_seed_operational_inserts_per_file() -> None:
    import mongomock

    from deep_agent.ingestion import seed as seeder

    db = mongomock.MongoClient()["deep_agent_test"]
    with patch("deep_agent.ingestion.seed.get_db", return_value=db):
        counts = seeder.seed_operational_data()

    # The operational seed set now also includes `promotions`.
    assert set(counts.keys()) == {"customers", "orders", "products", "promotions"}
    for name, n in counts.items():
        assert db[name].count_documents({}) == n
        assert n > 0


def test_TC_14_013_seed_all_returns_composite_summary() -> None:
    import mongomock

    from deep_agent.ingestion import seed as seeder

    fake_vs = MagicMock()
    fake_gs = MagicMock()
    db = mongomock.MongoClient()["deep_agent_test"]

    # seed_all() now reads every deterministic fixture back from the DB
    # (_verify_seeded). The real vector store persists KB chunks to the
    # collection; the fake must mirror that — stamping each metadata dict as
    # top-level fields (incl. seed_hash) — or the read-back sees 0 KB docs.
    def _persist_kb(*args, **kwargs) -> None:
        metadatas = kwargs.get("metadatas", args[1] if len(args) > 1 else [])
        for md in metadatas:
            db["knowledge_base"].insert_one({**md})

    fake_vs.add_texts.side_effect = _persist_kb

    with patch("deep_agent.ingestion.seed.build_vector_store", return_value=fake_vs), patch(
        "deep_agent.ingestion.seed.build_graph_store", return_value=fake_gs
    ), patch("deep_agent.ingestion.seed.get_db", return_value=db):
        summary = seeder.seed_all()

    assert set(summary.keys()) == {"knowledge_base", "knowledge_graph", "operational"}
    assert isinstance(summary["operational"], dict)
    assert summary["knowledge_base"] > 0
    assert summary["knowledge_graph"] > 0


def test_TC_14_015_seed_all_raises_when_seed_interrupted() -> None:
    """A partial seed (e.g. an Atlas failover mid-load) must raise
    SeedIncompleteError so ``deep-agent seed`` exits non-zero and
    ``deploy.sh`` tears the stack down instead of reporting success."""
    import mongomock

    from deep_agent.ingestion import seed as seeder

    fake_vs = MagicMock()
    fake_gs = MagicMock()
    db = mongomock.MongoClient()["deep_agent_test"]

    # Vector store writes are dropped on the floor — simulating KB chunks that
    # were acked by a primary that then stepped down and rolled them back.
    fake_vs.add_texts.side_effect = lambda *a, **k: None

    with patch(
        "deep_agent.ingestion.seed.build_vector_store", return_value=fake_vs
    ), patch(
        "deep_agent.ingestion.seed.build_graph_store", return_value=fake_gs
    ), patch(
        "deep_agent.ingestion.seed.get_db", return_value=db
    ), pytest.raises(seeder.SeedIncompleteError, match="knowledge_base"):
        seeder.seed_all()


def test_TC_14_020_seeds_are_domain_neutral() -> None:
    """Seed fixtures must stay industry-agnostic — they honor the
    domain-swap contract."""
    import re

    forbidden = [r"\btelco\b", r"\bgep\b", r"\bfinance\b", r"\bhealthcare\b", r"\bphi\b"]
    offenders: list[tuple[str, str]] = []
    for path in SEED_ROOT.rglob("*.json"):
        text = path.read_text(encoding="utf-8").lower()
        for pat in forbidden:
            if re.search(pat, text):
                offenders.append((str(path.relative_to(SEED_ROOT.parents[3])), pat))
    assert not offenders, f"seed fixtures leaked industry terms: {offenders}"


def test_TC_E_508_010_seed_operational_is_idempotent() -> None:
    """Re-running the operational seeder against an
    already-populated DB must not raise E11000 dup-key. Each row
    upserts by its natural key (product_id/customer_id/order_id/code);
    collection counts stay stable."""
    import mongomock

    from deep_agent.ingestion import seed as seeder

    db = mongomock.MongoClient()["deep_agent_test"]
    with patch("deep_agent.ingestion.seed.get_db", return_value=db):
        first = seeder.seed_operational_data()
        # Second pass must NOT throw — this is the regression that broke
        # `scripts/deploy.sh` when re-deploying onto an existing DB.
        second = seeder.seed_operational_data()

    assert first == second
    for name, n in first.items():
        assert db[name].count_documents({}) == n


def test_TC_E_508_011_seed_knowledge_base_skips_existing_chunks() -> None:
    """Re-running the KB seeder must skip chunks already
    present (matched by top-level ``seed_hash``) so we don't pay Voyage
    embedding cost on every deploy. ``MongoDBAtlasVectorSearch`` flattens
    metadata as top-level fields, so the dedupe predicate matches at
    that depth."""
    import mongomock

    from deep_agent.ingestion import seed as seeder

    db = mongomock.MongoClient()["deep_agent_test"]
    fake_vs = MagicMock()
    with patch(
        "deep_agent.ingestion.seed.build_vector_store", return_value=fake_vs
    ), patch("deep_agent.ingestion.seed.get_db", return_value=db):
        first = seeder.seed_knowledge_base()
        # Simulate the vector store actually persisting by stamping
        # each metadata dict as top-level fields on a doc, mirroring
        # what MongoDBAtlasVectorSearch.add_texts produces.
        call = fake_vs.add_texts.call_args
        metadatas = call.kwargs.get(
            "metadatas",
            call.args[1] if len(call.args) > 1 else [],
        )
        for md in metadatas:
            db["knowledge_base"].insert_one({**md})
        fake_vs.reset_mock()
        second = seeder.seed_knowledge_base()

    assert first > 0
    assert second == 0
    fake_vs.add_texts.assert_not_called()


def test_TC_14_014_missing_seed_file_is_noop() -> None:
    """A missing ``knowledge_base.json`` should return 0, not raise."""
    # Point at an empty directory by creating one
    import tempfile

    from deep_agent.ingestion import seed as seeder

    with tempfile.TemporaryDirectory() as tmp:
        assert seeder.seed_knowledge_base(seeds_dir=Path(tmp)) == 0
        assert seeder.seed_knowledge_graph(seeds_dir=Path(tmp)) == 0
        assert seeder.seed_operational_data(seeds_dir=Path(tmp)) == {}
