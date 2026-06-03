# MongoDB Backend for Deepagents Filesystem

This document explains how deepagents' built-in filesystem tools (`read_file`,
`write_file`, `edit_file`, `ls`, `glob`, `grep`) persist to MongoDB through
the `MongoVfsBackend` adapter. It is explanation-oriented; for the
usage-facing story of the S3 backend, see [vfs-backends.md](vfs-backends.md).
For the raw protocol surface, see [api-reference.md](api-reference.md).

## What deepagents expects

`deepagents.create_deep_agent` accepts a `backend` argument that is either a
`BackendProtocol` instance or a `BackendFactory` callable. The factory is
invoked per turn with a `ToolRuntime`, so every invocation can produce a
backend view scoped to the current config (thread, user, permissions, etc.).

The protocol methods return structured dataclasses:

| Method | Returns |
|---|---|
| `ls(path)` | `LsResult(entries=list[FileInfo])` |
| `read(file_path, offset=0, limit=2000)` | `ReadResult(file_data=FileData)` |
| `write(file_path, content)` | `WriteResult(path=str)` |
| `edit(file_path, old_string, new_string, replace_all=False)` | `EditResult(path=str, occurrences=int)` |
| `glob(pattern, path="/")` | `GlobResult(matches=list[FileInfo])` |
| `grep(pattern, path=None, glob=None)` | `GrepResult(matches=list[GrepMatch])` |

Paths are absolute (`/` rooted). `FileData` has `content: str` (utf-8 or
base64), `encoding: str`, `created_at: str`, `modified_at: str`.

## What this repo implements

`deep_agent.backends.mongo_backend` provides two symbols:

- **`MongoVfsBackend`** - a concrete `BackendProtocol` subclass. Holds an
  optional `VirtualFilesystem` and `thread_id` override; otherwise both are
  resolved lazily. Every method translates the deepagents call shape into
  `VirtualFilesystem` operations. The `thread_id` is resolved **per method
  call** via `langgraph.config.get_config()` (precedence: constructor
  override → `configurable.thread_id` → `"anonymous"`), so a single instance
  handed to deepagents at graph-build time serves every turn with the correct
  scope.
- **`mongo_backend_factory(runtime)`** - a back-compat shim. It accepts but
  **ignores** the `runtime` argument and returns a bare `MongoVfsBackend()`
  (which resolves `thread_id` and the target DB per call via `get_config()`).
  Kept only so external callers importing it keep working; **`build_graph()`
  does not use it.**

`build_graph()` passes a `CompositeBackend` **instance** (via
`_build_backend(store)`), not the factory. Because `MongoVfsBackend` resolves
its scope lazily on every call, one instance serves every turn with the right
thread scope without per-turn rebuilds.

### CompositeBackend routing

`build_graph()` wraps the filesystem backend in a
`deepagents.backends.composite.CompositeBackend` that routes by path prefix:

- **`/memories/**`** → a per-user `StoreBackend` (`deepagents.backends.store`)
  constructed with the same `store` the rest of the agent uses
  (`MongoDBStore`), with a `namespace` factory that reads `user_id` from the
  runtime config. This is the semantic-memory surface (backed by
  `MongoDBStore.put`), reserved for the typed `remember_fact` tool — distinct
  from the filesystem VFS.
- **everything else** → the default leg, `MongoVfsBackend()` (S3 blobs +
  MongoDB metadata, thread-scoped).

The uncheckpointed builder (`build_graph_uncheckpointed()`, used by unit tests
and linters) passes a bare `MongoVfsBackend()` instead: with no
checkpointer/store, the `StoreBackend` route would error, so the composite is
reserved for the full builder.

## Behavioural contract

### Paths

Paths are stored verbatim in `vfs_files.path` so a round trip of `write("/notes/a.md")`
followed by `ls("/notes")` returns the same path the agent wrote.

### `ls`

Non-recursive. Given `path="/"`:

- Every file whose `path` does not start with `/` is ignored (they're all
  absolute anyway).
- Files directly under the directory become `FileInfo(path=..., is_dir=False, size=..., modified_at=...)`.
- Any path with a remaining `/` after the directory prefix is collapsed to
  a single `FileInfo(path="/subdir/", is_dir=True, size=0, modified_at="")`
  entry. Duplicate subdirectory names are deduped.

### `read`

Reads bytes from the VFS, tries to decode as utf-8. If decoding fails, the
bytes are base64-encoded and `encoding="base64"` is reported so deepagents
still has a round-trippable representation. `offset`/`limit` slice text
content by line (binary files ignore both).

### `write`

Matches the deepagents contract: **refuses to overwrite an existing path**.
If a file already exists at the given path, the backend returns
`WriteResult(error="Cannot write to <file_path> because it already exists.
Read and then make an edit, or write to a new path.")` - the message includes
the full `file_path`. The agent is expected to call `edit` instead.

### `edit`

Loads the current bytes, decodes as utf-8, performs `str.replace` (once by
default, or everywhere when `replace_all=True`). Returns the occurrence count.
Re-writes the whole file through `VirtualFilesystem.write_file`, which
preserves `created_at` and advances `updated_at` (see
`VfsMetadataStore.upsert`).

Errors:

- File missing → `EditResult(error="Error: File '<path>' not found")`.
- Binary file → `EditResult(error="Cannot edit binary file <path>")`.
- No match → `EditResult(error="No occurrences of <old> found in <path>")`.

### `glob`

Scopes to `path` (default `/`), applies `fnmatch.fnmatchcase` to the portion
of the path after the scope, returns `FileInfo` entries sorted by path.

### `grep`

Regex over every file's utf-8 text content (binary files silently skipped).
Returns `GrepMatch(path, line, text)` entries with 1-indexed line numbers.
Optional `glob` narrows to matching paths; optional `path` narrows to a
subtree.

## Thread scoping

Every method on `MongoVfsBackend` resolves its `thread_id` (via
`get_config()`, falling back to `"anonymous"`) and calls through to
`VirtualFilesystem` with it. The metadata collection's compound unique index
on `(thread_id, path)` prevents one thread from accidentally reading another
thread's file - `ls("/")` for thread `t1` queries
`vfs_files.find({thread_id: "t1"})` and never sees `t2`'s files. This is the
structural enforcement of the [thread-scoping invariant](architecture.md#invariants).

## Failure modes

| Failure | Behaviour |
|---|---|
| Runtime has no `configurable.thread_id` | `_resolve_thread_id()` falls back to `thread_id="anonymous"`; tools continue to work but every "anonymous" caller shares a namespace. Surface the `user_id` requirement at the entrypoint (CLI + `/chat` already do this). |
| S3 write raises | Exception propagates through `VirtualFilesystem` up to the `write_file` call; the deepagents tool wrapper surfaces it as a tool error so the LLM can retry or route around. |
| File exceeds `VFS_MAX_BYTES` | `VfsQuotaExceededError` → the adapter does not catch it; the tool-call boundary converts it to an error message. |
| utf-8 decode failure on `read` | Returns `FileData` with `encoding="base64"`. Deepagents still considers the read successful; the agent sees base64. |
| utf-8 decode failure on `edit` | Returns `EditResult(error=...)`; the agent cannot edit binary files in place. |

## Tests

Unit tests in `tests/unit/test_mongo_backend.py` exercise every method of
the protocol plus thread-scoping and the per-method `configurable.thread_id`
resolution. The CompositeBackend wiring (`_build_backend`, the `/memories/`
route) is covered in `tests/unit/test_graph.py`. The live S3 contract is
exercised in `tests/integration/test_vfs_backends.py`.

## Why not in-state?

Deepagents' default `StateBackend` stores files on the LangGraph state
dictionary. Three reasons we override it:

1. **Durability beyond the checkpoint**. State is persisted through the
   checkpointer, but large artifacts bloat checkpoint size and make restore
   slow. MongoDB blob storage (or S3) decouples artifact bytes from the
   per-super-step checkpoint.
2. **Operator visibility**. Files in MongoDB are queryable from any
   dashboard; files in LangGraph state are opaque blobs.
3. **Cross-thread operations**. Future work can surface VFS files in a
   review UI without replaying a checkpoint.

The trade-off: every file operation now round-trips through MongoDB (or S3),
so local dev feels slightly slower than the in-state backend. Connection
pooling in the singleton `pymongo.MongoClient` keeps the overhead small.
