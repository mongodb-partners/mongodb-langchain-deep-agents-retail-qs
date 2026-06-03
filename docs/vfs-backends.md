# VFS Backends

The virtual filesystem stores blobs in S3 and metadata in MongoDB. S3 is the
only supported `VFS_BACKEND` value. The `BlobStore` abstraction stays in place so a future backend
(Azure Blob, GCS, local disk) is one new module + one parametrized contract
test — see [Adding a backend](#adding-a-backend).

| Layer | S3 backend |
|---|---|
| Byte storage | `s3://<bucket>/<prefix>/<thread_id>/<path>` |
| Metadata | `vfs_files` collection |
| Listing / globbing | `vfs_files.find({thread_id})` + fnmatch |
| Auth | IAM role / credentials chain |

## Why metadata in Mongo, blobs in S3

Two reasons to keep metadata in MongoDB while blobs go to S3:

1. **Listing / globbing semantics live in MongoDB.** A single
   `vfs_files.find({thread_id})` is fast and consistent regardless of
   blob backend.
2. **Coordination plane in one place.** Index uniqueness on
   `(thread_id, path)`, `created_at` / `updated_at`, and content type
   are all enforced in the same database that owns the rest of the
   application's state.

## Configuration

```bash
VFS_BACKEND=s3
VFS_S3_BUCKET=deep-agent-artifacts
VFS_S3_PREFIX=deep-agent
VFS_S3_REGION=us-east-1
```

The bucket must exist. `boto3` resolves credentials from the standard AWS
chain (env vars → shared credentials file → IAM role).

## Object layout

```
s3://<bucket>/<prefix>/<thread_id>/<path>
```

`thread_id` in the key naturally segments the namespace. Each object
carries `thread_id` as object metadata for debuggability.

## IAM policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::deep-agent-artifacts/deep-agent/*"
    },
    {
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::deep-agent-artifacts",
      "Condition": {"StringLike": {"s3:prefix": "deep-agent/*"}}
    }
  ]
}
```

Do **not** grant `s3:ListBucket` at the bucket root — see
[security.md](security.md).

## Deepagents filesystem tools

Deepagents ships built-in `read_file` / `write_file` / `edit_file` / `ls` /
`glob` / `grep` tools; by default they store files in LangGraph state. This
repo wires a `mongo_backend_factory` into `create_deep_agent` so those tools
call into our `VirtualFilesystem` instead, and artifacts persist to S3 with
metadata in MongoDB. `thread_id` is read from the tool runtime's
`RunnableConfig.configurable` so each turn sees its own view.

```
deepagents built-in tools       BackendProtocol          VirtualFilesystem
  write_file("/notes.md")  ──▶  MongoVfsBackend.write ──▶ S3 bytes
  read_file("/notes.md")   ──▶  MongoVfsBackend.read         + vfs_files metadata
```

See [mongodb-backend.md](mongodb-backend.md) for the adapter's full contract
(how `ls` builds directory entries, how `edit` preserves `created_at`, what
happens to binary files).

## Contract tests

`tests/unit/vfs_contract.py` defines the assertions every `BlobStore` must
satisfy. Currently parameterized against:

- a dict-backed in-memory backend (test fixture)
- `S3Backend` (moto `mock_aws`)

## Adding a backend

If you add a future backend, implement `BlobStore` and parameterise the
contract suite — no other tests should need to change. You'll also need to
widen `Settings.vfs_backend` from `Literal["s3"]` to include the new tag and
add the matching env-var validation.

## Size limits

`VFS_MAX_BYTES` (default 50 MiB) is enforced at
`VirtualFilesystem.write_file`. Oversize writes raise
`VfsQuotaExceededError`.

## Upsert semantics

Writing to an existing `(thread_id, path)` overwrites the S3 key in place;
the `locator` string is stable. The metadata `updated_at` advances;
`created_at` is preserved.
