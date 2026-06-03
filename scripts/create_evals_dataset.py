"""Upload an eval dataset to LangSmith.

Usage::

    # Default: the Agent Cartsmith retail showcase set
    uv run python scripts/create_evals_dataset.py

    # The generic deep-agent smoke set
    uv run python scripts/create_evals_dataset.py --name deep_agent_starter \
        --fixture tests/fixtures/evals_dataset.jsonl

The script is idempotent: if a dataset with the given name already exists, it
appends any new examples (matched by `message`). Rows may carry an optional
``expected_tools`` list, uploaded into the example outputs for the
``tool_trajectory`` evaluator. After it runs,
``deep-agent-evals --dataset agent-cartsmith-retail-demo`` resolves to the
showcase dataset in your workspace.

Requires ``LANGSMITH_API_KEY`` to be set (ambient or in ``.env``).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
DEFAULT_FIXTURE = _FIXTURES / "retail_evals.jsonl"
DEFAULT_NAME = "agent-cartsmith-retail-demo"


def _read_fixture(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def upload(*, name: str, description: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Create or top-up a LangSmith dataset. Returns a summary dict."""
    from langsmith import Client

    client = Client()

    try:
        dataset = client.read_dataset(dataset_name=name)
    except Exception:
        dataset = client.create_dataset(dataset_name=name, description=description)

    # Dedupe on message — so re-running the script does not create duplicates.
    existing_messages: set[str] = set()
    try:
        for ex in client.list_examples(dataset_id=dataset.id):
            msg = (ex.inputs or {}).get("message") or (ex.inputs or {}).get("question")
            if msg:
                existing_messages.add(msg)
    except Exception:
        pass

    created = 0
    for row in rows:
        msg = row["message"]
        if msg in existing_messages:
            continue
        outputs: dict[str, Any] = {"answer": row["answer"]}
        # Optional trajectory metadata consumed by the tool_trajectory evaluator.
        if row.get("expected_tools"):
            outputs["expected_tools"] = row["expected_tools"]
        client.create_example(
            inputs={"message": msg},
            outputs=outputs,
            dataset_id=dataset.id,
        )
        created += 1

    return {
        "dataset_id": str(dataset.id),
        "dataset_name": name,
        "already_present": len(rows) - created,
        "created": created,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="create_evals_dataset")
    p.add_argument("--name", default=DEFAULT_NAME, help="LangSmith dataset name")
    p.add_argument("--description", default="Agent Cartsmith retail Q&A eval set.")
    p.add_argument(
        "--fixture",
        type=Path,
        default=DEFAULT_FIXTURE,
        help="Path to a JSONL file of {message, answer, expected_tools?} rows",
    )
    args = p.parse_args(argv)

    if not args.fixture.exists():
        print(f"fixture not found: {args.fixture}", file=sys.stderr)
        return 2

    rows = _read_fixture(args.fixture)
    if not rows:
        print(f"fixture is empty: {args.fixture}", file=sys.stderr)
        return 2

    summary = upload(name=args.name, description=args.description, rows=rows)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
