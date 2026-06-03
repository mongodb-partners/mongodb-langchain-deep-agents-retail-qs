"""MongoDB-backed deepagents :class:`BackendProtocol` adapter.

Routes the built-in ``write_file`` / ``read_file`` / ``edit_file`` / ``ls`` /
``glob`` / ``grep`` filesystem tools through our
:class:`~deep_agent.vfs.VirtualFilesystem`, so artifacts land in S3 (blobs)
+ MongoDB (metadata) rather than LangGraph state. Thread scoping is resolved
at each method call via :func:`langgraph.config.get_config` so that a single
``MongoVfsBackend`` instance can serve every turn with the correct
``thread_id`` — mirroring how deepagents' own ``StateBackend`` reads state
via ``get_config()``.

Paths are stored verbatim in ``vfs_files.path`` so a subsequent ``ls(...)``
returns the same shape the agent wrote.
"""
from __future__ import annotations

import fnmatch
import mimetypes
import re
from datetime import UTC, datetime
from typing import Any

from deepagents.backends.protocol import (
    BackendProtocol,
    EditResult,
    FileData,
    FileInfo,
    GlobResult,
    GrepMatch,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from langgraph.config import get_config

from ..vfs import VfsFileNotFoundError, VirtualFilesystem, get_vfs

# `mimetypes` ships with the OS-specific `mime.types`; coverage of common
# text formats varies (notably `.md` is missing on macOS). Register the
# extensions the agent writes most often so the S3 object's Content-Type
# matches the path suffix and the console doesn't serve `.md` as `.txt`.
for _ext, _ct in (
    (".md", "text/markdown"),
    (".markdown", "text/markdown"),
    (".yaml", "application/yaml"),
    (".yml", "application/yaml"),
    (".jsonl", "application/x-ndjson"),
    (".ndjson", "application/x-ndjson"),
):
    mimetypes.add_type(_ct, _ext)


def _guess_content_type(file_path: str) -> str:
    ct, _ = mimetypes.guess_type(file_path)
    return ct or "text/plain"


class MongoVfsBackend(BackendProtocol):
    """BackendProtocol implementation backed by :class:`VirtualFilesystem`.

    ``thread_id`` is resolved lazily at
    each method call via :func:`langgraph.config.get_config` so one instance
    handed to deepagents at graph-build time serves every turn with the
    correct scoping. Blobs live in S3, metadata in MongoDB.
    """

    def __init__(
        self,
        *,
        vfs: VirtualFilesystem | None = None,
        thread_id: str | None = None,
    ) -> None:
        self._vfs_override = vfs
        self._thread_id_override = thread_id

    @property
    def thread_id(self) -> str:
        """Back-compat property. Prefer :meth:`_resolve_thread_id` internally."""
        return self._resolve_thread_id()

    def _get_config_field(self, key: str) -> str | None:
        try:
            cfg = get_config()
        except (RuntimeError, KeyError, TypeError):
            return None
        if not isinstance(cfg, dict):
            return None
        configurable = cfg.get("configurable")
        if not isinstance(configurable, dict):
            return None
        value = configurable.get(key)
        return str(value) if value else None

    def _resolve_thread_id(self) -> str:
        """Return the effective ``thread_id`` for this call.

        Order of precedence:
        1. Constructor-level override (if any).
        2. ``configurable.thread_id`` from ``langgraph.config.get_config()``.
        3. The string ``"anonymous"``.
        """
        if self._thread_id_override:
            return self._thread_id_override
        return self._get_config_field("thread_id") or "anonymous"

    def _vfs(self) -> VirtualFilesystem:
        """Return the VFS singleton.

        Override wins for tests; otherwise the process-wide VFS singleton
        serves every request.
        """
        if self._vfs_override is not None:
            return self._vfs_override
        return get_vfs()

    # --- ls ------------------------------------------------------------------

    def ls(self, path: str) -> LsResult:
        thread_id = self._resolve_thread_id()
        normalized = path if path.endswith("/") else path + "/"
        all_files = self._vfs().list_files(thread_id)

        entries: list[FileInfo] = []
        subdirs: set[str] = set()
        for meta in all_files:
            if not meta.path.startswith(normalized):
                continue
            rel = meta.path[len(normalized):]
            if "/" in rel:
                subdir = rel.split("/", 1)[0]
                subdirs.add(normalized + subdir + "/")
                continue
            entries.append(
                FileInfo(
                    path=meta.path,
                    is_dir=False,
                    size=int(meta.size),
                    modified_at=meta.updated_at.isoformat(),
                )
            )
        for sub in sorted(subdirs):
            entries.append(FileInfo(path=sub, is_dir=True, size=0, modified_at=""))
        entries.sort(key=lambda e: e.get("path", ""))
        return LsResult(entries=entries)

    # --- read ----------------------------------------------------------------

    def read(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> ReadResult:
        thread_id = self._resolve_thread_id()
        try:
            raw = self._vfs().read_file(thread_id, file_path)
        except VfsFileNotFoundError:
            return ReadResult(error=f"File '{file_path}' not found")

        try:
            text = raw.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            import base64

            text = base64.b64encode(raw).decode("ascii")
            encoding = "base64"

        if encoding == "utf-8" and (offset or limit):
            lines = text.splitlines()
            text = "\n".join(lines[offset : offset + limit])

        meta = self._vfs().metadata.get(thread_id, file_path)
        file_data: FileData = {"content": text, "encoding": encoding}
        if meta is not None:
            file_data["created_at"] = meta.created_at.isoformat()
            file_data["modified_at"] = meta.updated_at.isoformat()
        return ReadResult(file_data=file_data)

    # --- write ---------------------------------------------------------------

    def write(self, file_path: str, content: str) -> WriteResult:
        thread_id = self._resolve_thread_id()
        existing = self._vfs().metadata.get(thread_id, file_path)
        if existing is not None:
            return WriteResult(
                error=(
                    f"Cannot write to {file_path} because it already exists. "
                    "Read and then make an edit, or write to a new path."
                )
            )
        self._vfs().write_file(
            thread_id,
            file_path,
            content.encode("utf-8"),
            content_type=_guess_content_type(file_path),
        )
        return WriteResult(path=file_path)

    # --- edit ----------------------------------------------------------------

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        thread_id = self._resolve_thread_id()
        try:
            raw = self._vfs().read_file(thread_id, file_path)
        except VfsFileNotFoundError:
            return EditResult(error=f"Error: File '{file_path}' not found")

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return EditResult(error=f"Cannot edit binary file {file_path}")

        if old_string not in text:
            return EditResult(
                error=f"No occurrences of {old_string!r} found in {file_path}"
            )

        if replace_all:
            new_text = text.replace(old_string, new_string)
            occurrences = text.count(old_string)
        else:
            new_text = text.replace(old_string, new_string, 1)
            occurrences = 1

        self._vfs().write_file(
            thread_id,
            file_path,
            new_text.encode("utf-8"),
            content_type=_guess_content_type(file_path),
        )
        return EditResult(path=file_path, occurrences=int(occurrences))

    # --- glob ----------------------------------------------------------------

    def glob(self, pattern: str, path: str = "/") -> GlobResult:
        thread_id = self._resolve_thread_id()
        normalized = path if path.endswith("/") else path + "/"
        hits: list[FileInfo] = []
        for meta in self._vfs().list_files(thread_id):
            if not meta.path.startswith(normalized):
                continue
            rel = meta.path[len(normalized):]
            if fnmatch.fnmatchcase(rel, pattern):
                hits.append(
                    FileInfo(
                        path=meta.path,
                        is_dir=False,
                        size=int(meta.size),
                        modified_at=meta.updated_at.isoformat(),
                    )
                )
        hits.sort(key=lambda e: e.get("path", ""))
        return GlobResult(matches=hits)

    # --- grep ----------------------------------------------------------------

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> GrepResult:
        thread_id = self._resolve_thread_id()
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return GrepResult(error=f"Invalid regex: {exc}")

        search_root = path if path else "/"
        normalized = search_root if search_root.endswith("/") else search_root + "/"
        matches: list[GrepMatch] = []
        for meta in self._vfs().list_files(thread_id):
            if not meta.path.startswith(normalized):
                continue
            if glob is not None:
                rel = meta.path[len(normalized):]
                if not fnmatch.fnmatchcase(rel, glob):
                    continue
            try:
                raw = self._vfs().read_file(thread_id, meta.path)
                text = raw.decode("utf-8")
            except (VfsFileNotFoundError, UnicodeDecodeError):
                continue
            for i, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append(
                        GrepMatch(path=meta.path, line=i, text=line)
                    )
        return GrepResult(matches=matches)

    # --- housekeeping --------------------------------------------------------

    def _now(self) -> str:  # pragma: no cover - reserved for future use
        return datetime.now(UTC).isoformat()


def mongo_backend_factory(runtime: Any) -> MongoVfsBackend:
    """Back-compat shim. Returns a :class:`MongoVfsBackend` instance whose
    ``thread_id`` and target DB are both resolved per-method-call via
    ``get_config()``. The ``runtime`` argument is accepted but ignored -
    kept so any external caller that imports this factory keeps working.
    """
    return MongoVfsBackend()
