"""FastAPI application exposing the deep-agent graph.

Endpoints:

- ``GET  /live``       — liveness probe. Never touches dependencies.
- ``GET  /ready``      — readiness probe. 200 only after lifespan
                         completes and a cached MongoDB ping succeeded
                         within ``Settings.readiness_cache_ttl_s``.
                         Returns 503 during shutdown drain.
- ``GET  /health``     — back-compat shape; reuses the readiness cache.
- ``GET  /plans``      — latest planner todo snapshot for a thread.
- ``POST /chat``       — SSE token stream. Per-turn timeout via
                         ``CHAT_TURN_TIMEOUT_S``; correlation IDs
                         propagated through the response header, the
                         leading SSE frame, and ``RunnableConfig``.
                         Honors graceful shutdown (drains in-flight
                         streams up to ``SHUTDOWN_GRACE_PERIOD_S``).
- ``POST /feedback``   — persists run feedback.
- ``GET  /interrupts`` and ``POST /interrupts/resume`` — registered
                         only when ``HITL_TOOLS`` is non-empty.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field
from pymongo.errors import PyMongoError
from sse_starlette.sse import EventSourceResponse
from starlette.middleware.base import BaseHTTPMiddleware

from ..config import get_settings
from ..graph import _agent_log, build_graph
from ..persistence.indexes import ensure_indexes
from ..persistence.mongo import get_client, get_db
from ..persistence.response_cache import build_response_cache

log = logging.getLogger(__name__)

# ─── Module-level state ──────────────────────────────────────────────────

_GRAPH: Any | None = None
# Per-model graph cache. Keyed on the requested LLM model id.
# Sized at the registry length; fits comfortably in memory.
_GRAPHS_BY_MODEL: dict[str, Any] = {}
# Serializes per-model graph builds (the call site runs _graph_for via
# asyncio.to_thread, so a threading.Lock is the right primitive).
_GRAPH_BUILD_LOCK = threading.Lock()
_SHUTDOWN_EVENT = asyncio.Event()
_IN_FLIGHT_STREAMS: set[asyncio.Task[Any]] = set()
_READINESS_CACHE: dict[str, Any] = {"ok": False, "checked_at": 0.0, "error": None}
# Tools whose turn must never be response-cached (a cache replay would stream
# the stored text without re-running the tool, skipping the mutation).
_MUTATING_TOOLS: frozenset[str] = frozenset(
    {
        "add_to_cart",
        "update_cart_item",
        "remove_from_cart",
        "clear_cart",
        "place_order",
        "savings_calculator",
    }
)
_correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)


def _is_valid_uuid(s: str) -> bool:
    try:
        uuid.UUID(s)
        return True
    except (ValueError, AttributeError):
        return False


def get_correlation_id() -> str:
    """Return the active correlation ID, or generate one if absent."""
    return _correlation_id.get() or str(uuid.uuid4())


def _refresh_readiness_cache() -> None:
    s = get_settings()
    now = time.monotonic()
    if (now - _READINESS_CACHE["checked_at"]) <= s.readiness_cache_ttl_s and s.readiness_cache_ttl_s > 0:
        return
    try:
        get_client().admin.command("ping")
        _READINESS_CACHE.update(ok=True, checked_at=now, error=None)
    except Exception as exc:
        _READINESS_CACHE.update(ok=False, checked_at=now, error=type(exc).__name__)


def _extract_todos_from_event(event: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Pull a normalized todo list out of a LangGraph stream event, or
    ``None`` if the event doesn't carry one.

    The deepagents harness threads ``state["todos"]`` through every
    chain step. We grab the most recent value off ``output`` /
    ``chunk`` / ``input`` (in that priority order) and project each
    todo to ``{id, text, status}`` to match the ``/plans`` shape the
    frontend already consumes.
    """
    data = event.get("data") or {}
    raw: Any = None
    for key in ("output", "chunk", "input"):
        candidate = data.get(key)
        if isinstance(candidate, dict) and "todos" in candidate:
            raw = candidate.get("todos")
            break
    if not isinstance(raw, list):
        return None
    out: list[dict[str, Any]] = []
    for t in raw:
        if not isinstance(t, dict):
            continue
        status = t.get("status")
        if status not in ("pending", "in_progress", "completed"):
            status = "pending"
        out.append(
            {
                "id": str(t.get("id", "")),
                "text": str(t.get("content") or t.get("text") or ""),
                "status": status,
            }
        )
    return out


def _token_text(content: Any) -> str:
    """Project an LLM chunk's ``content`` down to a plain-text slice.

    Bedrock Anthropic streams ``AIMessageChunk.content`` as either a bare
    string (final writer prose) or a list of structured blocks —
    ``[{"type": "text", "text": "..."}, {"type": "tool_use", "input": {...}}]``.
    Only ``text`` blocks are user-visible.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


class ChatRequest(BaseModel):
    user_id: str = Field(
        ...,
        description=(
            "Required: scopes memory + the thread namespace. NOT authenticated "
            "— trust-on-input; add edge auth for cross-user isolation."
        ),
    )
    thread_id: str | None = Field(None, description="Optional checkpoint/thread ID (resumes state)")
    message: str
    # Optional Bedrock inference-profile id. Must be in
    # Settings.available_models; absent → server's default LLM_MODEL.
    model: str | None = Field(None, description="Optional model override")


class FeedbackRequest(BaseModel):
    run_id: str
    score: float
    comment: str | None = None
    user_id: str


# HITL resume body. Model lives at module scope (not inside the conditional
# /interrupts route block) so FastAPI can resolve the forward ref under
# ``from __future__ import annotations``.
class ResumeRequest(BaseModel):
    thread_id: str
    decision: Literal["approve", "edit", "reject"]
    edited_action: dict[str, Any] | None = None
    message: str | None = None


def _cors_origins() -> list[str]:
    """Permissive in dev, strict in prod."""
    if get_settings().allow_insecure:
        return ["*"]
    return [os.environ.get("FRONTEND_ORIGIN", "http://localhost:3000")]


# ─── Correlation-ID middleware ───────────────────────────────────────────


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Resolves or generates a correlation ID per request.

    Reads ``X-Correlation-Id`` from the request (validates as UUID v4);
    generates one if missing/invalid; binds it
    to a ContextVar so logs can include it; echoes it on the response.
    """

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        cid = request.headers.get("X-Correlation-Id", "").strip()
        if not _is_valid_uuid(cid):
            cid = str(uuid.uuid4())
        token = _correlation_id.set(cid)
        try:
            response: Response = await call_next(request)
        finally:
            _correlation_id.reset(token)
        response.headers["X-Correlation-Id"] = cid
        return response


# ─── Lifespan ────────────────────────────────────────────────────────────


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Provision indexes once, compile graph once.

    The shutdown phase flips ``/ready`` to 503, waits up to
    ``SHUTDOWN_GRACE_PERIOD_S`` for in-flight SSE streams to drain, signals
    stragglers via ``_SHUTDOWN_EVENT``, then closes the MongoClient.
    """
    # Only provision indexes on boot when explicitly opted in.
    # In the documented RBAC setup the runtime role has no CREATE_INDEX; DDL
    # is a one-shot bootstrap under the admin role, not a per-boot self-heal.
    if get_settings().provision_indexes_on_boot:
        try:
            ensure_indexes()
        except Exception:
            log.warning("index provisioning failed", exc_info=True)
    global _GRAPH
    _GRAPH = build_graph()
    yield
    # ─ Shutdown ─
    log.info("lifespan: entering shutdown")
    _SHUTDOWN_EVENT.set()  # /ready -> 503; in-flight handlers see the flag
    s = get_settings()
    if _IN_FLIGHT_STREAMS:
        log.info(
            "shutdown: waiting up to %ds for %d in-flight streams",
            s.shutdown_grace_period_s,
            len(_IN_FLIGHT_STREAMS),
        )
        try:
            await asyncio.wait_for(
                asyncio.gather(*_IN_FLIGHT_STREAMS, return_exceptions=True),
                timeout=s.shutdown_grace_period_s,
            )
        except TimeoutError:
            log.warning(
                "shutdown: %d streams still active after %ds grace period",
                len(_IN_FLIGHT_STREAMS),
                s.shutdown_grace_period_s,
            )
    # Flush + stop the AgentLog daemon worker BEFORE closing the MongoClient,
    # so the final super-step's log doc is written rather than
    # dropped (and the worker doesn't insert into a closed client).
    with contextlib.suppress(Exception):
        _agent_log().close(timeout=s.shutdown_grace_period_s)
    with contextlib.suppress(Exception):
        get_client().close()


# ─── Helpers ─────────────────────────────────────────────────────────────


class _ShutdownInterrupted(RuntimeError):
    """Raised inside the SSE driver when shutdown is requested mid-stream."""


async def _map_graph_events(
    active_graph: Any, stream_input: Any, config: RunnableConfig
) -> AsyncGenerator[dict[str, Any], None]:
    """Drive ``astream_events`` and map LangGraph events to SSE frames.

    Shared by ``/chat`` (input = state) and ``/interrupts/resume`` (input =
    ``Command(resume=...)``). Yields ``token`` / ``status`` / ``plan`` frames;
    raises :class:`_ShutdownInterrupted` if the server begins draining.
    """
    last_plan_signature: tuple[Any, ...] = ()
    async for event in active_graph.astream_events(
        stream_input, config=config, version="v2"
    ):
        if _SHUTDOWN_EVENT.is_set():
            raise _ShutdownInterrupted()
        ev = event.get("event")
        if ev == "on_chat_model_stream":
            chunk = event["data"].get("chunk")
            text = _token_text(getattr(chunk, "content", None))
            if text:
                yield {"event": "token", "data": text}
        elif ev == "on_tool_start":
            name = event.get("name") or ""
            if name:
                yield {
                    "event": "status",
                    "data": json.dumps({"phase": "tool_start", "name": name}),
                }
        elif ev == "on_tool_end":
            name = event.get("name") or ""
            if name:
                yield {
                    "event": "status",
                    "data": json.dumps({"phase": "tool_end", "name": name}),
                }
        elif ev in ("on_chain_end", "on_chain_stream"):
            todos = _extract_todos_from_event(event)
            if todos is None:
                continue
            sig = tuple(
                (t.get("id"), t.get("text"), t.get("status")) for t in todos
            )
            if sig != last_plan_signature:
                last_plan_signature = sig
                yield {
                    "event": "plan",
                    "data": json.dumps(
                        {"todos": todos, "updated_at": datetime.now(UTC).isoformat()}
                    ),
                }


async def _interrupt_frame(
    active_graph: Any, config: RunnableConfig, thread_id: str
) -> dict[str, Any] | None:
    """After the graph pauses, build an ``interrupt`` SSE frame from the
    durable checkpoint (``get_state().interrupts``), or None if not paused.

    The frame carries the proposed ``place_order`` action + args so the UI can
    render an approve/edit/reject card. Robust across langgraph versions: reads
    the top-level ``interrupts`` tuple, falling back to per-task interrupts.
    """
    try:
        state = await active_graph.aget_state(config)
    except Exception:
        return None
    interrupts = list(getattr(state, "interrupts", None) or [])
    if not interrupts:
        for task in getattr(state, "tasks", None) or []:
            interrupts.extend(getattr(task, "interrupts", None) or [])
    if not interrupts:
        return None
    value = getattr(interrupts[0], "value", None)
    action: dict[str, Any] = {}
    allowed: list[str] = ["approve", "edit", "reject"]
    if isinstance(value, dict):
        requests = value.get("action_requests") or []
        if requests and isinstance(requests[0], dict):
            a = requests[0]
            action = {
                "name": a.get("name"),
                "args": a.get("args", {}),
                "description": a.get("description", ""),
            }
        configs = value.get("review_configs") or []
        if configs and isinstance(configs[0], dict) and configs[0].get(
            "allowed_decisions"
        ):
            allowed = list(configs[0]["allowed_decisions"])
    return {
        "event": "interrupt",
        "data": json.dumps(
            {"thread_id": thread_id, "action": action, "allowed_decisions": allowed}
        ),
    }


def _sse_response(
    drive: Any, cid: str, timeout: int
) -> EventSourceResponse:
    """Wrap an async-generator ``drive`` (yields SSE frames) in the producer/
    consumer machinery: leading ``correlation`` frame, a wall-clock timeout,
    trailing ``done``, draining-aware ``error`` codes, and in-flight
    registration so lifespan shutdown can cancel the stream.

    Shared by ``/chat`` and ``/interrupts/resume`` so a resumed turn streams
    exactly like a fresh one.
    """

    async def _drive_with_timeout() -> AsyncIterator[dict[str, Any]]:
        agen = drive()
        iterator = agen.__aiter__()
        deadline = time.monotonic() + timeout if timeout > 0 else None
        try:
            while True:
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError()
                    try:
                        frame = await asyncio.wait_for(
                            iterator.__anext__(), timeout=remaining
                        )
                    except StopAsyncIteration:
                        return
                else:
                    try:
                        frame = await iterator.__anext__()
                    except StopAsyncIteration:
                        return
                yield frame
        finally:
            with contextlib.suppress(Exception):
                await agen.aclose()

    _SENTINEL: object = object()
    queue: asyncio.Queue[dict[str, Any] | object] = asyncio.Queue(maxsize=64)

    async def _producer() -> None:
        try:
            await queue.put({"event": "correlation", "data": cid})
            try:
                async for frame in _drive_with_timeout():
                    await queue.put(frame)
                await queue.put({"event": "done", "data": "[DONE]"})
            except TimeoutError:
                log.warning("chat turn timeout (%ds) cid=%s", timeout, cid)
                await queue.put({"event": "error", "data": "turn_timeout"})
            except _ShutdownInterrupted:
                await queue.put({"event": "error", "data": "shutdown"})
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    queue.put_nowait({"event": "error", "data": "shutdown"})
                raise
            except Exception:
                log.exception("chat stream failed", extra={"correlation_id": cid})
                await queue.put(
                    {"event": "error", "data": f"internal_error cid={cid}"}
                )
        finally:
            with contextlib.suppress(Exception):
                queue.put_nowait(_SENTINEL)

    producer_task = asyncio.create_task(_producer(), name=f"sse-{cid}")
    _IN_FLIGHT_STREAMS.add(producer_task)
    producer_task.add_done_callback(_IN_FLIGHT_STREAMS.discard)

    async def _event_stream() -> AsyncGenerator[dict[str, Any], None]:
        try:
            while True:
                frame = await queue.get()
                if frame is _SENTINEL:
                    return
                assert isinstance(frame, dict)
                yield frame
        finally:
            if not producer_task.done():
                producer_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await producer_task

    return EventSourceResponse(_event_stream())


def _graph() -> Any:
    if _GRAPH is None:
        raise RuntimeError("graph requested before lifespan startup completed")
    return _GRAPH


def _allowed_models() -> list[str]:
    """Parse Settings.available_models into a list of profile ids."""
    raw = get_settings().available_models or ""
    return [m.strip() for m in raw.split(",") if m.strip()]


def _model_label(model_id: str) -> str:
    """Render a human-friendly label for the dropdown.

    Strips the ``us./global.`` region prefix and the trailing ``-v1:0``
    revision so e.g. ``us.amazon.nova-pro-v1:0`` -> ``amazon.nova-pro``.
    """
    if model_id.startswith(("us.", "global.")):
        model_id_short = model_id.split(".", 1)[1]
    else:
        model_id_short = model_id
    return model_id_short


def _graph_for(model: str | None) -> Any:
    """Per-model graph cache.

    Returns the lifespan-built default graph when ``model`` is None or
    matches the default. Otherwise builds and caches a graph for that
    profile, validating against ``available_models`` first.
    """
    s = get_settings()
    if not model or model == s.llm_model:
        return _graph()

    allowed = _allowed_models()
    if allowed and model not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"model '{model}' not in AVAILABLE_MODELS",
        )
    cached = _GRAPHS_BY_MODEL.get(model)
    if cached is not None:
        return cached
    # Serialize the build so two concurrent first-requests for the same model
    # don't both compile (double Bedrock init). A threading.Lock
    # is correct here because the call site runs us via asyncio.to_thread.
    with _GRAPH_BUILD_LOCK:
        cached = _GRAPHS_BY_MODEL.get(model)
        if cached is not None:
            return cached
        log.info("compiling graph for model=%s", model)
        g = build_graph(model=model)
        _GRAPHS_BY_MODEL[model] = g
        return g


# ─── App factory ─────────────────────────────────────────────────────────


def create_app() -> FastAPI:
    app = FastAPI(title="deep_agent", version="0.1.0", lifespan=_lifespan)
    app.add_middleware(CorrelationIdMiddleware)
    # Never combine wildcard origins with credentialed CORS — Starlette would
    # reflect any Origin AND set Allow-Credentials, letting any
    # site make credentialed cross-origin calls in dev. This API is token-less,
    # so credentials aren't needed when origins is "*".
    _origins = _cors_origins()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=_origins != ["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ─── Probes ─────────────────────────────────────────────────

    @app.get("/live")
    def live() -> dict[str, Any]:
        """Liveness probe. NEVER touches dependencies."""
        return {"status": "live"}

    @app.get("/ready")
    def ready() -> Response:
        """Readiness probe."""
        if _SHUTDOWN_EVENT.is_set():
            return JSONResponse({"status": "draining"}, status_code=503)
        if _GRAPH is None:
            return JSONResponse({"status": "starting"}, status_code=503)
        _refresh_readiness_cache()
        if _READINESS_CACHE["ok"]:
            return JSONResponse({"status": "ready", "checks": {"mongo": "ok"}})
        return JSONResponse(
            {"status": "not_ready", "checks": {"mongo": _READINESS_CACHE["error"]}},
            status_code=503,
        )

    @app.get("/health")
    def health() -> dict[str, Any]:
        """Backward-compat probe; reuses the readiness cache.

        Does NOT echo the Atlas DB name or the bound model id — those are infra
        fingerprinting on an unauthenticated endpoint. The
        model id is available to the UI via the authenticated-path /models.
        """
        _refresh_readiness_cache()
        return {
            "status": "ok" if _READINESS_CACHE["ok"] else "degraded",
            "mongo": "ok" if _READINESS_CACHE["ok"] else f"error: {_READINESS_CACHE['error']}",
            "ts": datetime.now(UTC).isoformat(),
        }

    @app.get("/models")
    def models() -> dict[str, Any]:
        """List verified Bedrock models the UI may select.

        Returns ``{"default": <id>, "models": [{"id", "label"}, ...]}``.
        The dropdown is populated from this list.
        """
        s = get_settings()
        ids = _allowed_models()
        # Always include the default first if it's not already listed.
        if s.llm_model not in ids:
            ids = [s.llm_model, *ids]
        return {
            "default": s.llm_model,
            "models": [{"id": m, "label": _model_label(m)} for m in ids],
        }

    # ─── Reads ──────────────────────────────────────────────────

    @app.get("/stats")
    def stats() -> dict[str, Any]:
        """Live catalog counts for the storefront landing-page header.

        Aggregates over the ``products`` collection so the hero numbers
        (Products / Categories / On Sale) reflect the seeded catalog rather
        than hard-coded placeholders. Unauthenticated and read-only — it
        returns only aggregate counts, never document contents. Best-effort:
        on any DB error it returns nulls so the UI falls back to its static
        defaults instead of rendering an error.
        """
        try:
            products = get_db()["products"]
            return {
                "products": products.count_documents({}),
                "categories": len(products.distinct("category")),
                "on_sale": products.count_documents({"sale_price_usd": {"$ne": None}}),
            }
        except PyMongoError as exc:
            log.warning("stats endpoint failed: %s", exc)
            return {"products": None, "categories": None, "on_sale": None}

    @app.get("/plans")
    def plans(user_id: str, thread_id: str) -> dict[str, Any]:
        """Latest planner snapshot for a conversation.

        ``thread_id`` is the per-conversation sub the client also passes to
        ``/chat``; the server composes the same ``f"{user_id}:{sub}"`` key the
        writer stored under. Ordered by ``ts`` (restart-robust — ``step``
        resets to 0 on process restart).

        ``user_id`` is an untrusted scoping key, NOT an authentication
        boundary — an unauthenticated caller can set any value. Deploy edge
        auth if cross-user isolation matters. Always 200 with
        ``{"todos": [...], "updated_at": <iso | null>}``.
        """
        composite = f"{user_id}:{thread_id or 'default'}"
        cursor = get_db()[get_settings().agent_log_collection].find(
            {"thread_id": composite, "user_id": user_id},
            sort=[("ts", -1)],
            limit=1,
        )
        doc = next(iter(cursor), None)
        if doc is None:
            return {"todos": [], "updated_at": None}
        todos = [
            {"id": t.get("id", ""), "text": t.get("content", ""), "status": t.get("status", "pending")}
            for t in (doc.get("todos") or [])
        ]
        ts = doc.get("ts")
        return {"todos": todos, "updated_at": ts.isoformat() if ts else None}

    @app.get("/messages")
    def messages(user_id: str, thread_id: str) -> dict[str, Any]:
        """Reconstruct the message list from the latest agent-log doc.

        ``thread_id`` is the per-conversation sub (same value passed to
        ``/chat``); the server composes the ``f"{user_id}:{sub}"`` key the
        writer used and orders by ``ts``. ``user_id`` scopes the read but is
        NOT authenticated — it is trust-on-input, so this is not a cross-user
        security boundary on its own; add edge auth if that matters. Missing
        threads get ``{"messages": []}`` rather than 404.
        """
        composite = f"{user_id}:{thread_id or 'default'}"
        cursor = get_db()[get_settings().agent_log_collection].find(
            {"thread_id": composite, "user_id": user_id},
            sort=[("ts", -1)],
            limit=1,
        )
        doc = next(iter(cursor), None)
        if doc is None:
            return {"messages": []}
        return {"messages": doc.get("messages", [])}

    @app.get("/threads/latest")
    def latest_thread(user_id: str) -> dict[str, Any]:
        """Return the sub of ``user_id``'s most recent conversation, or null.

        Reads the newest agent-log doc for this user (by ``ts`` desc) and strips
        the ``f"{user_id}:"`` prefix the writer composed onto ``thread_id``,
        yielding the per-conversation ``sub`` the frontend passes back to
        ``/chat`` and ``/messages`` to rehydrate the last chat on load. Like
        ``/messages``, ``user_id`` is trust-on-input, not an auth boundary.
        Unknown users get ``{"thread_id": null}`` rather than 404.
        """
        cursor = get_db()[get_settings().agent_log_collection].find(
            {"user_id": user_id},
            projection={"_id": 0, "thread_id": 1},
            sort=[("ts", -1)],
            limit=1,
        )
        doc = next(iter(cursor), None)
        if doc is None:
            return {"thread_id": None}
        # thread_id is always exactly f"{user_id}:{sub}"; slice by prefix length
        # (not split on ":") so user ids containing ":" still recover the sub.
        composite = str(doc.get("thread_id", ""))
        prefix = f"{user_id}:"
        sub = composite[len(prefix):] if composite.startswith(prefix) else composite
        return {"thread_id": sub or None}

    @app.get("/files")
    def files(user_id: str, thread_id: str) -> dict[str, Any]:
        """List the VFS files written in a conversation (retail demo).

        Proves the agent wrote to the S3 + MongoDB VFS — the "Files Saved"
        panel reads this. ``thread_id`` is the per-conversation sub; the server
        composes the same ``f"{user_id}:{sub}"`` key the VFS stored under
        (``vfs_files.thread_id`` is that composite). Always 200 with
        ``{"files": [{"path", "size", "created_at"}, ...]}``.
        """
        composite = f"{user_id}:{thread_id or 'default'}"
        cursor = get_db()[get_settings().vfs_files_collection].find(
            {"thread_id": composite},
            projection={"_id": 0, "path": 1, "size": 1, "created_at": 1},
            sort=[("created_at", 1)],
        )
        out = []
        for d in cursor:
            created = d.get("created_at")
            out.append(
                {
                    "path": d.get("path", ""),
                    "size": int(d.get("size", 0)),
                    "created_at": created.isoformat() if hasattr(created, "isoformat") else None,
                }
            )
        return {"files": out}

    @app.get("/cart")
    def cart(user_id: str, thread_id: str) -> dict[str, Any]:
        """Return the shopper's current cart.

        The Cart panel reads this after each turn. ``thread_id`` is the
        per-conversation sub; the cart is keyed by the natural
        ``(user_id, thread_id)`` (MongoDB owns the ObjectId _id) — the same key
        the cart tools write under. Always 200 with
        ``{"lines": [...], "subtotal", "total_savings", "updated_at"}``; an empty
        cart for an unknown thread.
        """
        from ..tools.cart import cart_key, cart_summary

        key = cart_key(user_id, thread_id or "default")
        doc = get_db()[get_settings().carts_collection].find_one(key)
        return cart_summary(doc)

    # ─── /chat ──────────────────────────────────────────────────

    @app.post("/chat")
    async def chat(req: ChatRequest) -> EventSourceResponse:
        from langchain_core.messages import AIMessage, HumanMessage

        if _SHUTDOWN_EVENT.is_set():
            raise HTTPException(status_code=503, detail="server is draining")

        cid = get_correlation_id()
        sub = req.thread_id or "default"
        thread_id = f"{req.user_id}:{sub}"
        s = get_settings()

        config: RunnableConfig = {
            # Pin the LangSmith root run_id to the correlation_id the frontend
            # round-trips, so the /feedback user_score mirror attaches to the
            # real trace (cid is a validated UUID from the middleware).
            "run_id": uuid.UUID(cid),
            "configurable": {
                "thread_id": thread_id,
                "user_id": req.user_id,
                "correlation_id": cid,
            },
            "recursion_limit": s.recursion_limit,
            "metadata": {
                "correlation_id": cid,
                "user_id": req.user_id,
                "thread_id": thread_id,
            },
            "tags": [f"correlation_id:{cid}"],
        }
        input_state: dict[str, Any] = {
            "messages": [HumanMessage(content=req.message)],
            "user_id": req.user_id,
        }
        timeout = s.chat_turn_timeout_s

        # The first request for a non-default model compiles a graph (Bedrock
        # init + create_deep_agent) — a blocking call. Offload it so it
        # doesn't stall every concurrent SSE stream / trip liveness probes.
        active_graph = await asyncio.to_thread(_graph_for, req.model)

        # Query-keyed response cache. ``build_response_cache`` returns None when
        # disabled / no Voyage key, in which case the block below is entirely
        # inert and /chat behaves exactly as before.
        response_cache = build_response_cache()
        cache_model = req.model or s.llm_model

        async def _is_fresh_conversation() -> bool:
            """True when the thread has no prior messages in its checkpoint —
            the only turns we cache (a conversation opener has no context to
            lose). Degrades to False (cache bypassed) if state can't be read."""
            try:
                state = await active_graph.aget_state(config)
            except Exception:
                log.warning(
                    "response-cache: aget_state failed for thread=%s cid=%s; bypassing",
                    thread_id,
                    cid,
                )
                return False
            prior = (getattr(state, "values", None) or {}).get("messages")
            return not prior

        async def _drive() -> AsyncGenerator[dict[str, Any], None]:
            # Emits inline `token` / `status` / `plan` frames via the shared
            # _map_graph_events driver; wraps it with the response cache
            # (fresh-conversation hit/miss) and the HITL `interrupt` frame.

            # On a fresh conversation, try the response cache first.
            # A hit streams the stored answer and skips the agent entirely;
            # we also persist the turn to the checkpoint so a follow-up in the
            # same thread runs the agent with coherent history.
            is_fresh = await _is_fresh_conversation() if response_cache is not None else False
            if response_cache is not None and is_fresh:
                hit: str | None = None
                try:
                    hit = response_cache.lookup(req.message, req.user_id, cache_model)
                except Exception:
                    log.warning("response-cache lookup failed cid=%s; bypassing", cid)
                if hit:
                    yield {"event": "token", "data": hit}
                    try:
                        await active_graph.aupdate_state(
                            config,
                            {
                                "messages": [
                                    HumanMessage(content=req.message),
                                    AIMessage(content=hit),
                                ]
                            },
                        )
                    except Exception:
                        log.warning(
                            "response-cache: aupdate_state failed cid=%s; "
                            "follow-up turns may lack this exchange",
                            cid,
                        )
                    return

            answer_parts: list[str] = []
            mutated = False
            async for frame in _map_graph_events(active_graph, input_state, config):
                if frame["event"] == "token":
                    answer_parts.append(frame["data"])
                elif frame["event"] == "status":
                    # A turn that invoked a side-effecting tool must NOT be
                    # response-cached — a cache replay streams the stored
                    # text WITHOUT re-running the tool, so the cart/order
                    # mutation silently doesn't happen on a repeat of the same
                    # query (a demo footgun for the cart/checkout presets).
                    try:
                        st = json.loads(frame["data"])
                        if st.get("phase") == "tool_start" and st.get("name") in _MUTATING_TOOLS:
                            mutated = True
                    except Exception:
                        pass
                yield frame

            # If the graph paused on a HITL tool (place_order), surface an
            # `interrupt` frame from the durable checkpoint so the UI can render
            # an approve/edit/reject card. An interrupted turn is incomplete, so
            # it is NOT response-cached. Only checked when HITL is enabled
            # (this keeps the non-HITL path byte-identical).
            if get_settings().hitl_tools.strip():
                interrupt_msg = await _interrupt_frame(active_graph, config, thread_id)
                if interrupt_msg is not None:
                    yield interrupt_msg
                    return

            # Clean completion of a fresh-conversation MISS → store the streamed
            # answer keyed by (query, user_id, model). Reached only on normal
            # exhaustion of the stream; a timeout / shutdown / error tears this
            # generator down before here, so a failed turn is never cached.
            if response_cache is not None and is_fresh and answer_parts and not mutated:
                try:
                    response_cache.save(
                        req.message, req.user_id, cache_model, "".join(answer_parts)
                    )
                except Exception:
                    log.warning("response-cache save failed cid=%s", cid)

        return _sse_response(_drive, cid, timeout)

    # ─── Feedback ───────────────────────────────────────────────

    @app.post("/feedback")
    def feedback(req: FeedbackRequest) -> dict[str, Any]:
        s = get_settings()
        doc = {
            "run_id": req.run_id,
            "score": req.score,
            "comment": req.comment,
            "user_id": req.user_id,
            "correlation_id": get_correlation_id(),
            "created_at": datetime.now(UTC),
        }
        try:
            get_db()[s.feedback_collection].insert_one(doc)
        except PyMongoError as exc:
            log.exception("failed to persist feedback")
            raise HTTPException(status_code=500, detail="failed to persist feedback") from exc

        if s.langsmith_tracing:
            try:
                from langsmith import Client

                Client().create_feedback(
                    run_id=req.run_id,
                    key="user_score",
                    score=req.score,
                    comment=req.comment,
                )
            except Exception:
                log.warning("LangSmith feedback mirror failed", exc_info=True)

        return {"ok": True}

    # ─── HITL endpoints (registered only when HITL_TOOLS non-empty) ─

    if get_settings().hitl_tools.strip():
        @app.get("/interrupts")
        def interrupts(thread_id: str) -> dict[str, Any]:  # pragma: no cover - opt-in
            """Inspect pending interrupts for a thread."""
            graph = _graph()
            try:
                state = graph.get_state(
                    {"configurable": {"thread_id": thread_id}}
                )
            except Exception as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            return {"thread_id": thread_id, "next": list(state.next or [])}

        @app.post("/interrupts/resume")
        async def resume_interrupt(
            req: Annotated[ResumeRequest, Body(...)],
        ) -> EventSourceResponse:
            """Resume an interrupted graph with an approve/edit/reject decision,
            STREAMING the resumed turn.

            Decision shapes (deepagents HumanInTheLoopMiddleware contract):
              - approve  → ``{"type": "approve"}``
              - reject   → ``{"type": "reject", "message"?: str}``
              - edit     → ``{"type": "edit", "edited_action": {"name", "args"}}``

            The resumed turn streams exactly like /chat (correlation → token →
            done) via the shared _sse_response machinery, so the post-approval
            order confirmation lands in the same assistant message. ``thread_id``
            is the composite ``f"{user_id}:{sub}"`` the interrupt frame echoed
            back; ``user_id`` is recovered from it so tools (place_order) scope
            correctly.
            """
            from langgraph.types import Command

            decision: dict[str, Any]
            if req.decision == "edit":
                if not req.edited_action or "name" not in req.edited_action:
                    raise HTTPException(
                        status_code=400,
                        detail="edit decisions require edited_action with 'name' and 'args'",
                    )
                decision = {"type": "edit", "edited_action": req.edited_action}
            elif req.decision == "reject":
                decision = {"type": "reject"}
                if req.message:
                    decision["message"] = req.message
            else:  # approve
                decision = {"type": "approve"}

            cid = get_correlation_id()
            thread_id = req.thread_id
            user_id = thread_id.split(":", 1)[0]
            s = get_settings()
            config: RunnableConfig = {
                # Pin run_id to the correlation_id (see /chat above).
                "run_id": uuid.UUID(cid),
                "configurable": {
                    "thread_id": thread_id,
                    "user_id": user_id,
                    "correlation_id": cid,
                },
                "recursion_limit": s.recursion_limit,
                "metadata": {
                    "correlation_id": cid,
                    "user_id": user_id,
                    "thread_id": thread_id,
                },
                "tags": [f"correlation_id:{cid}"],
            }
            active_graph = _graph()
            cmd: Any = Command(resume={"decisions": [decision]})

            async def _resume_drive() -> AsyncGenerator[dict[str, Any], None]:
                async for frame in _map_graph_events(active_graph, cmd, config):
                    yield frame
                # A resumed turn could pause on a further interrupt (rare);
                # surface it so the UI can prompt again rather than hang.
                follow_up = await _interrupt_frame(active_graph, config, thread_id)
                if follow_up is not None:
                    yield follow_up

            return _sse_response(_resume_drive, cid, s.chat_turn_timeout_s)

    return app


app: FastAPI | None = None


def get_asgi_app() -> FastAPI:  # pragma: no cover - uvicorn entrypoint
    global app
    if app is None:
        app = create_app()
    return app
