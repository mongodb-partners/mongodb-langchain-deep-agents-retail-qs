"""Sub-phase 02: model factories (get_llm/get_embeddings/get_reranker)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    from deep_agent import config, models

    config.get_settings.cache_clear()
    models.get_llm.cache_clear()
    models.get_embeddings.cache_clear()
    models.get_reranker.cache_clear()


def test_TC_02_040_get_llm_delegates_to_init_chat_model() -> None:
    from deep_agent import models

    with patch("deep_agent.models.init_chat_model") as icm:
        icm.return_value = MagicMock()
        models.get_llm()
    _, kwargs = icm.call_args
    assert kwargs["model"] == "global.anthropic.claude-sonnet-4-6"
    assert kwargs["model_provider"] == "bedrock"
    assert kwargs["region_name"] == "us-east-1"
    assert kwargs["temperature"] == 0


def test_TC_02_050_asymmetric_embedder_routes_doc_and_query() -> None:
    from deep_agent.models import AsymmetricVoyageEmbeddings

    doc = MagicMock()
    doc.embed_documents.return_value = [[0.1, 0.2]]
    query = MagicMock()
    query.embed_query.return_value = [0.9, 0.8]

    emb = AsymmetricVoyageEmbeddings(doc, query)
    assert emb.embed_documents(["hello"]) == [[0.1, 0.2]]
    doc.embed_documents.assert_called_once_with(["hello"])
    query.embed_documents.assert_not_called()

    assert emb.embed_query("world") == [0.9, 0.8]
    query.embed_query.assert_called_once_with("world")
    doc.embed_query.assert_not_called()


def test_TC_02_060_reranker_factory() -> None:
    from deep_agent import models

    with patch("deep_agent.models.VoyageAIRerank") as vr:
        vr.return_value = MagicMock()
        models.get_reranker()
    _, kwargs = vr.call_args
    assert kwargs["model"] == "rerank-2.5"
    # API key passed through as SecretStr
    assert "voyage_api_key" in kwargs


def test_TC_02_070_voyage_base_url_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOYAGE_BASE_URL", "https://ai.mongodb.com/v1")
    from deep_agent import models

    with patch("deep_agent.models.VoyageAIEmbeddings") as vem, patch(
        "deep_agent.models.VoyageAIRerank"
    ) as vr:
        vem.return_value = MagicMock()
        vr.return_value = MagicMock()
        models.get_embeddings()
        models.get_reranker()

    for call in vem.call_args_list:
        _, kw = call
        assert kw.get("base_url") == "https://ai.mongodb.com/v1"
    _, kw = vr.call_args
    assert kw.get("base_url") == "https://ai.mongodb.com/v1"


# --- LLM cache retired: get_llm attaches no cache --------


def test_TC_540_B01_get_llm_attaches_no_cache() -> None:
    """get_llm() returns init_chat_model's result
    unchanged and never assigns an LLM cache (the cache is retired)."""
    from deep_agent import models

    class _Chat:  # plain object so a stray .cache assignment is observable
        pass

    chat = _Chat()
    with patch("deep_agent.models.init_chat_model", return_value=chat):
        out = models.get_llm()
    assert out is chat
    assert not hasattr(out, "cache"), "get_llm must not attach an LLM cache"
