"""Virtual filesystem: S3-only blob backend + MongoDB-backed metadata.

GridFS support was removed. Use S3 (or any S3-compatible service such as
MinIO for local dev) for blob storage; metadata stays in MongoDB.
"""
from __future__ import annotations

from functools import lru_cache

import boto3
import botocore.config

from ..config import get_settings
from ..persistence.mongo import get_db
from .base import (
    BlobStore,
    FileMetadata,
    VfsError,
    VfsFileNotFoundError,
    VfsQuotaExceededError,
)
from .filesystem import VirtualFilesystem
from .metadata import VfsMetadataStore
from .s3_backend import S3Backend

__all__ = [
    "BlobStore",
    "FileMetadata",
    "S3Backend",
    "VfsError",
    "VfsFileNotFoundError",
    "VfsMetadataStore",
    "VfsQuotaExceededError",
    "VirtualFilesystem",
    "get_vfs",
]


@lru_cache(maxsize=1)
def get_vfs() -> VirtualFilesystem:
    """Return the configured :class:`VirtualFilesystem` singleton (S3 only).

    Binds to ``Settings.mongodb_db``.
    The S3 client is built once with explicit timeouts + retry config
    so concurrent VFS calls don't race a fresh client per call.
    """
    s = get_settings()
    if not s.vfs_s3_bucket:
        raise ValueError(
            "VFS_S3_BUCKET is required (the GridFS backend was removed)"
        )
    db = get_db()
    client = boto3.client(
        "s3",
        region_name=s.vfs_s3_region,
        config=botocore.config.Config(
            connect_timeout=5,
            read_timeout=30,
            retries={"max_attempts": 5, "mode": "standard"},
        ),
    )
    backend: BlobStore = S3Backend(
        bucket=s.vfs_s3_bucket, prefix=s.vfs_s3_prefix, client=client
    )
    metadata = VfsMetadataStore(db[s.vfs_files_collection])
    return VirtualFilesystem(backend=backend, metadata=metadata, max_bytes=s.vfs_max_bytes)
