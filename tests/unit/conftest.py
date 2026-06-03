"""Unit-test-only fixtures.

Autouse env fixture pins every setting this project reads so unit tests are
hermetic regardless of the developer's local ``.env``. We also point
pydantic-settings at a non-existent env file so developers who keep real
credentials in the repo's ``.env`` do not leak into test settings.

The ``DEEP_AGENT_ENV_FILE`` set below runs at conftest **module import**
time — *before* any test file imports ``deep_agent.config``. This is
load-bearing: pydantic-settings captures the ``env_file`` path at class
define time (when ``Settings`` is first imported), so an autouse
``monkeypatch.setenv`` runs too late to influence it. The module-level
``setdefault`` ensures the value is in place by the time the import
chain reaches ``deep_agent.config``. ``setdefault`` keeps the dev escape
hatch working: a developer who exports ``DEEP_AGENT_ENV_FILE=...`` to
point at a real config still wins.
"""
from __future__ import annotations

import os

os.environ.setdefault("DEEP_AGENT_ENV_FILE", "/dev/null")

import pytest


@pytest.fixture(autouse=True)
def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Disable .env loading for unit tests. Some developers keep a real `.env`
    # in the repo root; without this, `BaseSettings` would fall back to those
    # credentials whenever a test uses `monkeypatch.delenv` to simulate
    # "secret not set" and the real key is still in the file.
    monkeypatch.setenv("DEEP_AGENT_ENV_FILE", "/dev/null")
    monkeypatch.setenv("MONGODB_URI", "mongodb://fake:27017/?tls=true")
    monkeypatch.setenv("MONGODB_DB", "deep_agent_test")
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-test-voyage")
    monkeypatch.setenv("LLM_PROVIDER", "bedrock")
    monkeypatch.setenv("LLM_MODEL", "global.anthropic.claude-sonnet-4-6")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("VOYAGE_DOCUMENT_MODEL", "voyage-4")
    monkeypatch.setenv("VOYAGE_QUERY_MODEL", "voyage-4-lite")
    monkeypatch.setenv("VOYAGE_DIMENSIONS", "1024")
    monkeypatch.setenv("VFS_BACKEND", "s3")
    monkeypatch.setenv("VFS_S3_BUCKET", "test-bucket")
    monkeypatch.setenv("ALLOW_INSECURE", "false")
    monkeypatch.setenv("ENABLE_AGENT_LOG_SEARCH", "false")
    monkeypatch.delenv("ENABLE_MIRROR_SEARCH", raising=False)
    monkeypatch.setenv("ENABLE_RESPONSE_CACHE", "false")
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("DATA_AGENT_MONGODB_URI", raising=False)
    monkeypatch.delenv("VOYAGE_BASE_URL", raising=False)
    monkeypatch.delenv("SEEDS_DIR", raising=False)
    # Env vars from the deleted domain-pack era; ensure no leak.
    monkeypatch.delenv("DOMAIN", raising=False)
    monkeypatch.delenv("DOMAINS_ROOT", raising=False)


@pytest.fixture(autouse=True)
def _clear_caches(_set_required_env: None) -> None:
    """Clear process-level caches between tests so env-pinned settings and
    singletons don't leak across tests."""
    from deep_agent import config as _cfg

    def _clear_all() -> None:
        _cfg.get_settings.cache_clear()
        # Best-effort reset of the lru_cached singletons that key off settings.
        import contextlib as _cl

        for mod, attr in (
            ("deep_agent.persistence.response_cache", "build_response_cache"),
        ):
            with _cl.suppress(Exception):
                import importlib

                getattr(importlib.import_module(mod), attr).cache_clear()

    _clear_all()
    yield
    _clear_all()
