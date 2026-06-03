"""S3 backend under moto.

moto stands in for S3 so the full contract runs without a live AWS account.
"""
from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    from deep_agent import config

    config.get_settings.cache_clear()


@pytest.fixture
def s3_client(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    from moto import mock_aws

    with mock_aws():
        import boto3

        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-bucket")
        yield client


def _make_backend(s3_client: Any):  # type: ignore[no-untyped-def]
    from deep_agent.vfs.s3_backend import S3Backend

    return S3Backend(bucket="test-bucket", prefix="deep-agent", client=s3_client)


def test_TC_07_010_put_get_round_trip(s3_client) -> None:  # type: ignore[no-untyped-def]
    backend = _make_backend(s3_client)
    locator = backend.put("t1", "notes/a.txt", b"hello", content_type="text/plain")
    assert locator == "deep-agent/t1/notes/a.txt"
    assert backend.get(locator) == b"hello"


def test_TC_07_020_thread_encoded_in_key(s3_client) -> None:  # type: ignore[no-untyped-def]
    backend = _make_backend(s3_client)
    k1 = backend.put("t1", "shared", b"a", content_type="text/plain")
    k2 = backend.put("t2", "shared", b"b", content_type="text/plain")
    assert k1 != k2
    assert "/t1/" in k1 and "/t2/" in k2


def test_TC_07_030_object_metadata_carries_thread_id(s3_client) -> None:  # type: ignore[no-untyped-def]
    backend = _make_backend(s3_client)
    locator = backend.put("t1", "m.bin", b"x", content_type="application/octet-stream")
    head = s3_client.head_object(Bucket="test-bucket", Key=locator)
    assert head["Metadata"]["thread_id"] == "t1"


def test_TC_07_040_full_contract(s3_client) -> None:  # type: ignore[no-untyped-def]
    import mongomock

    from deep_agent.vfs import VfsMetadataStore, VirtualFilesystem
    from deep_agent.vfs.s3_backend import S3Backend

    from . import vfs_contract

    db = mongomock.MongoClient()["deep_agent_test"]
    coll = db["vfs_files"]
    coll.create_index([("thread_id", 1), ("path", 1)], unique=True)
    metadata = VfsMetadataStore(coll)
    backend = S3Backend(bucket="test-bucket", prefix="deep-agent", client=s3_client)
    vfs = VirtualFilesystem(backend=backend, metadata=metadata, max_bytes=1024)

    vfs_contract.assert_round_trip(vfs)
    vfs_contract.assert_metadata_fields(vfs)
    vfs_contract.assert_upsert_cleans_old_blob(
        vfs,
        blob_store=_S3ExistsAdapter(backend, s3_client, "test-bucket"),
    )
    vfs_contract.assert_thread_scoping(vfs)
    vfs_contract.assert_size_limit(vfs)
    vfs_contract.assert_delete_missing(vfs)
    vfs_contract.assert_read_missing_raises(vfs)
    vfs_contract.assert_glob_scoped(vfs)


class _S3ExistsAdapter:
    def __init__(self, backend: Any, client: Any, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    def exists(self, locator: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self._bucket, Key=locator)
        except ClientError:
            return False
        return True


def test_TC_07_050_get_vfs_routes_to_s3_when_backend_s3(
    monkeypatch: pytest.MonkeyPatch, s3_client
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("VFS_BACKEND", "s3")
    monkeypatch.setenv("VFS_S3_BUCKET", "test-bucket")
    monkeypatch.setenv("VFS_S3_PREFIX", "deep-agent")

    import mongomock

    from deep_agent import vfs as vfs_pkg
    from deep_agent.vfs.s3_backend import S3Backend

    vfs_pkg.get_vfs.cache_clear()
    db = mongomock.MongoClient()["deep_agent_test"]
    from unittest.mock import patch

    with patch("deep_agent.vfs.get_db", return_value=db), patch(
        "deep_agent.vfs.boto3.client", return_value=s3_client
    ):
        instance = vfs_pkg.get_vfs()

    assert isinstance(instance.backend, S3Backend)
    vfs_pkg.get_vfs.cache_clear()


def test_TC_07_060_delete_missing_key_is_noop(s3_client) -> None:  # type: ignore[no-untyped-def]
    backend = _make_backend(s3_client)
    # Must not raise
    backend.delete("deep-agent/t1/never-existed")
