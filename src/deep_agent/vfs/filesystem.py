"""VirtualFilesystem — backend-agnostic coordinator for blob + metadata."""
from __future__ import annotations

import contextlib

from .base import (
    BlobStore,
    FileMetadata,
    VfsError,
    VfsFileNotFoundError,
    VfsQuotaExceededError,
)
from .metadata import VfsMetadataStore

_PATH_LENGTH_CAP = 1024


def _validate_path(path: str) -> None:
    """Reject traversal, null bytes, oversize.

    Failure mode is :class:`VfsError` so the LLM-facing tools see a
    clear refusal in the tool result rather than a backend-specific
    exception. The leading-slash convention is not enforced because
    deepagents' built-in tools use both rooted (``/foo``) and bare
    (``foo``) shapes; what matters for safety is that no segment
    equals ``..``.
    """
    if not isinstance(path, str) or not path.strip():
        raise VfsError("path must be a non-empty string")
    if "\x00" in path:
        raise VfsError("path must not contain null bytes")
    if len(path) > _PATH_LENGTH_CAP:
        raise VfsError(f"path length exceeds {_PATH_LENGTH_CAP} bytes")
    parts = path.split("/")
    if any(p == ".." for p in parts):
        raise VfsError(f"path must not contain '..' segments; got {path!r}")


class VirtualFilesystem:
    def __init__(
        self,
        *,
        backend: BlobStore,
        metadata: VfsMetadataStore,
        max_bytes: int,
    ) -> None:
        self.backend = backend
        self.metadata = metadata
        self.max_bytes = max_bytes

    def write_file(
        self,
        thread_id: str,
        path: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> FileMetadata:
        """Write a file with put → upsert → delete ordering.

        A failure between (a) put and (b)
        upsert leaves an orphan blob (cleanable by housekeeping)
        rather than a dangling pointer (would make subsequent reads
        return a 404 from the backend even though the metadata says
        the file exists). The prior blob is deleted only AFTER the
        metadata upsert succeeds.

        Caveat: ``S3Backend.put`` currently returns a stable
        path-derived locator (it overwrites in place rather than
        allocating a new key per write), so ``prior.locator ==
        locator`` and the step-3 reclaim is a no-op. The ordering
        still matters for any future content-addressed backend where
        each write produces a fresh key — keep the put → upsert →
        delete shape so swapping backends doesn't introduce a
        dangling-pointer regression.
        """
        _validate_path(path)
        if len(data) > self.max_bytes:
            raise VfsQuotaExceededError(
                f"file {path!r} is {len(data)} bytes; max {self.max_bytes} bytes"
            )

        prior = self.metadata.get(thread_id, path)

        # 1. Put the new blob first.
        locator = self.backend.put(thread_id, path, data, content_type=content_type)

        # 2. Upsert the metadata pointer. If this fails, clean up the new
        #    blob so we don't leak it; the prior pointer is still valid.
        try:
            meta = self.metadata.upsert(
                thread_id=thread_id,
                path=path,
                size=len(data),
                content_type=content_type,
                backend=self.backend.backend,
                locator=locator,
            )
        except Exception:
            with contextlib.suppress(Exception):
                self.backend.delete(locator)
            raise

        # 3. The metadata upsert succeeded; reclaim the prior blob.
        if prior is not None and prior.locator != locator:
            with contextlib.suppress(Exception):
                self.backend.delete(prior.locator)

        return meta

    def read_file(self, thread_id: str, path: str) -> bytes:
        meta = self.metadata.get(thread_id, path)
        if meta is None:
            raise VfsFileNotFoundError(f"no such file {path!r} for thread {thread_id!r}")
        return self.backend.get(meta.locator)

    def delete_file(self, thread_id: str, path: str) -> bool:
        meta = self.metadata.delete(thread_id, path)
        if meta is None:
            return False
        # Metadata is the source of truth; leave stale blobs to housekeeping.
        with contextlib.suppress(Exception):
            self.backend.delete(meta.locator)
        return True

    def list_files(self, thread_id: str) -> list[FileMetadata]:
        return self.metadata.list_all(thread_id)

    def glob_files(self, thread_id: str, pattern: str) -> list[FileMetadata]:
        return self.metadata.glob(thread_id, pattern)
