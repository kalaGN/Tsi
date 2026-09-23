"""本机长任务的有界状态记录；不把对话或工具参数复制到任务文件。"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from app.runtime.model_budget import strict_json
from app.observability.model_logging import log_task_state_change
from local_paths import data_root


MAX_TASK_BYTES = 64 * 1024
MAX_TASKS = 100
TASK_STATES = frozenset({
    "ready", "running", "awaiting_approval", "verifying",
    "completed", "needs_review", "failed", "cancelled",
})
INTERRUPTIBLE_STATES = frozenset({"running", "awaiting_approval", "verifying"})
CONDITION_KINDS = frozenset({"file_exists", "file_absent", "file_sha256", "project_check"})
CHECK_NAMES = frozenset({"compile", "test_all", "pip_check", "diff_check"})


class TaskRunError(Exception):
    """稳定的任务状态错误，不暴露文件正文和底层路径。"""


class TaskRunConflict(TaskRunError):
    """任务状态在另一操作中已改变。"""


@dataclass(frozen=True)
class TaskCondition:
    kind: str
    target: str
    expected_sha256: str | None = None

    @classmethod
    def parse(cls, value: object) -> "TaskCondition":
        if not isinstance(value, dict) or set(value) != {"kind", "target", "expected_sha256"}:
            raise TaskRunError("任务验收条件无效。")
        kind, target, digest = value["kind"], value["target"], value["expected_sha256"]
        if not isinstance(kind, str) or kind not in CONDITION_KINDS or not isinstance(target, str) or not target or len(target) > 512:
            raise TaskRunError("任务验收条件无效。")
        try:
            target.encode("utf-8")
        except UnicodeError as exc:
            raise TaskRunError("任务验收条件无效。") from exc
        if any(ord(character) < 32 for character in target):
            raise TaskRunError("任务验收条件无效。")
        if kind == "project_check":
            if target not in CHECK_NAMES or digest is not None:
                raise TaskRunError("任务验收条件无效。")
        elif kind == "file_sha256":
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise TaskRunError("任务验收条件无效。")
        elif digest is not None:
            raise TaskRunError("任务验收条件无效。")
        return cls(kind, target, digest)

    def payload(self) -> dict[str, str | None]:
        return {"kind": self.kind, "target": self.target, "expected_sha256": self.expected_sha256}


@dataclass(frozen=True)
class TaskRun:
    id: str
    session_id: str
    project_id: str
    goal: str
    conditions: tuple[TaskCondition, ...]
    state: str = "ready"
    revision: int = 0
    attempts: int = 0
    max_attempts: int = 3
    last_result: str = ""

    def public_payload(self) -> dict[str, object]:
        """只给界面必要状态；不回显目标、模型请求或工具参数。"""

        return {
            "id": self.id, "session_id": self.session_id, "project_id": self.project_id,
            "state": self.state,
            "revision": self.revision, "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "conditions": [item.payload() for item in self.conditions],
            "last_result": self.last_result,
        }


def _decode(value: object) -> TaskRun:
    if not isinstance(value, dict) or set(value) != {
        "version", "id", "session_id", "project_id", "goal", "conditions", "state", "revision",
        "attempts", "max_attempts", "last_result",
    } or type(value["version"]) is not int or value["version"] != 1:
        raise TaskRunError("任务记录无效。")
    task_id = value["id"]
    session_id = value["session_id"]
    project_id = value["project_id"]
    goal = value["goal"]
    conditions = value["conditions"]
    state = value["state"]
    revision = value["revision"]
    attempts = value["attempts"]
    max_attempts = value["max_attempts"]
    last_result = value["last_result"]
    if (
        not isinstance(task_id, str) or len(task_id) != 32
        or any(char not in "0123456789abcdef" for char in task_id)
        or not isinstance(session_id, str) or not 1 <= len(session_id) <= 64
        or not isinstance(project_id, str) or not 1 <= len(project_id) <= 64
        or not isinstance(goal, str) or not goal.strip()
        or not isinstance(conditions, list) or len(conditions) > 10
        or not isinstance(state, str) or state not in TASK_STATES
        or type(revision) is not int or revision < 0
        or type(attempts) is not int or attempts < 0
        or type(max_attempts) is not int or not 1 <= max_attempts <= 5 or attempts > max_attempts
        or not isinstance(last_result, str) or len(last_result) > 160
    ):
        raise TaskRunError("任务记录无效。")
    try:
        if len(goal.encode("utf-8")) > 4096:
            raise TaskRunError("任务目标过长。")
    except UnicodeError as exc:
        raise TaskRunError("任务目标编码无效。") from exc
    return TaskRun(task_id, session_id, project_id, goal, tuple(TaskCondition.parse(item) for item in conditions),
                   state, revision, attempts, max_attempts, last_result)


class TaskRunStore:
    """单进程任务目录；进程重启只恢复安全状态，不重放工具。"""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else data_root() / "task-runs"

    def create(self, session_id: str, project_id: str, goal: str, conditions: list[dict[str, object]]) -> TaskRun:
        task = _decode({
            "version": 1, "id": uuid.uuid4().hex, "session_id": session_id,
            "project_id": project_id,
            "goal": goal, "conditions": conditions, "state": "ready", "revision": 0,
            "attempts": 0, "max_attempts": 3, "last_result": "",
        })
        if len(self.list()) >= MAX_TASKS:
            raise TaskRunError("任务数量已达上限。")
        self._write(task, create=True)
        log_task_state_change(task_id=task.id, state=task.state, attempts=task.attempts, revision=task.revision)
        return task

    def load(self, task_id: str) -> TaskRun:
        path = self._path(task_id)
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > MAX_TASK_BYTES:
                raise TaskRunError("任务记录不可用。")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (not stat.S_ISREG(opened.st_mode) or opened.st_mode & 0o077
                        or opened.st_size > MAX_TASK_BYTES):
                    raise TaskRunError("任务记录不可用。")
                raw = stream.read(MAX_TASK_BYTES + 1)
            if len(raw) > MAX_TASK_BYTES:
                raise TaskRunError("任务记录不可用。")
            task = _decode(strict_json(raw.decode("utf-8")))
            if task.id != task_id:
                raise TaskRunError("任务记录不可用。")
            return task
        except (OSError, UnicodeError, ValueError) as exc:
            raise TaskRunError("任务记录不可用。") from exc

    def list(self) -> tuple[TaskRun, ...]:
        if not self.root.exists():
            return ()
        if self.root.is_symlink() or not self.root.is_dir() or self.root.stat().st_mode & 0o077:
            raise TaskRunError("任务目录不可用。")
        try:
            paths = tuple(sorted(self.root.iterdir(), key=lambda path: path.stat().st_mtime_ns, reverse=True))
        except OSError as exc:
            raise TaskRunError("任务目录不可用。") from exc
        if len(paths) > MAX_TASKS:
            raise TaskRunError("任务数量已达上限。")
        return tuple(self.load(path.stem) for path in paths if path.suffix == ".json")

    def transition(self, task_id: str, revision: int, state: str, *, last_result: str = "") -> TaskRun:
        task = self.load(task_id)
        if type(revision) is not int or task.revision != revision:
            raise TaskRunConflict("任务已变化，请重新加载。")
        if not isinstance(state, str) or state not in TASK_STATES or not isinstance(last_result, str) or len(last_result) > 160:
            raise TaskRunError("任务状态无效。")
        next_attempts = task.attempts + (state == "running" and task.state in {"ready", "needs_review"})
        if next_attempts > task.max_attempts:
            raise TaskRunError("任务尝试次数已达上限。")
        allowed = {
            "ready": {"running", "cancelled"},
            "running": {"awaiting_approval", "verifying", "needs_review", "failed", "cancelled"},
            "awaiting_approval": {"running", "needs_review", "cancelled"},
            "verifying": {"completed", "needs_review", "failed", "cancelled"},
            "needs_review": {"running", "cancelled"},
            "failed": set(), "completed": set(), "cancelled": set(),
        }
        if state not in allowed[task.state]:
            raise TaskRunError("任务状态转换无效。")
        updated = replace(task, state=state, revision=revision + 1, attempts=next_attempts,
                          last_result=last_result)
        self._write(updated)
        log_task_state_change(task_id=updated.id, state=updated.state,
                              attempts=updated.attempts, revision=updated.revision)
        return updated

    def recover_interrupted(self) -> tuple[TaskRun, ...]:
        """冷启动标记不确定步骤；绝不自动执行或批准任何工具。"""

        recovered = []
        for task in self.list():
            if task.state in INTERRUPTIBLE_STATES:
                recovered.append(self.transition(task.id, task.revision, "needs_review",
                                                 last_result="上次执行中断，请先检查工作区。"))
        return tuple(recovered)

    def _path(self, task_id: str) -> Path:
        if not isinstance(task_id, str) or len(task_id) != 32 or any(c not in "0123456789abcdef" for c in task_id):
            raise TaskRunError("任务 ID 无效。")
        return self.root / f"{task_id}.json"

    def _write(self, task: TaskRun, *, create: bool = False) -> None:
        payload = {
            "version": 1, "id": task.id, "session_id": task.session_id,
            "project_id": task.project_id, "goal": task.goal,
            "conditions": [item.payload() for item in task.conditions],
            "state": task.state, "revision": task.revision,
            "attempts": task.attempts, "max_attempts": task.max_attempts,
            "last_result": task.last_result,
        }
        encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > MAX_TASK_BYTES:
            raise TaskRunError("任务记录过大。")
        temporary = None
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.root.is_symlink() or not self.root.is_dir():
                raise OSError("unsafe task directory")
            os.chmod(self.root, 0o700)
            path = self._path(task.id)
            if path.is_symlink() or (path.exists() and create):
                raise OSError("unsafe task target")
            descriptor, name = tempfile.mkstemp(dir=self.root, prefix=".task-", suffix=".tmp")
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            raise TaskRunError("任务保存失败。") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
