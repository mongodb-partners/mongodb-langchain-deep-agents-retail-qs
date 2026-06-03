"""VFS abstraction against an in-memory stub backend."""
from __future__ import annotations

import pytest

from . import vfs_contract


@pytest.fixture
def vfs(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    import mongomock

    from deep_agent.vfs import VfsMetadataStore, VirtualFilesystem

    client = mongomock.MongoClient()
    # Enforce compound unique index the same way ensure_indexes() does
    coll = client["deep_agent_test"]["vfs_files"]
    coll.create_index([("thread_id", 1), ("path", 1)], unique=True)
    metadata = VfsMetadataStore(coll)
    store = vfs_contract.DictBlobStore()
    return VirtualFilesystem(backend=store, metadata=metadata, max_bytes=1024)


def test_TC_05_010_round_trip(vfs) -> None:  # type: ignore[no-untyped-def]
    vfs_contract.assert_round_trip(vfs)


def test_TC_05_020_metadata_fields(vfs) -> None:  # type: ignore[no-untyped-def]
    vfs_contract.assert_metadata_fields(vfs)


def test_TC_05_040_upsert_cleans_old_blob(vfs) -> None:  # type: ignore[no-untyped-def]
    vfs_contract.assert_upsert_cleans_old_blob(vfs, blob_store=vfs.backend)


def test_TC_05_050_thread_scoping(vfs) -> None:  # type: ignore[no-untyped-def]
    vfs_contract.assert_thread_scoping(vfs)


def test_TC_05_060_size_limit(vfs) -> None:  # type: ignore[no-untyped-def]
    vfs_contract.assert_size_limit(vfs)


def test_TC_05_070_delete_missing(vfs) -> None:  # type: ignore[no-untyped-def]
    vfs_contract.assert_delete_missing(vfs)


def test_TC_05_080_read_missing_raises(vfs) -> None:  # type: ignore[no-untyped-def]
    vfs_contract.assert_read_missing_raises(vfs)


def test_TC_05_090_glob_scoped(vfs) -> None:  # type: ignore[no-untyped-def]
    vfs_contract.assert_glob_scoped(vfs)


def test_TC_05_030_duplicate_upsert_does_not_insert_twice(vfs) -> None:  # type: ignore[no-untyped-def]
    vfs.write_file("t1", "x.txt", b"one", content_type="text/plain")
    vfs.write_file("t1", "x.txt", b"two", content_type="text/plain")
    vfs.write_file("t1", "x.txt", b"three", content_type="text/plain")
    listing = vfs.list_files("t1")
    assert len(listing) == 1
    assert listing[0].path == "x.txt"
    assert vfs.read_file("t1", "x.txt") == b"three"


# --- write reorder + path validation -----------------


def test_TC_R501_094_put_before_upsert(vfs) -> None:  # type: ignore[no-untyped-def]
    """put → upsert → delete, never delete-before-upsert."""
    from unittest.mock import patch

    calls: list[str] = []
    real_put = vfs.backend.put
    real_upsert = vfs.metadata.upsert
    real_delete = vfs.backend.delete

    def _track_put(*a, **k):  # type: ignore[no-untyped-def]
        calls.append("put")
        return real_put(*a, **k)

    def _track_upsert(*a, **k):  # type: ignore[no-untyped-def]
        calls.append("upsert")
        return real_upsert(*a, **k)

    def _track_delete(*a, **k):  # type: ignore[no-untyped-def]
        calls.append("delete")
        return real_delete(*a, **k)

    with patch.object(vfs.backend, "put", side_effect=_track_put), patch.object(
        vfs.metadata, "upsert", side_effect=_track_upsert
    ), patch.object(vfs.backend, "delete", side_effect=_track_delete):
        vfs.write_file("t1", "ordered.txt", b"v1", content_type="text/plain")
        vfs.write_file("t1", "ordered.txt", b"v2", content_type="text/plain")

    assert calls == ["put", "upsert", "put", "upsert", "delete"]


def test_TC_R501_094_metadata_failure_cleans_new_blob(vfs) -> None:  # type: ignore[no-untyped-def]
    """If metadata upsert fails, the freshly-put blob is deleted; prior preserved."""
    from unittest.mock import patch

    import pytest as _pytest

    vfs.write_file("t1", "fail.txt", b"prior", content_type="text/plain")

    deleted_locators: list[str] = []
    real_delete = vfs.backend.delete

    def _track_delete(loc):  # type: ignore[no-untyped-def]
        deleted_locators.append(loc)
        return real_delete(loc)

    with patch.object(
        vfs.metadata, "upsert", side_effect=RuntimeError("metadata down")
    ), patch.object(
        vfs.backend, "delete", side_effect=_track_delete
    ), _pytest.raises(RuntimeError):
        vfs.write_file("t1", "fail.txt", b"new", content_type="text/plain")

    assert len(deleted_locators) == 1
    assert vfs.read_file("t1", "fail.txt") == b"prior"


def test_TC_R501_095_path_traversal_rejected(vfs) -> None:  # type: ignore[no-untyped-def]
    """Reject `..` segments anywhere in the path."""
    import pytest as _pytest

    from deep_agent.vfs.base import VfsError

    for bad in ["../etc/passwd", "/foo/../etc", "a/../b"]:
        with _pytest.raises(VfsError):
            vfs.write_file("t1", bad, b"x", content_type="text/plain")


def test_TC_R501_095_null_byte_rejected(vfs) -> None:  # type: ignore[no-untyped-def]
    """Reject null-byte injection."""
    import pytest as _pytest

    from deep_agent.vfs.base import VfsError

    with _pytest.raises(VfsError):
        vfs.write_file("t1", "good\x00path.txt", b"x", content_type="text/plain")


def test_TC_R501_095_empty_path_rejected(vfs) -> None:  # type: ignore[no-untyped-def]
    """Reject empty/whitespace paths."""
    import pytest as _pytest

    from deep_agent.vfs.base import VfsError

    for bad in ["", "  ", "\t"]:
        with _pytest.raises(VfsError):
            vfs.write_file("t1", bad, b"x", content_type="text/plain")


def test_TC_R501_095_path_length_cap(vfs) -> None:  # type: ignore[no-untyped-def]
    """Reject paths longer than 1024 bytes."""
    import pytest as _pytest

    from deep_agent.vfs.base import VfsError

    long_path = "a" * 1025
    with _pytest.raises(VfsError):
        vfs.write_file("t1", long_path, b"x", content_type="text/plain")
