import asyncio
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from tools.contracts import ToolCall, ToolExecutionContext
from tools.project_checks import RunProjectCheckTool
from tools.registry import ToolRegistry
from tools.workspace import (
    ApplyWorkspaceEditsTool,
    GetWorkspaceGitDiffTool,
    GetWorkspaceGitStatusTool,
    ListWorkspaceFilesTool,
    ReadWorkspaceFileTool,
    SearchWorkspaceTextTool,
    UndoWorkspaceChangeTool,
    WorkspaceChangeJournal,
    WorkspacePolicy,
)
from tools import project_checks
from tools import workspace as workspace_module


def execute(tool, arguments, approval=None):
    async def handler(request):
        if approval is not None:
            approval.append(request)
        return approval is not None

    context = ToolExecutionContext(
        approval_handler=handler if approval is not None else None
    )
    result = asyncio.run(
        ToolRegistry([tool]).execute(
            ToolCall("call-1", tool.definition.name, json.dumps(arguments)),
            context,
        )
    )
    return json.loads(result.output), result


def test_policy_rejects_escape_symlink_and_protected_paths(tmp_path):
    (tmp_path / "safe").mkdir()
    (tmp_path / "safe" / "a.txt").write_text("ok", encoding="utf-8")
    (tmp_path / ".env").write_text("secret", encoding="utf-8")
    (tmp_path / "link").symlink_to(tmp_path / "safe", target_is_directory=True)
    policy = WorkspacePolicy(tmp_path)

    assert policy.resolve_read_file("safe/a.txt") == tmp_path / "safe" / "a.txt"
    for path in ("../outside", "/tmp/outside", ".env", "link/a.txt"):
        with pytest.raises(ValueError):
            policy.resolve_read_file(path)


@pytest.mark.parametrize(
    "path",
    (
        ".env.local",
        ".venv/bin/python",
        "data/session.json",
        "logs/model.log",
        "pkg/__pycache__/module.pyc",
        "docs/.pytest_cache/item",
    ),
)
def test_policy_rejects_every_protected_read_family(tmp_path, path):
    target = tmp_path.joinpath(*Path(path).parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("secret", encoding="utf-8")

    with pytest.raises(ValueError):
        WorkspacePolicy(tmp_path).resolve_read_file(path)


def test_list_search_and_read_support_chinese_and_hash(tmp_path):
    (tmp_path / "docs").mkdir()
    content = "第一行\n包含中文关键字\n第三行\n"
    (tmp_path / "docs" / "说明.md").write_text(content, encoding="utf-8")
    policy = WorkspacePolicy(tmp_path)

    listed, _ = execute(ListWorkspaceFilesTool(policy), {"path": "."})
    searched, _ = execute(SearchWorkspaceTextTool(policy), {"query": "中文"})
    read, _ = execute(
        ReadWorkspaceFileTool(policy),
        {"path": "docs/说明.md", "start_line": 2, "max_lines": 1},
    )

    assert {item["path"] for item in listed["data"]["items"]} >= {
        "docs",
        "docs/说明.md",
    }
    assert searched["data"]["matches"][0]["path"] == "docs/说明.md"
    assert read["data"]["content"] == "包含中文关键字\n"
    assert read["data"]["sha256"] == hashlib.sha256(content.encode()).hexdigest()


def test_read_rejects_binary_and_list_prunes_protected_directory(tmp_path):
    (tmp_path / "binary.bin").write_bytes(b"a\x00b")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("hidden", encoding="utf-8")
    policy = WorkspacePolicy(tmp_path)

    output, result = execute(ReadWorkspaceFileTool(policy), {"path": "binary.bin"})
    listed, _ = execute(ListWorkspaceFilesTool(policy), {})

    assert result.is_error is True
    assert output["error"]["code"] == "invalid_arguments"
    assert all(not item["path"].startswith(".git") for item in listed["data"]["items"])


def test_list_and_search_pages_stay_below_registry_result_limit(tmp_path):
    folder = tmp_path / "many"
    folder.mkdir()
    for index in range(200):
        name = f"{index:03d}-" + "长" * 60 + ".txt"
        (folder / name).write_text("匹配" + "文" * 600, encoding="utf-8")
    policy = WorkspacePolicy(tmp_path)

    listed, _ = execute(ListWorkspaceFilesTool(policy), {"limit": 200})
    searched, _ = execute(
        SearchWorkspaceTextTool(policy),
        {"query": "匹配", "limit": 100},
    )

    assert len(json.dumps(listed, ensure_ascii=False).encode("utf-8")) < 32 * 1024
    assert len(json.dumps(searched, ensure_ascii=False).encode("utf-8")) < 32 * 1024
    assert listed["data"]["next_cursor"] is not None
    assert searched["data"]["next_cursor"] is not None


def test_apply_replace_preview_approval_and_lifo_undo(tmp_path):
    target = tmp_path / "demo.txt"
    target.write_text("旧内容\n", encoding="utf-8")
    policy = WorkspacePolicy(tmp_path)
    journal = WorkspaceChangeJournal()
    apply_tool = ApplyWorkspaceEditsTool(policy, journal)
    approvals = []
    digest = hashlib.sha256(target.read_bytes()).hexdigest()

    applied, result = execute(
        apply_tool,
        {
            "edits": [
                {
                    "mode": "replace",
                    "path": "demo.txt",
                    "expected_sha256": digest,
                    "old_text": "旧内容",
                    "new_text": "新内容",
                }
            ]
        },
        approvals,
    )

    assert result.is_error is False
    assert target.read_text(encoding="utf-8") == "新内容\n"
    assert "-旧内容" in approvals[0].diff_text
    assert "+新内容" in approvals[0].diff_text

    undo_approvals = []
    undone, undo_result = execute(
        UndoWorkspaceChangeTool(policy, journal),
        {"change_id": applied["data"]["change_id"]},
        undo_approvals,
    )
    assert undo_result.is_error is False
    assert undone["data"]["undone"] is True
    assert target.read_text(encoding="utf-8") == "旧内容\n"


def test_apply_create_then_undo_removes_created_file(tmp_path):
    policy = WorkspacePolicy(tmp_path)
    journal = WorkspaceChangeJournal()
    approvals = []
    created = tmp_path / "新文件.txt"

    output, result = execute(
        ApplyWorkspaceEditsTool(policy, journal),
        {"edits": [{"mode": "create", "path": "新文件.txt", "content": "你好\n"}]},
        approvals,
    )
    assert result.is_error is False
    assert created.read_text(encoding="utf-8") == "你好\n"

    _, undo_result = execute(
        UndoWorkspaceChangeTool(policy, journal),
        {"change_id": output["data"]["change_id"]},
        [],
    )
    assert undo_result.is_error is False
    assert not created.exists()


def test_apply_detects_change_during_approval_without_overwrite(tmp_path):
    target = tmp_path / "race.txt"
    target.write_text("before", encoding="utf-8")
    policy = WorkspacePolicy(tmp_path)
    tool = ApplyWorkspaceEditsTool(policy, WorkspaceChangeJournal())
    arguments = {
        "edits": [{
            "mode": "replace",
            "path": "race.txt",
            "expected_sha256": hashlib.sha256(b"before").hexdigest(),
            "old_text": "before",
            "new_text": "agent",
        }]
    }

    async def scenario():
        async def approve(_request):
            target.write_text("user", encoding="utf-8")
            return True

        return await ToolRegistry([tool]).execute(
            ToolCall("race", tool.definition.name, json.dumps(arguments)),
            ToolExecutionContext(approval_handler=approve),
        )

    result = asyncio.run(scenario())

    assert result.is_error is True
    assert json.loads(result.output)["error"]["code"] == "workspace_conflict"
    assert target.read_text(encoding="utf-8") == "user"


def test_apply_detects_create_target_appearing_during_approval(tmp_path):
    target = tmp_path / "new.txt"
    tool = ApplyWorkspaceEditsTool(
        WorkspacePolicy(tmp_path),
        WorkspaceChangeJournal(),
    )

    async def scenario():
        async def approve(_request):
            target.write_text("user", encoding="utf-8")
            return True

        return await ToolRegistry([tool]).execute(
            ToolCall(
                "race-create",
                tool.definition.name,
                '{"edits":[{"mode":"create","path":"new.txt","content":"agent"}]}',
            ),
            ToolExecutionContext(approval_handler=approve),
        )

    result = asyncio.run(scenario())

    assert result.is_error is True
    assert json.loads(result.output)["error"]["code"] == "workspace_conflict"
    assert target.read_text(encoding="utf-8") == "user"


def test_apply_batch_failure_restores_already_replaced_file(tmp_path, monkeypatch):
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_text("old-a", encoding="utf-8")
    second.write_text("old-b", encoding="utf-8")
    policy = WorkspacePolicy(tmp_path)
    real_write = workspace_module._write_atomic
    failed = False

    def fail_second_once(path, content, mode):
        nonlocal failed
        if path == second and not failed:
            failed = True
            raise OSError("simulated write failure")
        real_write(path, content, mode)

    monkeypatch.setattr(workspace_module, "_write_atomic", fail_second_once)
    edits = []
    for path, old, new in ((first, "old-a", "new-a"), (second, "old-b", "new-b")):
        edits.append({
            "mode": "replace",
            "path": path.name,
            "expected_sha256": hashlib.sha256(old.encode()).hexdigest(),
            "old_text": old,
            "new_text": new,
        })

    output, result = execute(
        ApplyWorkspaceEditsTool(policy, WorkspaceChangeJournal()),
        {"edits": edits},
        [],
    )

    assert result.is_error is True
    assert output["error"]["code"] == "execution_failed"
    assert first.read_text(encoding="utf-8") == "old-a"
    assert second.read_text(encoding="utf-8") == "old-b"


def test_apply_accepts_ten_unique_files_and_rejects_eleventh(tmp_path):
    policy = WorkspacePolicy(tmp_path)
    ten_edits = [
        {"mode": "create", "path": f"file-{index}.txt", "content": str(index)}
        for index in range(10)
    ]
    approvals = []

    output, result = execute(
        ApplyWorkspaceEditsTool(policy, WorkspaceChangeJournal()),
        {"edits": ten_edits},
        approvals,
    )
    assert result.is_error is False
    assert len(output["data"]["paths"]) == 10
    assert approvals[0].paths == tuple(sorted(output["data"]["paths"]))

    too_many, too_many_result = execute(
        ApplyWorkspaceEditsTool(policy, WorkspaceChangeJournal()),
        {
            "edits": [
                {"mode": "create", "path": f"extra-{index}.txt", "content": "x"}
                for index in range(11)
            ]
        },
        [],
    )
    assert too_many_result.is_error is True
    assert too_many["error"]["code"] == "invalid_arguments"

    duplicate, duplicate_result = execute(
        ApplyWorkspaceEditsTool(policy, WorkspaceChangeJournal()),
        {
            "edits": [
                {"mode": "create", "path": "duplicate.txt", "content": "a"},
                {"mode": "create", "path": "duplicate.txt", "content": "b"},
            ]
        },
        [],
    )
    assert duplicate_result.is_error is True
    assert duplicate["error"]["code"] == "invalid_arguments"


@pytest.mark.parametrize("old_text", ("missing", "same"))
def test_apply_rejects_zero_or_multiple_exact_matches(tmp_path, old_text):
    target = tmp_path / "repeat.txt"
    target.write_text("same same", encoding="utf-8")
    output, result = execute(
        ApplyWorkspaceEditsTool(
            WorkspacePolicy(tmp_path),
            WorkspaceChangeJournal(),
        ),
        {
            "edits": [{
                "mode": "replace",
                "path": "repeat.txt",
                "expected_sha256": hashlib.sha256(b"same same").hexdigest(),
                "old_text": old_text,
                "new_text": "new",
            }]
        },
        [],
    )

    assert result.is_error is True
    assert output["error"]["code"] == "workspace_conflict"
    assert target.read_text(encoding="utf-8") == "same same"


def test_undo_rejects_user_change_and_preserves_file(tmp_path):
    target = tmp_path / "demo.txt"
    target.write_text("old", encoding="utf-8")
    policy = WorkspacePolicy(tmp_path)
    journal = WorkspaceChangeJournal()
    applied, _ = execute(
        ApplyWorkspaceEditsTool(policy, journal),
        {"edits": [{
            "mode": "replace",
            "path": "demo.txt",
            "expected_sha256": hashlib.sha256(b"old").hexdigest(),
            "old_text": "old",
            "new_text": "agent",
        }]},
        [],
    )
    target.write_text("user", encoding="utf-8")

    output, result = execute(
        UndoWorkspaceChangeTool(policy, journal),
        {"change_id": applied["data"]["change_id"]},
        [],
    )

    assert result.is_error is True
    assert output["error"]["code"] == "workspace_conflict"
    assert target.read_text(encoding="utf-8") == "user"


def test_apply_rejects_without_approval_conflict_and_protected_target(tmp_path):
    target = tmp_path / "demo.txt"
    target.write_text("old", encoding="utf-8")
    policy = WorkspacePolicy(tmp_path)
    journal = WorkspaceChangeJournal()
    tool = ApplyWorkspaceEditsTool(policy, journal)

    denied, denied_result = execute(
        tool,
        {
            "edits": [
                {
                    "mode": "replace",
                    "path": "demo.txt",
                    "expected_sha256": "0" * 64,
                    "old_text": "old",
                    "new_text": "new",
                }
            ]
        },
        [],
    )
    assert denied_result.is_error is True
    assert denied["error"]["code"] == "workspace_conflict"
    assert target.read_text() == "old"

    protected, protected_result = execute(
        tool,
        {"edits": [{"mode": "create", "path": "AGENTS.md", "content": "x"}]},
        [],
    )
    assert protected_result.is_error is True
    assert protected["error"]["code"] == "protected_path"

    runtime_protected, runtime_result = execute(
        tool,
        {"edits": [{"mode": "create", "path": "app/runtime/tool_loop.py", "content": "x"}]},
        [],
    )
    assert runtime_result.is_error is True
    assert runtime_protected["error"]["code"] == "protected_path"


def test_git_status_and_diff_return_relative_repository_data(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=t@example.com", "commit", "-qm", "init"],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / "a.txt").write_text("two\n", encoding="utf-8")
    policy = WorkspacePolicy(tmp_path)

    status, _ = execute(GetWorkspaceGitStatusTool(policy), {})
    diff, _ = execute(GetWorkspaceGitDiffTool(policy), {})

    assert "a.txt" in status["data"]["output"]
    assert "+two" in diff["data"]["content"]
    assert str(tmp_path) not in json.dumps([status, diff])


def test_project_check_uses_fixed_command_and_reports_nonzero(tmp_path):
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    python = tmp_path / ".venv" / "bin" / "python"
    python.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    python.chmod(0o755)
    policy = WorkspacePolicy(tmp_path)

    output, result = execute(RunProjectCheckTool(policy, timeout_seconds=2), {"name": "pip_check"})

    assert result.is_error is False
    assert output["data"]["exit_code"] == 3
    assert output["data"]["name"] == "pip_check"


def test_project_check_accepts_standard_venv_python_symlink(tmp_path):
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    interpreter = tmp_path / "python-real"
    interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    interpreter.chmod(0o755)
    (tmp_path / ".venv" / "bin" / "python").symlink_to(interpreter)

    output, result = execute(
        RunProjectCheckTool(WorkspacePolicy(tmp_path), timeout_seconds=2),
        {"name": "pip_check"},
    )

    assert result.is_error is False
    assert output["data"]["exit_code"] == 0


def test_project_check_uses_fixed_argv_cwd_and_minimal_environment(tmp_path, monkeypatch):
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    interpreter = tmp_path / "python-real"
    interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    interpreter.chmod(0o755)
    (tmp_path / ".venv" / "bin" / "python").symlink_to(interpreter)
    captured = {}

    class Stream:
        async def read(self, _size):
            return b""

    class Process:
        stdout = Stream()
        returncode = 0

        async def wait(self):
            return 0

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return Process()

    monkeypatch.setattr(project_checks.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-leak")
    output, result = execute(
        RunProjectCheckTool(WorkspacePolicy(tmp_path)),
        {"name": "test_all"},
    )

    assert result.is_error is False
    assert output["data"]["exit_code"] == 0
    assert captured["argv"] == (str(interpreter), "-m", "pytest", "-q")
    assert captured["kwargs"]["cwd"] == tmp_path.resolve()
    assert captured["kwargs"]["env"]["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert "DEEPSEEK_API_KEY" not in captured["kwargs"]["env"]


def test_project_check_reports_unavailable_timeout_and_truncation(tmp_path):
    policy = WorkspacePolicy(tmp_path)
    unavailable, unavailable_result = execute(
        RunProjectCheckTool(policy),
        {"name": "compile"},
    )
    assert unavailable_result.is_error is True
    assert unavailable["error"]["code"] == "check_unavailable"

    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    python = tmp_path / ".venv" / "bin" / "python"
    python.write_text("#!/bin/sh\nsleep 2\n", encoding="utf-8")
    python.chmod(0o755)
    timed_out, timeout_result = execute(
        RunProjectCheckTool(policy, timeout_seconds=0.01),
        {"name": "compile"},
    )
    assert timeout_result.is_error is True
    assert timed_out["error"]["code"] == "check_timeout"

    python.write_text(
        "#!/bin/sh\ni=0\nwhile [ $i -lt 40000 ]; do printf x; i=$((i+1)); done\n",
        encoding="utf-8",
    )
    truncated, truncated_result = execute(
        RunProjectCheckTool(policy, timeout_seconds=2),
        {"name": "pip_check"},
    )
    assert truncated_result.is_error is False
    assert truncated["data"]["truncated"] is True
    assert len(truncated["data"]["output"].encode("utf-8")) == 24 * 1024


def test_project_check_cancellation_stops_spawned_process_group(tmp_path):
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    python = tmp_path / ".venv" / "bin" / "python"
    python.write_text(
        "#!/bin/sh\necho $$ > check.pid\nsleep 10\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    tool = RunProjectCheckTool(WorkspacePolicy(tmp_path))

    async def scenario():
        task = asyncio.create_task(tool.invoke({"name": "compile"}))
        for _ in range(100):
            if (tmp_path / "check.pid").exists():
                break
            await asyncio.sleep(0.01)
        process_id = int((tmp_path / "check.pid").read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.05)
        return process_id

    process_id = asyncio.run(scenario())
    with pytest.raises(ProcessLookupError):
        os.kill(process_id, 0)


def test_journal_rejects_eleventh_change_without_losing_oldest(tmp_path):
    policy = WorkspacePolicy(tmp_path)
    journal = WorkspaceChangeJournal(max_entries=1)
    first = tmp_path / "first.txt"
    first.write_text("old", encoding="utf-8")
    first_tool = ApplyWorkspaceEditsTool(policy, journal)
    first_output, _ = execute(
        first_tool,
        {
            "edits": [{
                "mode": "replace",
                "path": "first.txt",
                "expected_sha256": hashlib.sha256(b"old").hexdigest(),
                "old_text": "old",
                "new_text": "new",
            }]
        },
        [],
    )
    second = tmp_path / "second.txt"
    second.write_text("old", encoding="utf-8")

    output, result = execute(
        ApplyWorkspaceEditsTool(policy, journal),
        {
            "edits": [{
                "mode": "replace",
                "path": "second.txt",
                "expected_sha256": hashlib.sha256(b"old").hexdigest(),
                "old_text": "old",
                "new_text": "new",
            }]
        },
        [],
    )

    assert result.is_error is True
    assert output["error"]["code"] == "workspace_conflict"
    assert second.read_text(encoding="utf-8") == "old"
    assert journal.latest.change_id == first_output["data"]["change_id"]


def test_git_diff_supports_path_and_bounded_pagination(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt", "b.txt"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=t@example.com", "commit", "-qm", "init"],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / "a.txt").write_text("two\n" * 100, encoding="utf-8")
    (tmp_path / "b.txt").write_text("three\n", encoding="utf-8")
    tool = GetWorkspaceGitDiffTool(WorkspacePolicy(tmp_path))

    first, _ = execute(tool, {"path": "a.txt", "cursor": 0, "max_chars": 120})
    second, _ = execute(
        tool,
        {"path": "a.txt", "cursor": first["data"]["next_cursor"], "max_chars": 120},
    )

    assert len(first["data"]["content"]) <= 120
    assert first["data"]["next_cursor"] is not None
    assert "b.txt" not in first["data"]["content"] + second["data"]["content"]
