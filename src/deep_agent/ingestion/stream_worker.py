"""Change-stream worker.

Watches the ``stream_events`` collection and pipes each new document into the
``knowledge_base`` vector store so freshly ingested events become searchable
in near-real time.

Resilience:
- Persists a resume token so restarts pick up mid-stream.
- Wraps the watcher in :func:`run_with_backoff` — exponential backoff capped at 60 s.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from pymongo.errors import PyMongoError

from ..config import get_settings
from ..persistence.mongo import get_db
from ..persistence.vector_store import build_vector_store

log = logging.getLogger(__name__)


def resume_token_path() -> Path:
    """Resolve the resume-token file location.

    Precedence:
      1. ``DEEP_AGENT_STATE_DIR``  — explicit override
      2. ``XDG_STATE_HOME/deep_agent`` — per the XDG Base Directory Spec
      3. ``~/.deep_agent``         — final fallback
    """
    if override := os.environ.get("DEEP_AGENT_STATE_DIR"):
        base = Path(override)
    elif xdg := os.environ.get("XDG_STATE_HOME"):
        base = Path(xdg) / "deep_agent"
    else:
        base = Path.home() / ".deep_agent"
    return base / "stream_resume_token.json"


def _load_resume_token() -> dict[str, Any] | None:
    path = resume_token_path()
    if not path.exists():
        return None
    try:
        token: dict[str, Any] = json.loads(path.read_text())
        return token
    except (OSError, ValueError):
        return None


def _save_resume_token(token: dict[str, Any]) -> None:
    try:
        path = resume_token_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(token))
    except OSError as exc:
        log.warning("failed to persist resume token: %s", exc)


def run_once(
    *,
    max_events: int | None = None,
    resume_after: dict[str, Any] | None = None,
) -> int:
    """Consume the change stream, upserting each event into the KB.

    Returns the number of events processed.
    """
    s = get_settings()
    db = get_db()
    coll = db[s.stream_events_collection]
    vs = build_vector_store()

    processed = 0
    watch_kwargs: dict[str, Any] = {}
    if resume_after is not None:
        watch_kwargs["resume_after"] = resume_after

    with coll.watch(**watch_kwargs) as stream:
        for event in stream:
            op = event.get("operationType")
            if op not in ("insert", "update", "replace"):
                continue
            doc = event.get("fullDocument") or {}
            # The shipped ASP $group emits {_id, count, last} merged on _id, so
            # the payload lives at ``last.text``. Prefer that
            # windowed payload; fall back to a flat top-level ``text`` for
            # non-windowed (direct-insert) sources.
            last = doc.get("last")
            payload = last if isinstance(last, dict) and "text" in last else doc
            text = payload.get("text")
            if not text:
                continue
            metadata = {k: v for k, v in payload.items() if k != "text"}
            vs.add_texts(texts=[text], metadatas=[metadata])
            processed += 1
            token = event.get("_id")
            if token is not None:
                _save_resume_token(token)
            if max_events is not None and processed >= max_events:
                break
    return processed


def run_with_backoff(
    *,
    max_attempts: int | None = None,
    base_delay: float = 1.0,
    cap: float = 60.0,
) -> None:
    """Run the worker, reconnecting with exponential backoff on :class:`PyMongoError`."""
    attempt = 0
    failures = 0
    while True:
        attempt += 1
        token = _load_resume_token()
        try:
            run_once(resume_after=token)
            failures = 0
        except PyMongoError as exc:
            failures += 1
            log.warning("stream worker error (attempt %d): %s", attempt, exc)
            if max_attempts is not None and attempt >= max_attempts:
                return
            delay = min(base_delay * (2 ** (failures - 1)), cap)
            time.sleep(delay)
            continue
        if max_attempts is not None and attempt >= max_attempts:
            return


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_with_backoff()
