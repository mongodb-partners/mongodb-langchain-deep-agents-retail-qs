"""Application settings loaded from environment variables and .env.

Single source of truth for secrets + tunables. Redacted in ``repr`` so
accidental logging does not leak credentials.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_seeds_dir() -> Path:
    # <repo_root>/examples/retail_assistant/seeds — the Agent Cartsmith retail demo
    # seed source (grocery products / customers / orders + retail KB + KG).
    return Path(__file__).resolve().parents[2] / "examples" / "retail_assistant" / "seeds"


def _resolve_env_file() -> str:
    """Resolve the env-file path at instantiation time.

    ``DEEP_AGENT_ENV_FILE`` lets tests (and tightly-controlled
    environments) opt out of implicit ``.env`` loading. We resolve this
    inside ``Settings.__init__`` rather than at module-import time so
    ``monkeypatch.setenv("DEEP_AGENT_ENV_FILE", ...)`` actually takes
    effect — pydantic-settings would otherwise capture the path once at
    class-define time, and an autouse fixture firing after that capture
    has no influence.
    """
    return os.environ.get("DEEP_AGENT_ENV_FILE", ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # ``env_file`` is set per-instantiation in ``__init__`` (see
        # _resolve_env_file). Leaving it unset here means the
        # SettingsConfigDict default (None) applies until __init__
        # injects the resolved path.
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    def __init__(self, **kwargs: Any) -> None:
        # Resolve the env file fresh on every Settings construction so
        # tests can monkeypatch ``DEEP_AGENT_ENV_FILE`` between calls.
        # ``_env_file`` is the pydantic-settings v2 keyword for the
        # init-time override.
        kwargs.setdefault("_env_file", _resolve_env_file())
        super().__init__(**kwargs)

    # --- MongoDB ---
    mongodb_uri: SecretStr = Field(..., alias="MONGODB_URI")
    # MONGODB_DB is required (no default).
    mongodb_db: str = Field(..., alias="MONGODB_DB")
    data_agent_mongodb_uri: SecretStr | None = Field(None, alias="DATA_AGENT_MONGODB_URI")
    # Production-sized pool defaults; env-overridable.
    mongodb_max_pool_size: int = Field(100, alias="MONGODB_MAX_POOL_SIZE")
    mongodb_min_pool_size: int = Field(10, alias="MONGODB_MIN_POOL_SIZE")
    mongodb_server_selection_timeout_ms: int = Field(
        5000, alias="MONGODB_SERVER_SELECTION_TIMEOUT_MS"
    )
    mongodb_socket_timeout_ms: int = Field(30000, alias="MONGODB_SOCKET_TIMEOUT_MS")

    # --- LLM ---
    llm_provider: str = Field("bedrock", alias="LLM_PROVIDER")
    # Default model is Claude Haiku 4.5 — fastest Anthropic model
    # in the registry, passes both single-tool and parallel-tool harnesses,
    # ~4s/turn vs Opus 4.7's ~16s. The model dropdown lets users switch to
    # Opus / Sonnet / Nova / etc. per request.
    # Note: Sonnet 4.5 has a known interaction with deepagents' `task` tool
    # on Bedrock — kept in the dropdown for completeness but not chosen as
    # default.
    llm_model: str = Field(
        "global.anthropic.claude-haiku-4-5-20251001-v1:0",
        alias="LLM_MODEL",
    )
    aws_region: str = Field("us-east-1", alias="AWS_DEFAULT_REGION")

    # --- Embeddings (Voyage AI) ---
    # Always required: the long-term-memory store embeds via Voyage on every
    # boot, and KB seed / GraphRAG / the response cache use it too.
    # The validator below fails fast when it is missing.
    voyage_api_key: SecretStr | None = Field(None, alias="VOYAGE_API_KEY")
    voyage_document_model: str = Field("voyage-4", alias="VOYAGE_DOCUMENT_MODEL")
    voyage_query_model: str = Field("voyage-4-lite", alias="VOYAGE_QUERY_MODEL")
    voyage_dimensions: int = Field(1024, alias="VOYAGE_DIMENSIONS")
    voyage_rerank_model: str = Field("rerank-2.5", alias="VOYAGE_RERANK_MODEL")
    voyage_base_url: str | None = Field(None, alias="VOYAGE_BASE_URL")

    # --- Tavily (web search) ---
    tavily_api_key: SecretStr | None = Field(None, alias="TAVILY_API_KEY")

    # --- Virtual filesystem (S3-only) ---
    vfs_backend: Literal["s3"] = Field("s3", alias="VFS_BACKEND")
    vfs_max_bytes: int = Field(50 * 1024 * 1024, alias="VFS_MAX_BYTES")
    vfs_s3_bucket: str | None = Field(None, alias="VFS_S3_BUCKET")
    vfs_s3_prefix: str = Field("deep-agent", alias="VFS_S3_PREFIX")
    vfs_s3_region: str | None = Field(None, alias="VFS_S3_REGION")

    # --- LangSmith ---
    # The LangSmith SDK reads LANGSMITH_API_KEY / LANGSMITH_PROJECT directly
    # from the process environment, so we don't mirror them onto Settings.
    langsmith_tracing: bool = Field(False, alias="LANGSMITH_TRACING")

    # --- Tunables ---
    # Output token ceiling per LLM call. Lowered from 32000 (Claude
    # 4.x ceiling) to 4096 — the universal ceiling that ALL Bedrock agentic
    # models support (Llama 4 Maverick caps at 4096; Nova 5000; Anthropic
    # 64k+). Claude emits ``tool_use`` blocks as streaming JSON; when it hits
    # the cap mid-emission the JSON is truncated and pydantic rejects the
    # partial input (e.g. ``write_file`` arriving with no ``content`` field).
    # 4096 is enough for typical tool-call payloads; raise via env for
    # Claude-only deployments writing very long artifacts in one turn.
    max_tokens: int = Field(4096, alias="MAX_TOKENS")

    # --- Agent log + hybrid search ---
    # The agent-log persistence + retrieval is now provided by the
    # ``langchain-mongodb-agent-log`` package. The settings below mirror
    # the package's constructor knobs and are wired into it at graph-
    # build time. Each ``Field`` accepts both the new ``AGENT_LOG_*``
    # env name AND the legacy ``MIRROR_*`` name (one minor cycle of
    # backwards compat — the legacy aliases will be removed in v0.2).
    #
    # 15 MiB cap keeps a single log doc safely below the 16 MiB BSON ceiling.
    agent_log_max_content_bytes: int = Field(
        15 * 1024 * 1024,
        validation_alias=AliasChoices(
            "AGENT_LOG_MAX_CONTENT_BYTES", "MIRROR_TOOL_RESULT_MAX_BYTES"
        ),
    )
    agent_log_retention_days: int = Field(
        90,
        validation_alias=AliasChoices(
            "AGENT_LOG_RETENTION_DAYS", "MIRROR_RETENTION_DAYS"
        ),
    )
    enable_agent_log_search: bool = Field(
        True,
        validation_alias=AliasChoices(
            "ENABLE_AGENT_LOG_SEARCH", "ENABLE_MIRROR_SEARCH"
        ),
    )
    # Query-keyed semantic RESPONSE cache (turn-level). This is the
    # sole semantic cache (an earlier prompt-level cache was retired in favor
    # of this one — it embedded the whole prompt and collided across different
    # queries). It embeds ONLY the user query and stores the final answer,
    # scoped by (user_id, model). Default ON.
    enable_response_cache: bool = Field(True, alias="ENABLE_RESPONSE_CACHE")
    response_cache_collection: str = "semantic_response_cache"
    response_cache_vector_index: str = "response_cache_semantic_index"
    response_cache_threshold: float = Field(0.9, alias="RESPONSE_CACHE_THRESHOLD")
    response_cache_ttl_days: int = Field(7, alias="RESPONSE_CACHE_TTL_DAYS")

    agent_log_search_text_max_bytes: int = Field(
        8192,
        validation_alias=AliasChoices(
            "AGENT_LOG_SEARCH_TEXT_MAX_BYTES", "MIRROR_SEARCH_TEXT_MAX_BYTES"
        ),
    )
    agent_log_vector_index: str = Field(
        "agent_log_vector_idx",
        validation_alias=AliasChoices(
            "AGENT_LOG_VECTOR_INDEX", "MIRROR_VECTOR_INDEX"
        ),
    )
    agent_log_search_index: str = Field(
        "agent_log_search_idx",
        validation_alias=AliasChoices(
            "AGENT_LOG_SEARCH_INDEX", "MIRROR_SEARCH_INDEX"
        ),
    )
    # How many past-conversation hits search_past_conversations
    # returns. Fed to build_tool(top_k=...); the package retriever caps at 20.
    agent_log_search_top_k: int = Field(
        5,
        validation_alias=AliasChoices(
            "AGENT_LOG_SEARCH_TOP_K", "MIRROR_SEARCH_TOP_K"
        ),
    )

    # --- Fetch hardening ---
    fetch_max_bytes: int = Field(2 * 1024 * 1024, alias="FETCH_MAX_BYTES")

    # --- HITL plumbing (no defaults; verticals opt in) ---
    hitl_tools: str = Field("", alias="HITL_TOOLS")
    data_agent_allow_list: str = Field("", alias="DATA_AGENT_ALLOW_LIST")
    # The data-agent allow-list is fail-CLOSED — an empty
    # DATA_AGENT_ALLOW_LIST refuses every collection. Set this to opt INTO an
    # open mode (every non-underscore collection queryable). Dev/demo only;
    # never enable in a multi-tenant deployment.
    data_agent_allow_all: bool = Field(False, alias="DATA_AGENT_ALLOW_ALL")

    # --- Operational primitives ---
    # Index DDL must NOT run on every request-serving boot under
    # the runtime credential the RBAC playbook denies CREATE_INDEX. Default
    # False; operators run a one-shot bootstrap (deep-agent CLI / admin role).
    provision_indexes_on_boot: bool = Field(False, alias="PROVISION_INDEXES_ON_BOOT")
    readiness_cache_ttl_s: int = Field(5, alias="READINESS_CACHE_TTL_S")
    shutdown_grace_period_s: int = Field(30, alias="SHUTDOWN_GRACE_PERIOD_S")
    chat_turn_timeout_s: int = Field(180, alias="CHAT_TURN_TIMEOUT_S")
    recursion_limit: int = Field(50, alias="RECURSION_LIMIT")

    # --- Agent-skills directory ---
    # Default to the in-image path the Dockerfile installs to. For local
    # dev override to ``AgentSkills`` (relative paths resolve against
    # ``os.getcwd()`` at graph-build time).
    agent_skills_dir: str = Field("/app/AgentSkills", alias="AGENT_SKILLS_DIR")

    # --- Per-request model selector ---
    # Comma-separated list of Bedrock inference-profile IDs verified to
    # drive the deep-agents tool loop. Each entry is rendered in the UI
    # dropdown; the human label is derived by stripping the
    # ``us./global.`` prefix. Frontend passes the chosen model on each
    # /chat request; absent → ``llm_model`` default.
    available_models: str = Field(
        ",".join([
            "global.anthropic.claude-opus-4-7",
            "global.anthropic.claude-opus-4-6-v1",
            "us.anthropic.claude-opus-4-5-20251101-v1:0",
            "us.anthropic.claude-opus-4-1-20250805-v1:0",
            "global.anthropic.claude-sonnet-4-6",
            "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "global.anthropic.claude-haiku-4-5-20251001-v1:0",
            "us.anthropic.claude-3-5-haiku-20241022-v1:0",
            "us.amazon.nova-premier-v1:0",
            "us.amazon.nova-pro-v1:0",
            "us.amazon.nova-lite-v1:0",
            "us.meta.llama4-maverick-17b-instruct-v1:0",
            "us.mistral.pixtral-large-2502-v1:0",
        ]),
        alias="AVAILABLE_MODELS",
    )

    # --- Collection names ---
    checkpoints_collection: str = "checkpoints"
    checkpoint_writes_collection: str = "checkpoint_writes"
    long_term_memory_collection: str = "long_term_memory"
    knowledge_base_collection: str = "knowledge_base"
    knowledge_graph_collection: str = "knowledge_graph"
    stream_events_collection: str = "stream_events"
    feedback_collection: str = "feedback"
    vfs_files_collection: str = "vfs_files"
    # Retail commerce surfaces. ``carts`` is written ONLY by the
    # dedicated cart tools (never NL→MQL — deliberately kept out of
    # DATA_AGENT_ALLOW_LIST). ``promotions`` holds structured coupon terms the
    # savings_calculator reads (NL→MQL read-allow-listed).
    carts_collection: str = "carts"
    promotions_collection: str = "promotions"
    # Agent-log collection (provided by langchain-mongodb-agent-log).
    # Default name matches the package's documented default.
    agent_log_collection: str = Field(
        "agent_log",
        validation_alias=AliasChoices("AGENT_LOG_COLLECTION"),
    )

    # --- Index names ---
    knowledge_base_vector_index: str = "vector_index"
    knowledge_base_search_index: str = "search_index"
    long_term_memory_vector_index: str = "memory_semantic_index"

    # --- Dev escape hatch ---
    allow_insecure: bool = Field(False, alias="ALLOW_INSECURE")

    # --- Seed source (replaces domain-pack loader) ---
    seeds_dir: Path = Field(default_factory=_default_seeds_dir, alias="SEEDS_DIR")

    @field_validator("voyage_dimensions")
    @classmethod
    def _positive_dims(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("voyage_dimensions must be positive")
        return v

    @model_validator(mode="after")
    def _enforce_tls(self) -> Settings:
        uri = self.mongodb_uri.get_secret_value()
        is_srv = uri.startswith("mongodb+srv://")
        tls_flag = "tls=true" in uri.lower() or "ssl=true" in uri.lower()
        if not is_srv and not tls_flag and not self.allow_insecure:
            raise ValueError(
                "MONGODB_URI must enforce TLS (use mongodb+srv:// or include tls=true); "
                "set ALLOW_INSECURE=true to override for local dev."
            )
        return self

    @model_validator(mode="after")
    def _enforce_s3_config(self) -> Settings:
        if self.vfs_backend == "s3" and not self.vfs_s3_bucket:
            raise ValueError("VFS_BACKEND=s3 requires VFS_S3_BUCKET to be set")
        return self

    @model_validator(mode="after")
    def _enforce_voyage_key(self) -> Settings:
        """VOYAGE_API_KEY is required whenever the app runs.

        The long-term-memory store is built **unconditionally**
        (``build_store`` → ``get_embeddings`` for the vector-index config), so
        Voyage is always needed — not only when ``ENABLE_AGENT_LOG_SEARCH`` or
        the response cache are on. The old gate let the app start
        without the key and then fail with an opaque pydantic error on the first
        memory op. Fail fast at startup with a clear message instead.
        """
        key = self.voyage_api_key.get_secret_value() if self.voyage_api_key else ""
        if not key:
            raise ValueError(
                "VOYAGE_API_KEY is required. The long-term-memory store always "
                "embeds via Voyage; the agent-log hybrid search "
                "(ENABLE_AGENT_LOG_SEARCH) and the response cache "
                "(ENABLE_RESPONSE_CACHE) use the same embedder. Set VOYAGE_API_KEY."
            )
        return self

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"<Settings db={self.mongodb_db} provider={self.llm_provider} "
            f"vfs={self.vfs_backend} [redacted]>"
        )

    __str__ = __repr__


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
