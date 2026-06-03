"""Patch dangling ``tool_use`` blocks before every model call.

Deepagents ships :class:`PatchToolCallsMiddleware` but it only runs on
``before_agent`` (once per invocation). On multi-hop runs — especially when a
subagent returns and the main planner resumes — an orphan ``tool_use`` block
can appear mid-conversation. Bedrock (Anthropic on AWS) hard-rejects these
with::

    messages.N: `tool_use` ids were found without `tool_result` blocks
    immediately after: toolu_bdrk_XXX. Each `tool_use` block must have a
    corresponding `tool_result` block in the next message.

This middleware hooks ``wrap_model_call`` so it can inspect and mutate the
EXACT message list the provider will see, after every upstream middleware has
had a chance to touch it. We:

1. Insert synthetic :class:`ToolMessage` blocks for any orphan ``tool_use`` id.
2. Reorder tool_result runs so their order matches the preceding AIMessage's
   tool_calls list (Bedrock's Anthropic validator requires same-order pairing).
"""
from __future__ import annotations

import logging
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage

log = logging.getLogger(__name__)


def _repair(messages: list[Any]) -> tuple[list[Any], int, int]:
    """Return (repaired_messages, num_patched, num_reordered).

    Anthropic's rule is stricter than "a matching ToolMessage exists somewhere
    later": every ``tool_use`` id must be answered by a ``tool_result`` in the
    run of ToolMessages that IMMEDIATELY follows the AIMessage. If a
    non-ToolMessage sits between the AIMessage and the eventual ToolMessage,
    Bedrock rejects with ``messages.N: tool_use ids were found without
    tool_result blocks immediately after``. We enforce that stricter
    contract here.
    """
    # Pass 1: ensure every tool_use id has a matching ToolMessage in the
    # immediate-after run.
    patched: list[Any] = []
    num_patched = 0
    for i, msg in enumerate(messages):
        patched.append(msg)
        tool_calls = getattr(msg, "tool_calls", None) if isinstance(msg, AIMessage) else None
        if not tool_calls:
            continue
        # Scan forward ONLY through the contiguous ToolMessage run.
        j = i + 1
        while j < len(messages) and isinstance(messages[j], ToolMessage):
            j += 1
        run_ids = {
            messages[k].tool_call_id for k in range(i + 1, j)
            if isinstance(messages[k], ToolMessage) and messages[k].tool_call_id
        }
        for tc in tool_calls:
            tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
            if not tc_id:
                continue
            if tc_id in run_ids:
                continue
            # Missing — splice a synthetic ToolMessage right after the AIMessage.
            # This ensures the immediate-after run now pairs every tool_use id.
            patched.append(
                ToolMessage(
                    content=(
                        f"Tool call {tc.get('name') if isinstance(tc, dict) else ''} "
                        f"with id {tc_id} was cancelled before it could complete."
                    ),
                    name=tc.get("name") if isinstance(tc, dict) else "",
                    tool_call_id=tc_id,
                )
            )
            num_patched += 1

    # Pass 2: reorder any tool_result runs to match the preceding AIMessage's
    # tool_call order.
    num_reordered = 0
    i = 0
    while i < len(patched):
        msg = patched[i]
        if isinstance(msg, AIMessage) and msg.tool_calls:
            expected_ids = [
                (tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None))
                for tc in msg.tool_calls
            ]
            expected_ids = [x for x in expected_ids if x]
            if len(expected_ids) > 1:
                j = i + 1
                run_start = j
                while j < len(patched) and isinstance(patched[j], ToolMessage):
                    j += 1
                run = patched[run_start:j]
                by_id = {t.tool_call_id: t for t in run if t.tool_call_id in expected_ids}
                ordered = [by_id[tid] for tid in expected_ids if tid in by_id]
                extras = [t for t in run if t.tool_call_id not in expected_ids]
                new_run = ordered + extras
                if [id(t) for t in run] != [id(t) for t in new_run]:
                    patched[run_start:j] = new_run
                    num_reordered += 1
                i = j
            else:
                i += 1
        else:
            i += 1

    return patched, num_patched, num_reordered


class PatchDanglingToolCallsMiddleware(AgentMiddleware):
    """Repair message history at the model-call boundary.

    Unlike ``before_model`` (which modifies the *graph* state), ``wrap_model_call``
    lets us rewrite the request right before it hits the provider, so upstream
    middleware (filesystem, summarization, prompt-caching) cannot re-introduce
    orphan tool_use blocks between our fix and the wire.
    """

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        repaired, patched_n, reordered_n = _repair(request.messages)
        if patched_n or reordered_n:
            log.info(
                "patch_dangling: in=%d patched=%d reordered=%d",
                len(request.messages), patched_n, reordered_n,
            )
            request = request.override(messages=repaired)
        return handler(request)

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        repaired, patched_n, reordered_n = _repair(request.messages)
        if patched_n or reordered_n:
            log.info(
                "patch_dangling: in=%d patched=%d reordered=%d",
                len(request.messages), patched_n, reordered_n,
            )
            request = request.override(messages=repaired)
        return await handler(request)
