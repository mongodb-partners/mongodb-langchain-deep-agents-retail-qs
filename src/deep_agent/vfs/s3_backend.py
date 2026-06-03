"""S3-backed :class:`BlobStore` implementation.

Object key layout: ``<prefix>/<thread_id>/<path>``. The ``path`` inside the
key preserves sub-directories; S3 treats them as a flat key space, but the
prefix structure keeps listing and IAM scoping sane.
"""
from __future__ import annotations

from typing import Any

from .base import VfsError


class S3Backend:
    backend: str = "s3"

    def __init__(self, *, bucket: str, prefix: str, client: Any) -> None:
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._client = client

    @staticmethod
    def _validate_thread_id(thread_id: str) -> None:
        """The thread_id becomes an S3 key segment; a '/'-bearing
        or traversal value would break the documented ``<prefix>/<thread_id>/``
        prefix/IAM isolation. Reject anything that isn't a single safe segment."""
        if (
            not thread_id
            or "/" in thread_id
            or ".." in thread_id
            or "\x00" in thread_id
            or thread_id != thread_id.strip()
        ):
            raise VfsError(f"unsafe thread_id for S3 key segment: {thread_id!r}")

    def _key(self, thread_id: str, path: str) -> str:
        self._validate_thread_id(thread_id)
        return f"{self._prefix}/{thread_id}/{path.lstrip('/')}"

    def put(
        self,
        thread_id: str,
        path: str,
        data: bytes,
        *,
        content_type: str,
    ) -> str:
        key = self._key(thread_id, path)
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            Metadata={"thread_id": thread_id},
        )
        return key

    def get(self, locator: str) -> bytes:
        resp = self._client.get_object(Bucket=self._bucket, Key=locator)
        data: bytes = resp["Body"].read()
        return data

    def delete(self, locator: str) -> None:
        # S3 delete_object is already idempotent, but be explicit about swallowing
        # the NoSuchKey class of failures so callers can always call delete safely.
        try:
            self._client.delete_object(Bucket=self._bucket, Key=locator)
        except self._client.exceptions.NoSuchKey:  # pragma: no cover - boto3 contract
            return
