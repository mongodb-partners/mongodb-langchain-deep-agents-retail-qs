"""Shared VFS contract — exercised against a dict-backed stub and reused
for the S3 backend (via moto)."""
from __future__ import annotations

from typing import Any


class DictBlobStore:
    """In-memory stub implementing the `BlobStore` protocol."""

    backend = "gridfs"  # contract: either "gridfs" or "s3"; stub masquerades as gridfs

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}
        self._counter = 0

    def put(self, thread_id: str, path: str, data: bytes, *, content_type: str) -> str:
        self._counter += 1
        locator = f"loc-{self._counter}"
        self._store[locator] = data
        return locator

    def get(self, locator: str) -> bytes:
        return self._store[locator]

    def delete(self, locator: str) -> None:
        self._store.pop(locator, None)

    def exists(self, locator: str) -> bool:
        return locator in self._store


def assert_round_trip(vfs: Any) -> None:
    meta = vfs.write_file("t1", "notes/a.txt", b"hello", content_type="text/plain")
    assert meta.size == 5
    assert meta.path == "notes/a.txt"
    assert meta.thread_id == "t1"
    assert meta.content_type == "text/plain"
    assert meta.backend in ("gridfs", "s3")
    assert vfs.read_file("t1", "notes/a.txt") == b"hello"


def assert_metadata_fields(vfs: Any) -> None:
    meta = vfs.write_file("t1", "f.bin", b"\x00\x01\x02", content_type="application/octet-stream")
    assert meta.size == 3
    assert meta.created_at is not None
    assert meta.updated_at is not None
    assert meta.locator


def assert_upsert_cleans_old_blob(vfs: Any, *, blob_store: Any) -> None:
    first = vfs.write_file("t1", "x", b"one", content_type="text/plain")
    old_locator = first.locator
    vfs.write_file("t1", "x", b"two", content_type="text/plain")
    # The new blob is readable.
    assert vfs.read_file("t1", "x") == b"two"
    # Metadata holds exactly one row for this (thread, path).
    matching = [m for m in vfs.list_files("t1") if m.path == "x"]
    assert len(matching) == 1
    # If backends assign different locators per write (GridFS), the old blob
    # must have been reclaimed. Backends with deterministic locators (S3 keys
    # derived from path) overwrite in place — both are acceptable.
    new_locator = matching[0].locator
    if hasattr(blob_store, "exists") and new_locator != old_locator:
        assert not blob_store.exists(old_locator)


def assert_thread_scoping(vfs: Any) -> None:
    vfs.write_file("t1", "shared.txt", b"t1-secret", content_type="text/plain")
    vfs.write_file("t2", "shared.txt", b"t2-secret", content_type="text/plain")
    assert vfs.read_file("t1", "shared.txt") == b"t1-secret"
    assert vfs.read_file("t2", "shared.txt") == b"t2-secret"

    t1_names = {m.path for m in vfs.list_files("t1")}
    t2_names = {m.path for m in vfs.list_files("t2")}
    assert "shared.txt" in t1_names
    assert "shared.txt" in t2_names
    # they must not see each other's metadata by path
    assert all(m.thread_id == "t1" for m in vfs.list_files("t1"))
    assert all(m.thread_id == "t2" for m in vfs.list_files("t2"))


def assert_size_limit(vfs: Any) -> None:
    from deep_agent.vfs import VfsQuotaExceededError

    big = b"\x00" * (vfs.max_bytes + 1)
    try:
        vfs.write_file("t1", "big.bin", big, content_type="application/octet-stream")
    except VfsQuotaExceededError:
        pass
    else:
        raise AssertionError("oversize write was not refused")


def assert_delete_missing(vfs: Any) -> None:
    assert vfs.delete_file("t1", "nope.txt") is False


def assert_read_missing_raises(vfs: Any) -> None:
    from deep_agent.vfs import VfsFileNotFoundError

    try:
        vfs.read_file("t1", "does-not-exist")
    except VfsFileNotFoundError:
        pass
    else:
        raise AssertionError("missing read should raise VfsFileNotFoundError")


def assert_glob_scoped(vfs: Any) -> None:
    vfs.write_file("t1", "reports/a.md", b"a", content_type="text/markdown")
    vfs.write_file("t1", "reports/b.md", b"b", content_type="text/markdown")
    vfs.write_file("t1", "other.txt", b"x", content_type="text/plain")
    vfs.write_file("t2", "reports/c.md", b"c", content_type="text/markdown")

    hits_t1 = sorted(m.path for m in vfs.glob_files("t1", "reports/*.md"))
    assert hits_t1 == ["reports/a.md", "reports/b.md"]
    hits_t2 = sorted(m.path for m in vfs.glob_files("t2", "reports/*.md"))
    assert hits_t2 == ["reports/c.md"]
