"""Command-line entrypoint.

Subcommands:
- ``chat``  — REPL or one-shot invocation against the compiled deep-agent graph.
- ``seed``  — run the seed loader.
- ``serve`` — boot the FastAPI application via uvicorn.

Single-domain reference. The ``--domain`` flag and per-pack
plumbing are gone; vertical apps fork the repo to ship their own domain.
"""
from __future__ import annotations

import argparse
import logging
import sys

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from .graph import build_graph

log = logging.getLogger(__name__)


def _run_chat(args: argparse.Namespace) -> int:
    graph = build_graph()
    config: RunnableConfig = {
        "configurable": {
            "thread_id": args.thread,
            "user_id": args.user,
        }
    }

    def invoke(message: str) -> str:
        # AgentLogMiddleware (langchain-mongodb-agent-log)
        # writes one message-log doc per super-step into the agent_log
        # collection. No separate chat_history append needed.
        state = graph.invoke(
            {
                "messages": [HumanMessage(content=message)],
                "user_id": args.user,
            },
            config=config,
        )
        msgs = state.get("messages", []) if isinstance(state, dict) else []
        answer = ""
        for m in reversed(msgs):
            if isinstance(m, AIMessage):
                answer = str(m.content)
                break
        return answer

    if args.once is not None:
        print(invoke(args.once))
        return 0

    print(f"deep-agent chat (user={args.user}, thread={args.thread}) — Ctrl-D to exit")
    while True:
        try:
            line = input("> ").strip()
        except EOFError:
            print()
            return 0
        if not line:
            continue
        if line in {"/quit", "/exit"}:
            return 0
        print(invoke(line))


def _run_seed(_: argparse.Namespace) -> int:
    from .ingestion.seed import SeedIncompleteError, seed_all

    try:
        summary = seed_all()
    except SeedIncompleteError as exc:
        print(f"seed failed: {exc}", file=sys.stderr)
        return 1
    print(summary)
    return 0


def _run_serve(args: argparse.Namespace) -> int:  # pragma: no cover - e2e only
    import uvicorn

    uvicorn.run(
        "deep_agent.server.app:get_asgi_app",
        host=args.host,
        port=args.port,
        factory=True,
        reload=args.reload,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="deep-agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    chat = sub.add_parser("chat", help="interactive REPL or --once invocation")
    chat.add_argument("--once", type=str, default=None, help="run a single prompt and exit")
    chat.add_argument("--user", type=str, default="demo-user", help="user_id for memory scoping")
    chat.add_argument("--thread", type=str, default="cli-default", help="thread_id (checkpoint key)")
    chat.set_defaults(func=_run_chat)

    seed = sub.add_parser("seed", help="load seeds into Atlas")
    seed.set_defaults(func=_run_seed)

    serve = sub.add_parser("serve", help="boot the FastAPI app")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=_run_serve)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
