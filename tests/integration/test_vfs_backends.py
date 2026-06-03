"""VFS backend integration tests.

GridFS has been removed. Runs the shared contract suite against real
S3 (when ``VFS_S3_BUCKET`` is exported).
"""
from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

from tests.unit import vfs_contract

pytestmark = pytest.mark.integration


# ----- S3 against a real bucket -----


def test_TC_INT_050_s3_contract_live(atlas_client: Any, s3_bucket: str) -> None:
    """Runs the contract against a real S3 bucket + real Atlas metadata.

    Each test invocation uses a unique prefix so cleanup is scoped to this run.
    """
    import boto3

    from deep_agent.vfs import VfsMetadataStore, VirtualFilesystem
    from deep_agent.vfs.s3_backend import S3Backend

    client = boto3.client("s3", region_name=os.environ.get("VFS_S3_REGION"))
    db_name = f"deep_agent_int_{uuid.uuid4().hex[:8]}"
    prefix = f"deep-agent-int/{uuid.uuid4().hex}"
    db = atlas_client[db_name]
    try:
        coll = db["vfs_files"]
        coll.create_index([("thread_id", 1), ("path", 1)], unique=True)
        metadata = VfsMetadataStore(coll)
        backend = S3Backend(bucket=s3_bucket, prefix=prefix, client=client)
        vfs = VirtualFilesystem(backend=backend, metadata=metadata, max_bytes=65536)

        vfs_contract.assert_round_trip(vfs)
        vfs_contract.assert_metadata_fields(vfs)
        vfs_contract.assert_thread_scoping(vfs)
        vfs_contract.assert_size_limit(vfs)
        vfs_contract.assert_delete_missing(vfs)
        vfs_contract.assert_read_missing_raises(vfs)
        vfs_contract.assert_glob_scoped(vfs)
    finally:
        atlas_client.drop_database(db_name)
        # Best-effort S3 prefix cleanup
        try:
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=s3_bucket, Prefix=prefix):
                objs = page.get("Contents", [])
                if not objs:
                    continue
                client.delete_objects(
                    Bucket=s3_bucket,
                    Delete={"Objects": [{"Key": o["Key"]} for o in objs]},
                )
        except Exception:
            pass
