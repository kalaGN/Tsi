import asyncio
import json
import subprocess

import pytest

from tools.contracts import GitApprovalRequest, ToolCall, ToolExecutionContext
from tools.git import GitCommitTool, GitPushTool, GitStageTool
from tools.registry import ToolRegistry
from tools.workspace import WorkspacePolicy


def _git(root, *arguments):
    return subprocess.run(
        ("git", *arguments),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.name", "Tsi Test")
    _git(tmp_path, "config", "user.email", "tsi@example.invalid")
    target = tmp_path / "demo.txt"
    target.write_text("初始\n", encoding="utf-8")
    _git(tmp_path, "add", "--", "demo.txt")
    _git(tmp_path, "commit", "-q", "-m", "test: 初始提交")
    return target


def _execute(tool, arguments, approval=None):
    context = None
    if approval is not None:
        context = ToolExecutionContext(approval_handler=approval)
    result = asyncio.run(
        ToolRegistry((tool,)).execute(
            ToolCall("git-call", tool.definition.name, json.dumps(arguments)),
            context,
        )
    )
    return json.loads(result.output), result


def test_stage_previews_without_side_effect_then_stages_selected_files(tmp_path):
    target = _repository(tmp_path)
    target.write_text("修改后\n", encoding="utf-8")
    extra = tmp_path / "新增.txt"
    extra.write_text("新增\n", encoding="utf-8")
    approvals = []

    async def approve(request):
        approvals.append(request)
        assert _git(tmp_path, "diff", "--cached") == ""
        return True

    payload, result = _execute(
        GitStageTool(WorkspacePolicy(tmp_path)),
        {"paths": ["新增.txt", "demo.txt"]},
        approve,
    )

    assert result.is_error is False
    assert payload["data"] == {
        "paths": ["demo.txt", "新增.txt"],
        "staged": True,
    }
    assert isinstance(approvals[0], GitApprovalRequest)
    assert approvals[0].operation == "stage"
    assert "-初始" in approvals[0].preview_text
    assert "+修改后" in approvals[0].preview_text
    assert "新增.txt" in _git(
        tmp_path,
        "-c",
        "core.quotePath=false",
        "diff",
        "--cached",
        "--name-only",
    )


@pytest.mark.parametrize(
    "paths",
    [
        [],
        ["demo.txt", "demo.txt"],
        [["demo.txt"]],
        ["."],
        ["-A"],
        ["bad\nname.txt"],
    ],
)
def test_stage_rejects_non_explicit_file_paths(tmp_path, paths):
    _repository(tmp_path)

    payload, result = _execute(
        GitStageTool(WorkspacePolicy(tmp_path)),
        {"paths": paths},
        lambda _request: True,
    )

    assert result.is_error is True
    assert payload["error"]["code"] == "invalid_arguments"


def test_stage_denial_and_workspace_race_do_not_change_real_index(tmp_path):
    target = _repository(tmp_path)
    target.write_text("第一次修改\n", encoding="utf-8")

    async def deny(_request):
        return False

    denied, _ = _execute(
        GitStageTool(WorkspacePolicy(tmp_path)),
        {"paths": ["demo.txt"]},
        deny,
    )

    async def mutate_then_approve(_request):
        target.write_text("审批后又修改\n", encoding="utf-8")
        return True

    conflicted, _ = _execute(
        GitStageTool(WorkspacePolicy(tmp_path)),
        {"paths": ["demo.txt"]},
        mutate_then_approve,
    )

    assert denied["error"]["code"] == "approval_denied"
    assert conflicted["error"]["code"] == "git_conflict"
    assert _git(tmp_path, "diff", "--cached") == ""


def test_commit_uses_chinese_message_and_disables_hooks_and_signing(tmp_path):
    target = _repository(tmp_path)
    target.write_text("准备提交\n", encoding="utf-8")
    _git(tmp_path, "add", "--", "demo.txt")
    hooks = tmp_path / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    marker = tmp_path / "hook-ran"
    hook = hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    _git(tmp_path, "config", "commit.gpgSign", "true")
    approvals = []

    async def approve(request):
        approvals.append(request)
        return True

    payload, result = _execute(
        GitCommitTool(WorkspacePolicy(tmp_path)),
        {"message": "feat: 增加安全提交"},
        approve,
    )

    assert result.is_error is False
    assert payload["data"]["branch"] == "main"
    assert payload["data"]["message"] == "feat: 增加安全提交"
    assert len(payload["data"]["commit"]) == 12
    assert _git(tmp_path, "log", "-1", "--format=%s") == "feat: 增加安全提交"
    assert not marker.exists()
    assert "提交信息：feat: 增加安全提交" in approvals[0].preview_text


@pytest.mark.parametrize(
    "message",
    ["feat: english only", "中文但无类型", "feat: 中文\n第二行", "oops: 中文"],
)
def test_commit_rejects_invalid_message(tmp_path, message):
    target = _repository(tmp_path)
    target.write_text("修改\n", encoding="utf-8")
    _git(tmp_path, "add", "--", "demo.txt")

    payload, result = _execute(
        GitCommitTool(WorkspacePolicy(tmp_path)),
        {"message": message},
        lambda _request: True,
    )

    assert result.is_error is True
    assert payload["error"]["code"] == "invalid_arguments"


class _PushRunner:
    def __init__(self, *, remote_url="https://token@github.com/acme/repo.git"):
        self.remote_url = remote_url
        self.head = "a" * 40
        self.upstream = True
        self.pushed = []

    async def run(self, arguments, **_kwargs):
        command = tuple(arguments)
        if command == ("symbolic-ref", "--short", "HEAD"):
            return "main\n"
        if command == ("config", "--get", "branch.main.remote"):
            return "origin\n" if self.upstream else ""
        if command == ("config", "--get", "branch.main.merge"):
            return "refs/heads/main\n" if self.upstream else ""
        if command == ("remote", "get-url", "--push", "origin"):
            return self.remote_url + "\n"
        if command == ("rev-list", "--reverse", "origin/main..HEAD"):
            return self.head + "\n"
        if command == ("rev-parse", "HEAD"):
            return self.head + "\n"
        if command == ("log", "--format=%h %s", "origin/main..HEAD"):
            return "aaaaaaaaaaaa feat: 待推送提交\n"
        if command[:2] == ("push", "--porcelain"):
            self.pushed.append(command)
            return ""
        raise AssertionError(f"unexpected git command: {command!r}")


def test_push_redacts_credentials_and_runs_only_after_approval(tmp_path):
    runner = _PushRunner()
    approvals = []

    async def approve(request):
        approvals.append(request)
        assert runner.pushed == []
        return True

    payload, result = _execute(
        GitPushTool(WorkspacePolicy(tmp_path), runner),
        {},
        approve,
    )

    assert result.is_error is False
    assert payload["data"] == {"remote": "origin", "branch": "main", "commits": 1}
    assert runner.pushed == [
        ("push", "--porcelain", "--", "origin", "HEAD:refs/heads/main")
    ]
    assert approvals[0].network_access is True
    assert "https://github.com/<redacted>" in approvals[0].summary
    assert "token" not in approvals[0].summary


def test_push_rejects_missing_upstream_unsafe_remote_and_state_race(tmp_path):
    missing = _PushRunner()
    missing.upstream = False
    missing_payload, _ = _execute(
        GitPushTool(WorkspacePolicy(tmp_path), missing),
        {},
        lambda _request: True,
    )

    async def approve(_request):
        return True

    unsafe_payload, _ = _execute(
        GitPushTool(
            WorkspacePolicy(tmp_path),
            _PushRunner(remote_url="file:///tmp/remote.git"),
        ),
        {},
        approve,
    )

    racing = _PushRunner()

    async def change_head(_request):
        racing.head = "b" * 40
        return True

    race_payload, _ = _execute(
        GitPushTool(WorkspacePolicy(tmp_path), racing),
        {},
        change_head,
    )

    assert missing_payload["error"]["code"] == "git_no_upstream"
    assert unsafe_payload["error"]["code"] == "git_remote_unsafe"
    assert race_payload["error"]["code"] == "git_conflict"
    assert racing.pushed == []
