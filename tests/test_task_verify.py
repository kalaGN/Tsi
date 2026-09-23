"""任务验收只使用确定性证据。"""

import asyncio
import hashlib

from app.runtime.task_runs import TaskCondition
from app.runtime.task_verify import verify_task
from tools.workspace import WorkspacePolicy


def condition(kind, target, digest=None):
    return TaskCondition(kind, target, digest)


def test_file_evidence_and_unknown_conditions(tmp_path):
    (tmp_path / "answer.txt").write_text("ok", encoding="utf-8")
    policy = WorkspacePolicy(tmp_path)
    digest = hashlib.sha256(b"ok").hexdigest()
    result = asyncio.run(verify_task((
        condition("file_exists", "answer.txt"),
        condition("file_absent", "missing.txt"),
        condition("file_sha256", "answer.txt", digest),
    ), policy))
    assert result.status == "passed"
    assert all(item.status == "passed" for item in result.evidence)
    assert asyncio.run(verify_task((), policy)).status == "unknown"


def test_mismatch_and_protected_path_cannot_complete(tmp_path):
    (tmp_path / "answer.txt").write_text("wrong", encoding="utf-8")
    policy = WorkspacePolicy(tmp_path)
    result = asyncio.run(verify_task((
        condition("file_sha256", "answer.txt", "0" * 64),
        condition("file_exists", ".env"),
    ), policy))
    assert result.status == "failed"
    assert [item.status for item in result.evidence] == ["failed", "unknown"]


def test_fixed_project_check_uses_only_named_check(monkeypatch, tmp_path):
    called = []

    async def fake_check(_self, arguments):
        called.append(arguments)
        return {"exit_code": 0, "truncated": False}

    monkeypatch.setattr("app.runtime.task_verify.RunProjectCheckTool.invoke", fake_check)
    result = asyncio.run(verify_task((condition("project_check", "diff_check"),), WorkspacePolicy(tmp_path)))
    assert result.status == "passed"
    assert called == [{"name": "diff_check"}]
