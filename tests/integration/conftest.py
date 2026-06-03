"""Integration test fixtures — gated on ``ATLAS_URI``.

Each test that opts in receives a real ``pymongo.MongoClient`` against the
configured Atlas cluster. Without ``ATLAS_URI`` these fixtures call
``pytest.skip`` so ``uv run pytest -m integration`` is a no-op on dev
machines that don't have a cluster.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest


def _skip_if_no_atlas() -> str:
    uri = os.environ.get("ATLAS_URI")
    if not uri:
        pytest.skip("ATLAS_URI not set; skipping integration test")
    return uri


@pytest.fixture(scope="session")
def atlas_uri() -> str:
    return _skip_if_no_atlas()


@pytest.fixture
def atlas_client(atlas_uri: str) -> Iterator[Any]:
    from pymongo import MongoClient

    client: MongoClient[Any] = MongoClient(atlas_uri)
    yield client
    client.close()


@pytest.fixture
def s3_bucket() -> str:
    """Name of a pre-existing S3 bucket for the S3 VFS integration test.

    Gated on ``VFS_S3_BUCKET`` so teams without AWS can still run the rest
    of the integration tier.
    """
    bucket = os.environ.get("VFS_S3_BUCKET")
    if not bucket:
        pytest.skip("VFS_S3_BUCKET not set; skipping S3 VFS integration test")
    return bucket


@pytest.fixture(scope="session")
def asp_uri() -> str:
    """Atlas Stream Processing connection URI; skips when unset."""
    uri = os.environ.get("ASP_URI")
    if not uri:
        pytest.skip("ASP_URI not set; skipping Atlas Stream Processing E2E")
    return uri


@pytest.fixture(scope="session")
def kafka_bootstrap() -> str:
    return os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
