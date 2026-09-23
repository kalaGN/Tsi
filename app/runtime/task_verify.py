"""长任务的确定性验收器；仅使用受限工作区和固定项目检查。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.runtime.task_runs import TaskCondition
from tools.contracts import ToolArgumentError, ToolRejectedError
from tools.project_checks import RunProjectCheckTool
from tools.workspace import WorkspacePathError, WorkspacePolicy


MAX_VERIFY_FILE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class ConditionEvidence:
    condition: TaskCondition
    status: str
    detail: str

    def payload(self) -> dict[str, object]:
        return {"condition": self.condition.payload(), "status": self.status, "detail": self.detail}


@dataclass(frozen=True)
class VerificationResult:
    status: str
    evidence: tuple[ConditionEvidence, ...]

    def payload(self) -> dict[str, object]:
        return {"status": self.status, "evidence": [item.payload() for item in self.evidence]}


async def verify_task(conditions: tuple[TaskCondition, ...], policy: WorkspacePolicy) -> VerificationResult:
    """未知条件不算通过；固定检查只回传状态，不回传可能含秘密的 stdout。"""

    if not conditions:
        return VerificationResult("unknown", ())
    evidence = []
    check_tool = None
    for condition in conditions:
        if condition.kind == "project_check":
            if check_tool is None:
                check_tool = RunProjectCheckTool(policy)
            try:
                result = await check_tool.invoke({"name": condition.target})
            except (ToolArgumentError, ToolRejectedError, OSError, ValueError):
                evidence.append(ConditionEvidence(condition, "unknown", "固定检查不可用。"))
            else:
                passed = result["exit_code"] == 0 and not result["truncated"]
                evidence.append(ConditionEvidence(
                    condition, "passed" if passed else "failed",
                    "固定检查通过。" if passed else "固定检查未通过或输出被截断。",
                ))
            continue
        try:
            if condition.kind == "file_absent":
                try:
                    policy.resolve_write_file(condition.target, creating=True)
                except WorkspacePathError:
                    policy.resolve_read_file(condition.target)
                    evidence.append(ConditionEvidence(condition, "failed", "目标文件仍存在。"))
                else:
                    evidence.append(ConditionEvidence(condition, "passed", "目标文件不存在。"))
                continue
            path = policy.resolve_read_file(condition.target)
            details = path.stat()
            if details.st_size > MAX_VERIFY_FILE_BYTES:
                raise ValueError("file too large")
            if condition.kind == "file_exists":
                evidence.append(ConditionEvidence(condition, "passed", "目标文件存在。"))
            else:
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
                passed = actual == condition.expected_sha256
                evidence.append(ConditionEvidence(
                    condition, "passed" if passed else "failed",
                    "文件 SHA-256 匹配。" if passed else "文件 SHA-256 不匹配。",
                ))
        except (WorkspacePathError, OSError, ValueError):
            evidence.append(ConditionEvidence(condition, "unknown", "目标文件无法安全检查。"))
    status = "passed" if all(item.status == "passed" for item in evidence) else (
        "failed" if any(item.status == "failed" for item in evidence) else "unknown"
    )
    return VerificationResult(status, tuple(evidence))
