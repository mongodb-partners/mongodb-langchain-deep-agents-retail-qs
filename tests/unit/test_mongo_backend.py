"""Sub-phase 16: MongoDB-backed deepagents BackendProtocol adapter.

The adapter routes deepagents' built-in filesystem tools (`read_file`,
`write_file`, `edit_file`, `ls`, `glob`, `grep`) through our
:class:`VirtualFilesystem`, so artifacts land in MongoDB (GridFS or S3) instead
of LangGraph state. Thread scoping is honoured via a factory that reads
``thread_id`` from the tool runtime's config.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import mongomock
import pytest

from tests.unit.vfs_contract import DictBlobStore


def _make_vfs(max_bytes: int = 65536) -> Any:
    from deep_agent.vfs import VfsMetadataStore, VirtualFilesystem

    db = mongomock.MongoClient()["deep_agent_test"]
    coll = db["vfs_files"]
    coll.create_index([("thread_id", 1), ("path", 1)], unique=True)
    return VirtualFilesystem(
        backend=DictBlobStore(),
        metadata=VfsMetadataStore(coll),
        max_bytes=max_bytes,
    )


def _make_backend(vfs: Any, thread_id: str = "t1") -> Any:
    from deep_agent.backends.mongo_backend import MongoVfsBackend

    return MongoVfsBackend(vfs=vfs, thread_id=thread_id)


# ----- basic protocol coverage -----------------------------------------------


def test_TC_16_010_write_then_read_round_trip() -> None:
    vfs = _make_vfs()
    backend = _make_backend(vfs)

    res = backend.write("/notes/hello.txt", "Hello, research.")
    assert res.error is None
    assert res.path == "/notes/hello.txt"

    read = backend.read("/notes/hello.txt")
    assert read.error is None
    assert read.file_data is not None
    assert read.file_data["content"] == "Hello, research."
    assert read.file_data["encoding"] == "utf-8"


def test_TC_16_011_read_missing_returns_error() -> None:
    backend = _make_backend(_make_vfs())
    read = backend.read("/nowhere.txt")
    assert read.error and "not found" in read.error.lower()
    assert read.file_data is None


def test_TC_16_012_write_rejects_existing_path() -> None:
    """deepagents' contract: `write` errors on duplicate; agent uses `edit`."""
    vfs = _make_vfs()
    backend = _make_backend(vfs)
    assert backend.write("/f.txt", "first").error is None
    second = backend.write("/f.txt", "second")
    assert second.error and "already exists" in second.error.lower()


def test_TC_16_013_edit_replaces_string() -> None:
    vfs = _make_vfs()
    backend = _make_backend(vfs)
    backend.write("/p.md", "alpha beta gamma")

    edit = backend.edit("/p.md", old_string="beta", new_string="DELTA")
    assert edit.error is None
    assert edit.occurrences == 1

    read = backend.read("/p.md")
    assert read.file_data and read.file_data["content"] == "alpha DELTA gamma"


def test_TC_16_014_edit_replace_all() -> None:
    vfs = _make_vfs()
    backend = _make_backend(vfs)
    backend.write("/p.md", "a a a")
    edit = backend.edit("/p.md", old_string="a", new_string="X", replace_all=True)
    assert edit.occurrences == 3
    assert backend.read("/p.md").file_data["content"] == "X X X"


def test_TC_16_015_edit_missing_file() -> None:
    backend = _make_backend(_make_vfs())
    edit = backend.edit("/missing", old_string="x", new_string="y")
    assert edit.error and "not found" in edit.error.lower()


def test_TC_16_016_edit_no_match() -> None:
    vfs = _make_vfs()
    backend = _make_backend(vfs)
    backend.write("/p.md", "hello")
    edit = backend.edit("/p.md", old_string="nope", new_string="x")
    assert edit.error  # non-empty error
    assert edit.occurrences is None


def test_TC_16_017_write_sets_content_type_from_extension() -> None:
    """Regression: `.md` was being persisted as ``text/plain``, so the S3
    console served downloads as ``.txt``. The backend must derive
    ``content_type`` from the path suffix.
    """
    vfs = _make_vfs()
    backend = _make_backend(vfs)

    backend.write("/notes/report.md", "# heading")
    backend.write("/notes/data.json", "{}")
    backend.write("/notes/raw.txt", "plain")
    backend.write("/notes/blob.unknown", "x")

    md = vfs.metadata.get("t1", "/notes/report.md")
    js = vfs.metadata.get("t1", "/notes/data.json")
    txt = vfs.metadata.get("t1", "/notes/raw.txt")
    unk = vfs.metadata.get("t1", "/notes/blob.unknown")
    assert md is not None and md.content_type == "text/markdown"
    assert js is not None and js.content_type == "application/json"
    assert txt is not None and txt.content_type == "text/plain"
    assert unk is not None and unk.content_type == "text/plain"


def test_TC_16_018_edit_preserves_content_type_from_extension() -> None:
    """Edit goes through `write_file` again; content type must still be
    derived from the path so a re-saved `.md` doesn't regress to ``text/plain``.
    """
    vfs = _make_vfs()
    backend = _make_backend(vfs)
    backend.write("/notes/report.md", "alpha")
    backend.edit("/notes/report.md", old_string="alpha", new_string="beta")

    md = vfs.metadata.get("t1", "/notes/report.md")
    assert md is not None and md.content_type == "text/markdown"


# ----- listing / globbing / grep --------------------------------------------


def test_TC_16_020_ls_root_lists_files_and_subdirs() -> None:
    vfs = _make_vfs()
    backend = _make_backend(vfs)
    backend.write("/a.md", "1")
    backend.write("/b.md", "2")
    backend.write("/reports/r1.md", "r1")
    backend.write("/reports/r2.md", "r2")

    res = backend.ls("/")
    assert res.error is None
    paths = [e["path"] for e in (res.entries or [])]
    # Files at root
    assert "/a.md" in paths
    assert "/b.md" in paths
    # Single directory entry for reports/
    assert "/reports/" in paths
    report_entry = next(e for e in res.entries if e["path"] == "/reports/")
    assert report_entry.get("is_dir") is True


def test_TC_16_021_ls_subdir_lists_only_its_files() -> None:
    vfs = _make_vfs()
    backend = _make_backend(vfs)
    backend.write("/reports/a.md", "a")
    backend.write("/reports/b.md", "b")
    backend.write("/other.md", "x")

    res = backend.ls("/reports")
    paths = sorted(e["path"] for e in (res.entries or []))
    assert paths == ["/reports/a.md", "/reports/b.md"]


def test_TC_16_030_glob_respects_pattern_and_scope() -> None:
    vfs = _make_vfs()
    backend = _make_backend(vfs)
    backend.write("/reports/a.md", "a")
    backend.write("/reports/b.txt", "b")
    backend.write("/summary.md", "s")

    res = backend.glob("*.md", path="/reports")
    paths = sorted(m["path"] for m in (res.matches or []))
    assert paths == ["/reports/a.md"]


def test_TC_16_040_grep_matches_lines_with_path_and_line() -> None:
    vfs = _make_vfs()
    backend = _make_backend(vfs)
    backend.write("/a.md", "foo\nbar baz\nbar qux")
    backend.write("/b.md", "nothing here")

    res = backend.grep(pattern="bar")
    assert res.error is None
    matches = res.matches or []
    # Two matches from /a.md (lines 2 and 3)
    a_hits = [m for m in matches if m["path"] == "/a.md"]
    assert sorted(m["line"] for m in a_hits) == [2, 3]
    assert not any(m["path"] == "/b.md" for m in matches)


# ----- thread scoping --------------------------------------------------------


def test_TC_16_050_thread_scoping_isolates_backends() -> None:
    vfs = _make_vfs()
    b1 = _make_backend(vfs, thread_id="t1")
    b2 = _make_backend(vfs, thread_id="t2")

    b1.write("/secret.md", "t1-only")
    b2.write("/secret.md", "t2-only")

    assert b1.read("/secret.md").file_data["content"] == "t1-only"
    assert b2.read("/secret.md").file_data["content"] == "t2-only"

    # ls on one thread must not see the other thread's files
    t1_paths = {e["path"] for e in (b1.ls("/").entries or [])}
    t2_paths = {e["path"] for e in (b2.ls("/").entries or [])}
    assert t1_paths == {"/secret.md"}
    assert t2_paths == {"/secret.md"}


# ----- lazy thread_id resolution ----------------------------------


def test_TC_20_071_construct_without_thread_id_does_not_raise() -> None:
    """MongoVfsBackend(vfs=...) is legal; thread_id is optional."""
    from deep_agent.backends.mongo_backend import MongoVfsBackend

    backend = MongoVfsBackend(vfs=_make_vfs())
    assert isinstance(backend, MongoVfsBackend)


def test_TC_20_072_resolve_thread_id_outside_graph_returns_anonymous() -> None:
    """Outside a LangGraph invocation, get_config() raises
    RuntimeError; the backend must fall through to 'anonymous', never propagate."""
    from deep_agent.backends.mongo_backend import MongoVfsBackend

    backend = MongoVfsBackend(vfs=_make_vfs())
    # No monkeypatching: we're outside any graph invocation, so
    # langgraph.config.get_config() will raise RuntimeError.
    assert backend._resolve_thread_id() == "anonymous"


def test_TC_20_073_resolve_thread_id_from_get_config() -> None:
    """When get_config() yields a configurable.thread_id, use it."""
    from deep_agent.backends.mongo_backend import MongoVfsBackend

    backend = MongoVfsBackend(vfs=_make_vfs())
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "deep_agent.backends.mongo_backend.get_config",
            lambda: {"configurable": {"thread_id": "t-from-config"}},
        )
        assert backend._resolve_thread_id() == "t-from-config"


def test_TC_20_074_explicit_thread_id_override_beats_get_config() -> None:
    """Constructor-level thread_id wins over get_config()."""
    from deep_agent.backends.mongo_backend import MongoVfsBackend

    backend = MongoVfsBackend(vfs=_make_vfs(), thread_id="override")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "deep_agent.backends.mongo_backend.get_config",
            lambda: {"configurable": {"thread_id": "t-from-config"}},
        )
        assert backend._resolve_thread_id() == "override"


def test_TC_20_075_methods_use_resolved_thread_id_from_get_config() -> None:
    """Protocol methods call _resolve_thread_id on every invocation,
    picking up the current LangGraph runtime's thread_id."""
    from deep_agent.backends.mongo_backend import MongoVfsBackend

    vfs = _make_vfs()
    backend = MongoVfsBackend(vfs=vfs)  # no override
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "deep_agent.backends.mongo_backend.get_config",
            lambda: {"configurable": {"thread_id": "t-live"}},
        )
        res = backend.write("/lazy.md", "content")
        assert res.error is None
        # Read back with the same lazy resolution
        read = backend.read("/lazy.md")
        assert read.file_data is not None
        assert read.file_data["content"] == "content"
    # Files must have landed under thread_id='t-live' in the VFS
    assert vfs.read_file("t-live", "/lazy.md") == b"content"


def test_TC_20_076_factory_returns_instance_shim() -> None:
    """mongo_backend_factory remains exported as a thin
    shim that returns a MongoVfsBackend instance. It no longer needs to read
    `runtime`; thread_id is resolved lazily."""
    from deep_agent.backends.mongo_backend import MongoVfsBackend, mongo_backend_factory

    with pytest.MonkeyPatch.context() as mp:
        vfs = _make_vfs()
        mp.setattr("deep_agent.backends.mongo_backend.get_vfs", lambda: vfs)
        backend = mongo_backend_factory(MagicMock())

    assert isinstance(backend, MongoVfsBackend)
