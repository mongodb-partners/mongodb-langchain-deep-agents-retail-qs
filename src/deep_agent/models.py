"""Shared model layer: LLM, embeddings, reranker.

Every agent / tool / retriever / cache obtains its LLM or embedder through the
factories here so swapping providers is a single-module change.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from langchain_voyageai import VoyageAIEmbeddings, VoyageAIRerank

from .config import get_settings

# Per-model output-token ceiling. The deepagents tool loop
# emits ``tool_use`` JSON as a streaming block; when the model hits its
# per-call ``max_tokens`` mid-emission, the JSON is truncated and the
# pydantic validator rejects the partial args (e.g. ``write_file``
# arriving with ``file_path`` but no ``content`` — the symptom is the
# model getting stuck in a "let me try a different approach" retry loop).
#
# 4096 is the universal floor (Llama 4 Maverick caps there) but it's
# too tight for Haiku/Sonnet/Opus to fit a write_file call carrying a
# multi-paragraph artifact in one shot. Map each verified Bedrock profile
# to its real safe ceiling; unknown profiles fall back to
# ``Settings.max_tokens`` so a vertical's custom model gets explicit
# operator control.
_MAX_TOKENS_BY_MODEL: dict[str, int] = {
    # Anthropic 4.x: real ceilings are 64k+, 8192 is plenty.
    "global.anthropic.claude-opus-4-7": 8192,
    "global.anthropic.claude-opus-4-6-v1": 8192,
    "us.anthropic.claude-opus-4-5-20251101-v1:0": 8192,
    "us.anthropic.claude-opus-4-1-20250805-v1:0": 8192,
    "global.anthropic.claude-sonnet-4-6": 8192,
    "global.anthropic.claude-sonnet-4-5-20250929-v1:0": 8192,
    "global.anthropic.claude-haiku-4-5-20251001-v1:0": 8192,
    # Claude 3.5 Haiku: 8192 hard cap.
    "us.anthropic.claude-3-5-haiku-20241022-v1:0": 8192,
    # Nova: Premier 32k, Pro/Lite 5000.
    "us.amazon.nova-premier-v1:0": 8192,
    "us.amazon.nova-pro-v1:0": 5000,
    "us.amazon.nova-lite-v1:0": 5000,
    # Mistral Pixtral Large: 8192.
    "us.mistral.pixtral-large-2502-v1:0": 8192,
    # Llama 4 Maverick: hard cap at 4096.
    "us.meta.llama4-maverick-17b-instruct-v1:0": 4096,
}


@lru_cache(maxsize=8)
def get_llm(model: str | None = None) -> BaseChatModel:
    """Return a configured chat model.

    The optional ``model`` argument lets callers override
    ``Settings.llm_model`` per request. The lru_cache is keyed on
    ``model`` so each chosen profile gets its own cached instance.

    Per-model ``max_tokens`` lookup so smaller models like
    Haiku get their real ceiling rather than the conservative
    Llama-compatibility floor.

    Uses :func:`langchain.chat_models.init_chat_model` so the provider can be
    flipped via ``LLM_PROVIDER`` / ``LLM_MODEL`` env vars without touching
    agent code.
    """
    s = get_settings()
    chosen = model or s.llm_model
    max_tokens = _MAX_TOKENS_BY_MODEL.get(chosen, s.max_tokens)
    kwargs: dict[str, Any] = {
        "model": chosen,
        "model_provider": s.llm_provider,
        "max_tokens": max_tokens,
    }
    # Claude Opus 4.7+ deprecates ``temperature``. Older models still accept it.
    if "opus-4-7" not in chosen:
        kwargs["temperature"] = 0
    if s.llm_provider == "bedrock":
        kwargs["region_name"] = s.aws_region
    llm: BaseChatModel = init_chat_model(**kwargs)
    # The prompt-level LLM cache is retired (superseded by the query-keyed
    # response cache). No LLM-level cache is attached here, and the deprecated
    # set_llm_cache process-global swap remains banned.
    return llm


class AsymmetricVoyageEmbeddings(Embeddings):
    """Embeddings that route ingestion and query to different Voyage models.

    ``voyage-4`` and ``voyage-4-lite`` share a common embedding space, so a
    document indexed with the high-capacity model is correctly retrievable by
    a query embedded with the latency-optimized lite model.
    """

    def __init__(
        self,
        document_model: VoyageAIEmbeddings,
        query_model: VoyageAIEmbeddings,
    ) -> None:
        self._document_model = document_model
        self._query_model = query_model

    @property
    def document_model(self) -> VoyageAIEmbeddings:
        return self._document_model

    @property
    def query_model(self) -> VoyageAIEmbeddings:
        return self._query_model

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._document_model.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._query_model.embed_query(text)

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._document_model.aembed_documents(texts)

    async def aembed_query(self, text: str) -> list[float]:
        return await self._query_model.aembed_query(text)


@lru_cache(maxsize=1)
def get_embeddings() -> Embeddings:
    """Return an asymmetric Voyage embedder.

    Documents embedded with ``VOYAGE_DOCUMENT_MODEL`` (default ``voyage-4``);
    queries embedded with ``VOYAGE_QUERY_MODEL`` (default ``voyage-4-lite``).
    ``VOYAGE_BASE_URL`` (if set) routes both embedders through a gateway.
    """
    s = get_settings()
    # Pass the configured dimension so emitted vectors match the
    # Atlas index numDimensions. Without it the embedder uses Voyage's server
    # default and silently desyncs from a re-provisioned 512/256-dim index.
    common: dict[str, Any] = {
        "voyage_api_key": s.voyage_api_key,
        "output_dimension": s.voyage_dimensions,
    }
    if s.voyage_base_url:
        common["base_url"] = s.voyage_base_url
    doc_embedder = VoyageAIEmbeddings(model=s.voyage_document_model, **common)
    query_embedder = VoyageAIEmbeddings(model=s.voyage_query_model, **common)
    return AsymmetricVoyageEmbeddings(doc_embedder, query_embedder)


@lru_cache(maxsize=1)
def get_reranker() -> VoyageAIRerank:
    """Return the configured Voyage AI reranker."""
    s = get_settings()
    kwargs: dict[str, Any] = {
        "model": s.voyage_rerank_model,
        "voyage_api_key": s.voyage_api_key,
    }
    if s.voyage_base_url:
        kwargs["base_url"] = s.voyage_base_url
    return VoyageAIRerank(**kwargs)
