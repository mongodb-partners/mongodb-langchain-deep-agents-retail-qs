"""VFS types shared by every backend and tool."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable


class VfsError(Exception):
    """Base class for VFS-layer errors surfaced to tools."""


class VfsFileNotFoundError(VfsError):
    """Raised when a path is missing for the requesting thread."""


class VfsQuotaExceededError(VfsError):
    """Raised when a write exceeds the configured per-file size limit."""


@dataclass(frozen=True)
class FileMetadata:
    thread_id: str
    path: str
    size: int
    content_type: str
    backend: Literal["s3"]
    locator: str
    created_at: datetime
    updated_at: datetime


@runtime_checkable
class BlobStore(Protocol):
    """Byte-blob backend. ``locator`` is backend-specific (S3 key today;
    other tags reserved for future drivers)."""

    @property
    def backend(self) -> str: ...

    def put(
        self, thread_id: str, path: str, data: bytes, *, content_type: str
    ) -> str: ...

    def get(self, locator: str) -> bytes: ...

    def delete(self, locator: str) -> None: ...
