import json

import pytest

from app.evaluation.contracts import EvaluationConfigError, load_suite


def _case(**changes):
    value = {
        "id": "direct-answer",
        "input": "你好",
        "replay_steps": [{"output_text": "你好"}],
        "expected": {"output_contains": ["你好"]},
    }
    value.update(changes)
    return value


def test_load_suite_parses_replay_case(tmp_path):
    path = tmp_path / "core.jsonl"
    path.write_text(json.dumps(_case(), ensure_ascii=False) + "\n", encoding="utf-8")

    suite = load_suite(path)

    assert suite.name == "core"
    assert suite.cases[0].replay_turns[0][0].output_text == "你好"


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"unknown": True}, "unknown field"),
        ({"setup": {"files": {"../secret": "x"}}}, "path is unsafe"),
        ({"setup": {"files": {"AGENTS.md": "覆盖"}}}, "path is unsafe"),
        ({"setup": {"files": {"dir\\file.txt": "x"}}}, "path is unsafe"),
        ({"setup": {"files": {"bad\nname.txt": "x"}}}, "path is unsafe"),
        ({"setup": {"messages": [{"role": "user", "content": "x"}]}}, "complete alternating"),
        ({"expected": {"required_tools": ["x"], "forbidden_tools": ["x"]}}, "overlap"),
        ({"tags": ["bad tag"]}, "tag is invalid"),
        ({"expected": {"status": "success", "error_code": "boom"}}, "cannot expect an error code"),
    ],
)
def test_load_suite_rejects_invalid_contract(tmp_path, changes, message):
    path = tmp_path / "core.jsonl"
    path.write_text(json.dumps(_case(**changes)) + "\n", encoding="utf-8")

    with pytest.raises(EvaluationConfigError, match=message):
        load_suite(path)


def test_load_suite_rejects_duplicate_case_ids(tmp_path):
    path = tmp_path / "core.jsonl"
    line = json.dumps(_case())
    path.write_text(f"{line}\n{line}\n", encoding="utf-8")

    with pytest.raises(EvaluationConfigError, match="duplicate case id"):
        load_suite(path)


def test_load_suite_parses_visible_tool_expectations(tmp_path):
    path = tmp_path / "core.jsonl"
    path.write_text(
        json.dumps(
            _case(
                expected={
                    "visible_tools": [
                        {"step": 1, "contains": ["activate_tool_groups"], "excludes": ["read_workspace_file"]}
                    ]
                }
            )
        ) + "\n",
        encoding="utf-8",
    )

    item = load_suite(path).cases[0].expected.visible_tools[0]

    assert item.step == 1
    assert item.excludes == ("read_workspace_file",)


def test_load_suite_allows_empty_file_content(tmp_path):
    path = tmp_path / "core.jsonl"
    path.write_text(
        json.dumps(_case(setup={"files": {"empty.txt": ""}}, expected={"files": [{"path": "empty.txt", "content": ""}]})) + "\n",
        encoding="utf-8",
    )

    case = load_suite(path).cases[0]

    assert case.setup.files == (("empty.txt", ""),)
    assert case.expected.files[0].content == ""


def test_load_suite_rejects_duplicate_tool_call_ids(tmp_path):
    path = tmp_path / "core.jsonl"
    path.write_text(
        json.dumps(
            _case(
                replay_steps=[
                    {"tool_calls": [{"call_id": "same", "name": "get_current_time", "arguments": {}}]},
                    {"tool_calls": [{"call_id": "same", "name": "get_current_time", "arguments": {}}]},
                ]
            )
        ) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(EvaluationConfigError, match="call ids contain duplicates"):
        load_suite(path)


def test_load_suite_rejects_oversized_suite(tmp_path, monkeypatch):
    from app.evaluation import contracts

    path = tmp_path / "core.jsonl"
    path.write_bytes(b"{}\n{}\n")
    monkeypatch.setattr(contracts, "MAX_SUITE_BYTES", 3)

    with pytest.raises(EvaluationConfigError, match="suite is too large"):
        load_suite(path)
