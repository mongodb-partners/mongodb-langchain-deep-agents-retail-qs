"""NL → MQL via :class:`MongoDBDatabaseToolkit` with a defense-in-depth safety wrapper.

Protections layered on top of the native toolkit:

1. **Collection allow-list**: only whitelisted collections are queryable.
2. **Underscore block**: any collection whose name begins with ``_`` is refused.
3. **Destructive-op refusal**: pipelines containing ``$out`` / ``$merge`` /
   ``$function`` / ``$where`` / ``$accumulator`` server-side JS, or
   insert/update/delete keywords, are refused.
4. **Implicit ``$limit``**: pipelines without an explicit ``$limit`` stage are
   capped at 1000 documents.
"""
from __future__ import annotations

import contextlib
import json
import re
from functools import lru_cache
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mongodb.agent_toolkit import MongoDBDatabase, MongoDBDatabaseToolkit

from ..config import get_settings
from ..models import get_llm


def _allow_list_from_settings() -> set[str]:
    """Parse ``DATA_AGENT_ALLOW_LIST`` (CSV) into a set.

    Empty/unset → empty set, which makes ``enforce_safety`` refuse every
    collection (verticals MUST configure this for the data toolkit to be
    useful).
    """
    raw = get_settings().data_agent_allow_list
    return {x.strip() for x in raw.split(",") if x.strip()}

DESTRUCTIVE_PATTERNS = re.compile(
    r"\$(out|merge|function|where|accumulator)\b", re.IGNORECASE
)
KEYWORD_PATTERNS = re.compile(
    r"\b(drop|deleteMany|deleteOne|updateMany|updateOne|insertMany|insertOne)\b"
)

DEFAULT_PIPELINE_LIMIT = 1000

# Match ``db.<collection>.<op>(...)`` or ``<collection>.<op>(...)`` query forms.
_DB_COLLECTION_PATTERN = re.compile(
    r"(?:db\.)?([A-Za-z_][A-Za-z0-9_]*)\.(?:find|aggregate|count|countDocuments|distinct)\b"
)


def _extract_collection(pipeline_text: str) -> str:
    """Best-effort parse of the target collection from a query string."""
    if not isinstance(pipeline_text, str):
        return ""
    m = _DB_COLLECTION_PATTERN.search(pipeline_text)
    if m:
        return m.group(1)
    # JSON-style ``{"collection": "foo", ...}``
    try:
        obj = json.loads(pipeline_text)
    except (json.JSONDecodeError, ValueError):
        return ""
    if isinstance(obj, dict):
        c = obj.get("collection") or obj.get("coll")
        if isinstance(c, str):
            return c
    return ""


class QueryRefusedError(RuntimeError):
    """Raised when a tool-generated query violates safety policy."""


def _looks_destructive(pipeline_text: str) -> str | None:
    if DESTRUCTIVE_PATTERNS.search(pipeline_text):
        return "destructive aggregation stage"
    if KEYWORD_PATTERNS.search(pipeline_text):
        return "destructive keyword"
    return None


def _has_explicit_limit(pipeline_text: str) -> bool:
    return "$limit" in pipeline_text


def _walk_pipeline_collections(stages: Any) -> list[str]:
    """Walk the pipeline AST and yield every collection name referenced by a
    stage that names a collection.

    Catches the bypass paths the regex layer misses:
    - ``$lookup.from`` / ``$graphLookup.from`` (cross-collection joins)
    - ``$unionWith`` (string form OR ``{coll, pipeline}`` form, recursively)
    - ``$merge.into`` / ``$out`` (writes — already destructive but flagged
      by name here too)
    """
    out: list[str] = []
    if not isinstance(stages, list):
        return out
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        for op, body in stage.items():
            if (op == "$lookup" and isinstance(body, dict) and "from" in body) or (op == "$graphLookup" and isinstance(body, dict) and "from" in body):
                out.append(str(body["from"]))
            elif op == "$unionWith":
                if isinstance(body, str):
                    out.append(body)
                elif isinstance(body, dict):
                    if "coll" in body:
                        out.append(str(body["coll"]))
                    if isinstance(body.get("pipeline"), list):
                        out.extend(_walk_pipeline_collections(body["pipeline"]))
            elif op == "$merge":
                if isinstance(body, str):
                    out.append(body)
                elif isinstance(body, dict) and "into" in body:
                    out.append(str(body["into"]))
            elif op == "$out" and isinstance(body, str):
                out.append(body)
    return out


# Collection-naming references inside a raw mongosh / non-JSON pipeline STRING
# (which never JSON-parses, so the AST walk used to skip it).
# These catch $lookup/$graphLookup `from`, $unionWith (string or {coll}),
# $merge (string or {into}), and $out targets in the textual form.
_STRING_REF_PATTERNS = (
    re.compile(r"\bfrom\s*:\s*['\"]([A-Za-z_][\w]*)['\"]"),
    re.compile(r"\binto\s*:\s*['\"]([A-Za-z_][\w]*)['\"]"),
    re.compile(r"\bcoll\s*:\s*['\"]([A-Za-z_][\w]*)['\"]"),
    re.compile(r"\$unionWith\s*:\s*['\"]([A-Za-z_][\w]*)['\"]"),
    re.compile(r"\$out\s*:\s*['\"]([A-Za-z_][\w]*)['\"]"),
    re.compile(r"\$merge\s*:\s*['\"]([A-Za-z_][\w]*)['\"]"),
)


def _scan_string_collection_refs(text: str) -> list[str]:
    """Best-effort scan of join/union/write collection targets in a raw string."""
    refs: list[str] = []
    for pat in _STRING_REF_PATTERNS:
        refs.extend(m.group(1) for m in pat.finditer(text))
    return refs


def _split_collection_names(raw: Any) -> list[str]:
    """Normalize the schema tool's ``collection_names`` input to a name list.

    Accepts a comma-separated string (``"orders, customers"``) or a list.
    """
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    if isinstance(raw, str):
        return [x.strip() for x in raw.split(",") if x.strip()]
    return []


def _refuse_collection(name: str, allow_list: set[str], *, allow_all: bool) -> str | None:
    """Return a refusal reason if ``name`` is internal or disallowed, else None.

    Underscore-prefixed (internal) collections are ALWAYS refused, even in
    explicit open mode. The allow-list itself is enforced unless ``allow_all``.
    """
    if name.startswith("_"):
        return f"refusing internal collection '{name}' (underscore-prefixed)"
    if not allow_all:
        if not allow_list:
            return "data-agent allow-list is empty (set DATA_AGENT_ALLOW_LIST)"
        if name not in allow_list:
            return f"disallowed collection '{name}'; allow-list: {sorted(allow_list)}"
    return None


def _inject_limit_mongosh(text: str, limit: int) -> str | None:
    """Inject a ``{$limit: N}`` stage into a ``db.coll.aggregate([...])`` string
    that lacks one. Returns the rewritten string, or None if no aggregate array
    can be located (quote-aware bracket match)."""
    m = re.search(r"\.aggregate\s*\(\s*\[", text)
    if not m:
        return None
    start = m.end() - 1  # index of the opening '['
    depth = 0
    in_str: str | None = None
    i = start
    while i < len(text):
        ch = text[i]
        if in_str is not None:
            if ch == in_str and text[i - 1] != "\\":
                in_str = None
        elif ch in "'\"":
            in_str = ch
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                inner = text[start + 1 : i].strip()
                sep = ", " if inner else ""
                return f'{text[:i]}{sep}{{ "$limit": {limit} }}{text[i:]}'
        i += 1
    return None


def enforce_safety(
    collection: str,
    pipeline_text: str,
    allow_list: set[str],
    *,
    allow_all: bool = False,
) -> str:
    """Inspect a pipeline request and return a safe version, or raise.

    Walks the aggregation AST when the pipeline parses as JSON, and applies a
    string-level scan for join/union/write collection targets when it is a
    mongosh string. An empty allow-list fails CLOSED unless ``allow_all``
    (DATA_AGENT_ALLOW_ALL) is set.
    """
    if collection.startswith("_"):
        raise QueryRefusedError(
            f"refusing query on internal collection '{collection}' (underscore-prefixed)"
        )
    if not allow_all:
        if not allow_list:
            raise QueryRefusedError(
                "refusing query: data-agent allow-list is empty "
                "(set DATA_AGENT_ALLOW_LIST, or DATA_AGENT_ALLOW_ALL=true to opt out)"
            )
        if collection not in allow_list:
            raise QueryRefusedError(
                f"refusing query on disallowed collection '{collection}'; "
                f"allow-list: {sorted(allow_list)}"
            )
    reason = _looks_destructive(pipeline_text)
    if reason:
        raise QueryRefusedError(f"refusing destructive pipeline ({reason})")

    parsed: Any = None
    with contextlib.suppress(json.JSONDecodeError, ValueError):
        parsed = json.loads(pipeline_text)

    # Check cross-collection references for BOTH JSON-list input (AST walk) and
    # mongosh-string input (regex scan) — the latter previously sailed through.
    refs = (
        _walk_pipeline_collections(parsed)
        if isinstance(parsed, list)
        else _scan_string_collection_refs(pipeline_text)
    )
    for ref in refs:
        msg = _refuse_collection(ref, allow_list, allow_all=allow_all)
        if msg:
            raise QueryRefusedError(f"refusing pipeline reference: {msg}")

    # Detect an explicit $limit structurally (not by substring), and cap
    # mongosh-string aggregates too.
    if isinstance(parsed, list):
        has_limit = any(isinstance(s, dict) and "$limit" in s for s in parsed)
        if not has_limit:
            parsed.append({"$limit": DEFAULT_PIPELINE_LIMIT})
            return str(json.dumps(parsed))
        return pipeline_text
    if "$limit" not in pipeline_text:
        injected = _inject_limit_mongosh(pipeline_text, DEFAULT_PIPELINE_LIMIT)
        if injected is not None:
            return injected
    return pipeline_text


@lru_cache(maxsize=1)
def _database() -> MongoDBDatabase:
    """Cached :class:`MongoDBDatabase` singleton.

    Scope the toolkit to the allow-list at the source via
    ``include_collections`` so ``get_usable_collection_names`` and the schema
    tool can only ever see allow-listed collections — defense in depth behind
    the wrapper-level refusal.
    """
    s = get_settings()
    uri_secret = s.data_agent_mongodb_uri or s.mongodb_uri
    allow_list = _allow_list_from_settings()
    include = sorted(allow_list) if (allow_list and not s.data_agent_allow_all) else None
    return MongoDBDatabase.from_connection_string(
        connection_string=uri_secret.get_secret_value(),
        database=s.mongodb_db,
        include_collections=include,
    )


@lru_cache(maxsize=1)
def _toolkit() -> MongoDBDatabaseToolkit:
    return MongoDBDatabaseToolkit(db=_database(), llm=get_llm())


def get_data_tools() -> list[BaseTool]:
    """Return the MongoDBDatabaseToolkit tools wrapped with the safety layer.

    Allow-list comes from ``Settings.data_agent_allow_list`` (CSV env var).
    """
    tools = list(_toolkit().get_tools())
    allow_list = _allow_list_from_settings()
    allow_all = get_settings().data_agent_allow_all
    return [
        _SafeToolWrapper(inner=t, allow_list=allow_list, allow_all=allow_all)
        for t in tools
    ]


class _SafeToolWrapper(BaseTool):
    """Wraps a toolkit tool; inspects every call for unsafe queries."""

    inner: Any
    allow_list: set[str]
    allow_all: bool = False
    name: str = "mongo_tool"
    description: str = "MongoDB tool (safety-wrapped)"

    def __init__(self, inner: Any, allow_list: set[str], allow_all: bool = False) -> None:
        kwargs: dict[str, Any] = dict(
            inner=inner,
            allow_list=allow_list,
            allow_all=allow_all,
            name=inner.name,
            description=inner.description + " (safety-wrapped)",
        )
        # Forward the inner tool's args_schema so the LLM sees the real
        # parameter names (e.g. ``query``, ``collection_names``) instead of
        # the ``*args, **kwargs`` placeholders our wrapper declares.
        inner_schema = getattr(inner, "args_schema", None)
        if inner_schema is not None:
            kwargs["args_schema"] = inner_schema
        super().__init__(**kwargs)

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        pipeline_text = (
            kwargs.get("query")
            or kwargs.get("pipeline")
            or (args[0] if args else "")
        )
        # Only ``mongodb_query`` / ``mongodb_query_checker`` hit live data; the
        # ``mongodb_schema`` and ``mongodb_list_collections`` tools take
        # non-query kwargs (``collection_names``, ``tool_input``) and must not
        # be funneled through ``enforce_safety``.
        if self.inner.name not in {"mongodb_query", "mongodb_query_checker"}:
            pipeline_text = ""
            # The schema tool dumps sample documents for the collection names
            # it is given. Gate those names by the same
            # allow-list so it cannot leak internal / non-allow-listed
            # collections (e.g. long_term_memory, agent_log, checkpoints).
            if "schema" in self.inner.name:
                raw_names = (
                    kwargs.get("collection_names")
                    or kwargs.get("tool_input")
                    or (args[0] if args else "")
                )
                for cn in _split_collection_names(raw_names):
                    msg = _refuse_collection(cn, self.allow_list, allow_all=self.allow_all)
                    if msg:
                        return f"QUERY REFUSED: {msg}"
        collection = kwargs.get("collection") or _extract_collection(pipeline_text)
        if isinstance(pipeline_text, str) and pipeline_text:
            try:
                pipeline_text = enforce_safety(
                    collection, pipeline_text, self.allow_list, allow_all=self.allow_all
                )
            except QueryRefusedError as exc:
                return f"QUERY REFUSED: {exc}"
            if kwargs.get("query") is not None:
                kwargs["query"] = pipeline_text
            elif kwargs.get("pipeline") is not None:
                kwargs["pipeline"] = pipeline_text
            elif args:
                args = (pipeline_text, *args[1:])
        # Best-effort contract: a raised exception here would orphan the
        # parent agent's ``tool_use`` block and Bedrock would reject the next
        # turn. Return a structured error string instead so the agent can
        # adjust and retry or move on.
        try:
            return self.inner._run(*args, **kwargs)
        except Exception as exc:
            return f"QUERY ERROR: {type(exc).__name__}: {exc}"[:400]

    async def _arun(self, *args: Any, **kwargs: Any) -> Any:
        return self._run(*args, **kwargs)
