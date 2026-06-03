"""Sub-phase 18: evals starter dataset + uploader."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "evals_dataset.jsonl"
)
RETAIL_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "retail_evals.jsonl"
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_TC_18_010_fixture_is_valid_jsonl() -> None:
    """Every line parses as JSON with `message` + `answer` keys."""
    assert FIXTURE.exists()
    count = 0
    with FIXTURE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            assert "message" in row and "answer" in row
            assert isinstance(row["message"], str) and row["message"]
            assert isinstance(row["answer"], str) and row["answer"]
            count += 1
    assert count >= 5, "starter dataset should have at least 5 rows"


def test_TC_540_C05_retail_fixture_grown_and_valid() -> None:
    """The retail showcase set has >=12 rows; every row is valid
    {message, answer}; any expected_tools is a list of non-empty strings."""
    assert RETAIL_FIXTURE.exists()
    rows = []
    with RETAIL_FIXTURE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            assert isinstance(row.get("message"), str) and row["message"]
            assert isinstance(row.get("answer"), str) and row["answer"]
            if "expected_tools" in row:
                assert isinstance(row["expected_tools"], list)
                assert all(isinstance(t, str) and t for t in row["expected_tools"])
            rows.append(row)
    assert len(rows) >= 12, "retail showcase dataset should have at least 12 rows"
    # At least some rows exercise the trajectory evaluator.
    assert any("expected_tools" in r for r in rows)


def test_TC_540_C05_uploader_passes_expected_tools() -> None:
    """expected_tools rides into the example outputs."""
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from scripts.create_evals_dataset import upload
    finally:
        sys.path.pop(0)

    fake_client = MagicMock()
    fake_client.read_dataset.side_effect = Exception("not found")
    fake_client.create_dataset.return_value = MagicMock(id="ds-1")
    fake_client.list_examples.return_value = iter([])

    rows = [
        {"message": "m1", "answer": "a1", "expected_tools": ["view_cart"]},
        {"message": "m2", "answer": "a2"},  # no expected_tools
    ]
    with patch("langsmith.Client", return_value=fake_client):
        upload(name="retail", description="d", rows=rows)

    calls = fake_client.create_example.call_args_list
    by_msg = {c.kwargs["inputs"]["message"]: c.kwargs["outputs"] for c in calls}
    assert by_msg["m1"] == {"answer": "a1", "expected_tools": ["view_cart"]}
    assert by_msg["m2"] == {"answer": "a2"}  # no expected_tools key when absent


def test_TC_18_020_uploader_creates_dataset_when_missing() -> None:
    """When `read_dataset` fails, the script creates a new dataset and uploads rows."""
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from scripts.create_evals_dataset import upload
    finally:
        sys.path.pop(0)

    fake_client = MagicMock()
    fake_client.read_dataset.side_effect = Exception("not found")
    fake_dataset = MagicMock(id="ds-123")
    fake_client.create_dataset.return_value = fake_dataset
    fake_client.list_examples.return_value = iter([])

    rows = [
        {"message": "m1", "answer": "a1"},
        {"message": "m2", "answer": "a2"},
    ]

    with patch("langsmith.Client", return_value=fake_client):
        summary = upload(name="deep_agent_starter", description="desc", rows=rows)

    fake_client.create_dataset.assert_called_once()
    assert fake_client.create_example.call_count == 2
    assert summary["created"] == 2
    assert summary["already_present"] == 0


def test_TC_18_021_uploader_dedupes_on_message() -> None:
    """Existing examples (matched on `message`) must not be re-uploaded."""
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from scripts.create_evals_dataset import upload
    finally:
        sys.path.pop(0)

    fake_client = MagicMock()
    fake_dataset = MagicMock(id="ds-xyz")
    fake_client.read_dataset.return_value = fake_dataset
    existing = MagicMock()
    existing.inputs = {"message": "m1"}
    fake_client.list_examples.return_value = iter([existing])

    rows = [
        {"message": "m1", "answer": "a1"},  # already present
        {"message": "m2", "answer": "a2"},  # new
    ]

    with patch("langsmith.Client", return_value=fake_client):
        summary = upload(name="deep_agent_starter", description="desc", rows=rows)

    fake_client.create_dataset.assert_not_called()
    assert fake_client.create_example.call_count == 1
    assert summary["created"] == 1
    assert summary["already_present"] == 1


def test_TC_18_030_main_parses_args_and_prints_summary(
    capsys,  # type: ignore[no-untyped-def]
) -> None:
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from scripts.create_evals_dataset import main
    finally:
        sys.path.pop(0)

    with patch("scripts.create_evals_dataset.upload") as up:
        up.return_value = {
            "dataset_id": "ds-1",
            "dataset_name": "deep_agent_starter",
            "already_present": 0,
            "created": 8,
        }
        rc = main(["--name", "deep_agent_starter", "--fixture", str(FIXTURE)])

    out = capsys.readouterr().out
    assert rc == 0
    assert "deep_agent_starter" in out
    assert "\"created\": 8" in out
