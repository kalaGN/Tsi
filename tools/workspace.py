"""受启动目录约束的只读、结构化编辑和撤销工具。"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import shutil
import signal
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Mapping
from uuid import uuid4

from tools.contracts import (
    ToolApprovalRequest,
    ToolArgumentError,
    ToolDefinition,
    ToolEffect,
    ToolErrorCode,
    ToolRejectedError,
)

if TYPE_CHECKING:
    from tools.skills import SkillCatalog


MAX_READ_FILE_BYTES = 1024 * 1024
MAX_EDIT_FILE_BYTES = 256 * 1024
MAX_EDIT_FILES = 10
MAX_DIFF_BYTES = 64 * 1024
MAX_JOURNAL_BYTES = 512 * 1024
MAX_RELATIVE_PATH_CHARS = 1024
MAX_TOOL_TEXT_CHARS = 24 * 1024
PROTECTED_COMPONENTS = {".git", ".venv", "data", "logs", "__pycache__", ".pytest_cache"}
PROTECTED_WRITE_PATHS = {
    "AGENTS.md",
    ".gitignore",
    "requirements.txt",
    "tools/contracts.py",
    "tools/registry.py",
    "tools/workspace.py",
    "tools/project_checks.py",
    "tools/__init__.py",
    "app/runtime/chat.py",
    "app/runtime/session.py",
    "app/runtime/tool_loop.py",
    "app/tui/__main__.py",
    "app/tui/approval.py",
    "app/tui/application.py",
    "app/tui/state.py",
}


class WorkspacePathError(ValueError):
    """路径不满足 Workspace 边界，并标明是否命中保护规则。"""

    def __init__(self, *, protected: bool = False) -> None:
        super().__init__("workspace path is invalid")
        self.protected = protected


@dataclass(frozen=True)
class WorkspacePolicy:
    """固定根目录并集中验证路径、符号链接和保护区域。"""

    root: Path

    def __post_init__(self) -> None:
        supplied = Path(self.root)
        if supplied.is_symlink():
            raise ValueError("workspace root must be a directory")
        root = supplied.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("workspace root must be a directory")
        object.__setattr__(self, "root", root)

    def normalize(self, relative_path: object, *, writing: bool = False) -> str:
        if (
            not isinstance(relative_path, str)
            or not relative_path
            or len(relative_path) > MAX_RELATIVE_PATH_CHARS
            or "\x00" in relative_path
        ):
            raise WorkspacePathError()
        raw_parts = relative_path.split("/")
        if relative_path != "." and any(part in ("", ".") for part in raw_parts):
            raise WorkspacePathError()
        path = PurePosixPath(relative_path)
        if path.is_absolute() or any(part in ("", "..") for part in path.parts):
            raise WorkspacePathError()
        normalized = path.as_posix()
        if normalized == "":
            raise WorkspacePathError()
        if self._is_protected(path, writing=writing):
            raise WorkspacePathError(protected=True)
        return normalized

    def resolve_read_file(self, relative_path: object) -> Path:
        normalized = self.normalize(relative_path)
        target = self._walk(normalized, allow_missing=False)
        if not target.is_file():
            raise WorkspacePathError()
        return target

    def resolve_read_directory(self, relative_path: object = ".") -> Path:
        normalized = self.normalize(relative_path)
        target = self._walk(normalized, allow_missing=False)
        if not target.is_dir():
            raise WorkspacePathError()
        return target

    def resolve_write_file(self, relative_path: object, *, creating: bool) -> Path:
        normalized = self.normalize(relative_path, writing=True)
        target = self._walk(normalized, allow_missing=creating)
        if creating:
            if target.exists() or target.is_symlink() or not target.parent.is_dir():
                raise WorkspacePathError()
        elif not target.is_file():
            raise WorkspacePathError()
        return target

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def is_protected_relative(
        self,
        relative_path: str,
        *,
        writing: bool = False,
    ) -> bool:
        try:
            self.normalize(relative_path, writing=writing)
        except WorkspacePathError as exc:
            return exc.protected
        return False

    def _walk(self, normalized: str, *, allow_missing: bool) -> Path:
        current = self.root
        parts = PurePosixPath(normalized).parts
        if parts == (".",):
            return current
        for index, part in enumerate(parts):
            current = current / part
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                if allow_missing and index == len(parts) - 1:
                    break
                raise WorkspacePathError() from None
            if stat.S_ISLNK(mode):
                raise WorkspacePathError()
            if index < len(parts) - 1 and not stat.S_ISDIR(mode):
                raise WorkspacePathError()
        try:
            current.absolute().relative_to(self.root)
        except ValueError:
            raise WorkspacePathError() from None
        return current

    @staticmethod
    def _is_protected(path: PurePosixPath, *, writing: bool) -> bool:
        parts = path.parts
        if any(part in PROTECTED_COMPONENTS for part in parts):
            return True
        if any(part == ".env" or part.startswith(".env.") for part in parts):
            return True
        normalized = path.as_posix()
        if writing and (
            normalized in PROTECTED_WRITE_PATHS
            or normalized.startswith("docs/rules/")
        ):
            return True
        return False


def _arguments(arguments: Mapping[str, object], allowed: set[str]) -> None:
    if not set(arguments).issubset(allowed):
        raise ToolArgumentError()


def _integer(value: object, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise ToolArgumentError()
    return value


def _read_text(
    path: Path,
    *,
    max_bytes: int = MAX_READ_FILE_BYTES,
) -> tuple[bytes, str]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ToolArgumentError() from exc
    if len(data) > max_bytes or b"\x00" in data:
        raise ToolArgumentError()
    try:
        return data, data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolArgumentError() from exc


class ListWorkspaceFilesTool:
    definition = ToolDefinition(
        "list_workspace_files",
        "分页列出启动工作区内允许读取的文件和目录",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "depth": {"type": "integer"},
                "cursor": {"type": "integer"},
                "limit": {"type": "integer"},
            },
            "additionalProperties": False,
        },
    )

    def __init__(self, policy: WorkspacePolicy) -> None:
        self.policy = policy

    async def invoke(self, arguments: Mapping[str, object]) -> object:
        _arguments(arguments, {"path", "depth", "cursor", "limit"})
        try:
            base = self.policy.resolve_read_directory(arguments.get("path", "."))
        except WorkspacePathError as exc:
            raise _path_error(exc) from exc
        depth = _integer(arguments.get("depth"), 5, 1, 5)
        cursor = _integer(arguments.get("cursor"), 0, 0, 1_000_000)
        limit = _integer(arguments.get("limit"), 200, 1, 200)
        base_depth = len(base.relative_to(self.policy.root).parts)
        items: list[dict[str, str]] = []
        for current, directories, files in os.walk(base, followlinks=False):
            current_path = Path(current)
            relative_depth = (
                len(current_path.relative_to(self.policy.root).parts) - base_depth
            )
            allowed_directories = []
            for name in directories:
                candidate = current_path / name
                relative = self.policy.relative(candidate)
                if candidate.is_symlink() or self.policy.is_protected_relative(
                    relative
                ):
                    continue
                if relative_depth < depth:
                    allowed_directories.append(name)
                    items.append({"path": relative, "type": "directory"})
            directories[:] = allowed_directories if relative_depth < depth else []
            if relative_depth >= depth:
                continue
            for name in files:
                candidate = current_path / name
                relative = self.policy.relative(candidate)
                if candidate.is_symlink() or self.policy.is_protected_relative(
                    relative
                ):
                    continue
                if candidate.is_file():
                    items.append({"path": relative, "type": "file"})
        items.sort(key=lambda item: item["path"])
        page = _bounded_page(items, cursor, limit)
        next_cursor = cursor + len(page) if cursor + len(page) < len(items) else None
        return {"items": page, "next_cursor": next_cursor}


class SearchWorkspaceTextTool:
    definition = ToolDefinition(
        "search_workspace_text",
        "在启动工作区允许读取的 UTF-8 文件中按字面量搜索",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "path": {"type": "string"},
                "glob": {"type": "string"},
                "cursor": {"type": "integer"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    )

    def __init__(self, policy: WorkspacePolicy) -> None:
        self.policy = policy

    async def invoke(self, arguments: Mapping[str, object]) -> object:
        _arguments(arguments, {"query", "path", "glob", "cursor", "limit"})
        query = arguments.get("query")
        pattern = arguments.get("glob")
        if not isinstance(query, str) or not 1 <= len(query) <= 256:
            raise ToolArgumentError()
        if pattern is not None and (not isinstance(pattern, str) or not pattern):
            raise ToolArgumentError()
        try:
            base = self.policy.resolve_read_directory(arguments.get("path", "."))
        except WorkspacePathError as exc:
            raise _path_error(exc) from exc
        cursor = _integer(arguments.get("cursor"), 0, 0, 1_000_000)
        limit = _integer(arguments.get("limit"), 100, 1, 100)
        matches: list[dict[str, object]] = []
        scanned = 0
        for path in _allowed_files(self.policy, base):
            relative = self.policy.relative(path)
            if self.policy.is_protected_relative(relative):
                continue
            if pattern is not None and not PurePosixPath(relative).match(pattern):
                continue
            if scanned >= 2000:
                break
            scanned += 1
            try:
                _, text = _read_text(path)
            except ToolArgumentError:
                continue
            for line_number, line in enumerate(text.splitlines(), 1):
                if query in line:
                    truncated = len(line) > 500
                    matches.append(
                        {
                            "path": relative,
                            "line": line_number,
                            "text": line[:500],
                            "truncated": truncated,
                        }
                    )
        page = _bounded_page(matches, cursor, limit)
        next_cursor = cursor + len(page) if cursor + len(page) < len(matches) else None
        return {"matches": page, "next_cursor": next_cursor, "scanned_files": scanned}


def _bounded_page(items: list[dict[str, object]], cursor: int, limit: int):
    """在条数限制之外约束 JSON 页大小，给 Registry 封装留出余量。"""

    page: list[dict[str, object]] = []
    for item in items[cursor : cursor + limit]:
        candidate = page + [item]
        size = len(
            json.dumps(candidate, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        if size > MAX_TOOL_TEXT_CHARS:
            break
        page.append(item)
    return page


class ReadWorkspaceFileTool:
    definition = ToolDefinition(
        "read_workspace_file",
        "按行读取启动工作区内允许的 UTF-8 文本文件并返回哈希",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "max_lines": {"type": "integer"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    )

    def __init__(self, policy: WorkspacePolicy) -> None:
        self.policy = policy

    async def invoke(self, arguments: Mapping[str, object]) -> object:
        _arguments(arguments, {"path", "start_line", "max_lines"})
        try:
            path = self.policy.resolve_read_file(arguments.get("path"))
        except WorkspacePathError as exc:
            raise _path_error(exc) from exc
        data, text = _read_text(path)
        start = _integer(arguments.get("start_line"), 1, 1, 10_000_000)
        maximum = _integer(arguments.get("max_lines"), 400, 1, 400)
        lines = text.splitlines(keepends=True)
        selected: list[str] = []
        selected_bytes = 0
        content_truncated = False
        for line in lines[start - 1 : start - 1 + maximum]:
            remaining = MAX_TOOL_TEXT_CHARS - selected_bytes
            if remaining <= 0:
                break
            selected_line, was_truncated = _truncate_utf8(line, remaining)
            selected.append(selected_line)
            selected_bytes += len(selected_line.encode("utf-8"))
            if was_truncated:
                content_truncated = True
                break
        end = start + len(selected) - 1 if selected else 0
        next_line = end + 1 if end and end < len(lines) else None
        return {
            "path": self.policy.relative(path),
            "sha256": hashlib.sha256(data).hexdigest(),
            "total_lines": len(lines),
            "start_line": start,
            "end_line": end,
            "next_line": next_line,
            "content_truncated": content_truncated,
            "content": "".join(selected),
        }


def _truncate_utf8(text: str, maximum_bytes: int) -> tuple[str, bool]:
    """按 UTF-8 字节边界截断，不制造替换字符或破坏中文。"""

    encoded = text.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return text, False
    truncated = encoded[:maximum_bytes]
    while truncated:
        try:
            return truncated.decode("utf-8"), True
        except UnicodeDecodeError:
            truncated = truncated[:-1]
    return "", True


def _allowed_files(policy: WorkspacePolicy, base: Path):
    """稳定遍历允许文件，并在进入保护目录前剪枝。"""

    candidates: list[Path] = []
    for current, directories, files in os.walk(base, followlinks=False):
        current_path = Path(current)
        directories[:] = sorted(
            name
            for name in directories
            if not (current_path / name).is_symlink()
            and not policy.is_protected_relative(
                policy.relative(current_path / name)
            )
        )
        for name in files:
            path = current_path / name
            relative = policy.relative(path)
            if (
                not path.is_symlink()
                and path.is_file()
                and not policy.is_protected_relative(relative)
            ):
                candidates.append(path)
    yield from sorted(candidates, key=lambda path: policy.relative(path))


async def _run_git(
    policy: WorkspacePolicy,
    *arguments: str,
    max_output_bytes: int = 256 * 1024,
) -> dict[str, object]:
    git = shutil.which("git")
    if git is None:
        raise ToolRejectedError(ToolErrorCode.CHECK_UNAVAILABLE)
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "LANG": "C.UTF-8",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    try:
        process = await asyncio.create_subprocess_exec(
            git, *arguments, cwd=policy.root, env=environment,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        raise ToolRejectedError(ToolErrorCode.CHECK_UNAVAILABLE) from exc
    try:
        output, truncated = await asyncio.wait_for(
            _read_process_output(process, max_output_bytes),
            timeout=30,
        )
    except asyncio.TimeoutError as exc:
        await _stop_process(process)
        raise ToolRejectedError(ToolErrorCode.CHECK_UNAVAILABLE) from exc
    except asyncio.CancelledError:
        await _stop_process(process)
        raise
    # 非法 UTF-8 字节会被替换字符扩张；再次按字节截断，保证公开结果仍受同一上限约束。
    text, decode_truncated = _truncate_utf8(
        output.decode("utf-8", errors="replace"),
        max_output_bytes,
    )
    truncated = truncated or decode_truncated
    if process.returncode != 0:
        raise ToolRejectedError(ToolErrorCode.CHECK_UNAVAILABLE)
    return {"exit_code": process.returncode, "output": text, "truncated": truncated}


async def _read_process_output(
    process: asyncio.subprocess.Process,
    maximum: int,
) -> tuple[bytes, bool]:
    """持续排空子进程管道，但只在内存保留固定字节数。"""

    if process.stdout is None:
        raise RuntimeError("process stdout is unavailable")
    retained = bytearray()
    truncated = False
    while True:
        chunk = await process.stdout.read(8192)
        if not chunk:
            break
        remaining = maximum - len(retained)
        if remaining > 0:
            retained.extend(chunk[:remaining])
        if len(chunk) > max(remaining, 0):
            truncated = True
    await process.wait()
    return bytes(retained), truncated


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (AttributeError, ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=1)
    except asyncio.TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (AttributeError, ProcessLookupError):
            process.kill()
        await process.wait()


class GetWorkspaceGitStatusTool:
    definition = ToolDefinition(
        "get_workspace_git_status",
        "读取工作区 Git 简短状态",
        {"type": "object", "properties": {}, "additionalProperties": False},
    )

    def __init__(self, policy: WorkspacePolicy) -> None:
        self.policy = policy

    async def invoke(self, arguments: Mapping[str, object]) -> object:
        _arguments(arguments, set())
        return await _run_git(
            self.policy,
            "-c",
            "core.quotepath=false",
            "status",
            "--short",
            "--untracked-files=all",
            max_output_bytes=MAX_TOOL_TEXT_CHARS,
        )


class GetWorkspaceGitDiffTool:
    definition = ToolDefinition(
        "get_workspace_git_diff",
        "分页读取工作区已暂存和未暂存 Diff，可限定一个允许的相对路径",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "cursor": {"type": "integer"},
                "max_chars": {"type": "integer"},
            },
            "additionalProperties": False,
        },
    )

    def __init__(self, policy: WorkspacePolicy) -> None:
        self.policy = policy

    async def invoke(self, arguments: Mapping[str, object]) -> object:
        _arguments(arguments, {"path", "cursor", "max_chars"})
        cursor = _integer(arguments.get("cursor"), 0, 0, 1_000_000)
        maximum = _integer(
            arguments.get("max_chars"),
            6 * 1024,
            100,
            6 * 1024,
        )
        path_argument = arguments.get("path")
        suffix: tuple[str, ...] = ()
        if path_argument is not None:
            try:
                relative = self.policy.normalize(path_argument)
            except WorkspacePathError as exc:
                raise _path_error(exc) from exc
            suffix = ("--", relative)
        unstaged = await _run_git(
            self.policy,
            "-c", "core.quotepath=false", "diff", "--no-ext-diff", "--no-color",
            *suffix,
        )
        staged = await _run_git(
            self.policy,
            "-c",
            "core.quotepath=false",
            "diff",
            "--cached",
            "--no-ext-diff",
            "--no-color",
            *suffix,
        )
        combined = (
            "## unstaged\n"
            + str(unstaged["output"])
            + "\n## staged\n"
            + str(staged["output"])
        )
        content = combined[cursor : cursor + maximum]
        next_cursor = (
            cursor + len(content)
            if cursor + len(content) < len(combined)
            else None
        )
        return {
            "content": content,
            "next_cursor": next_cursor,
            "source_truncated": bool(unstaged["truncated"] or staged["truncated"]),
        }


@dataclass(frozen=True)
class _FileChange:
    path: Path
    relative: str
    before: bytes | None
    after: bytes
    mode: int


@dataclass(frozen=True)
class WorkspaceChange:
    change_id: str
    files: tuple[_FileChange, ...]
    result_hashes: tuple[str, ...]


class WorkspaceChangeJournal:
    """保留当前进程最近十个批次，以 LIFO 方式撤销。"""

    def __init__(self, max_entries: int = 10) -> None:
        self.max_entries = max_entries
        self._entries: list[WorkspaceChange] = []

    @property
    def latest(self) -> WorkspaceChange | None:
        return self._entries[-1] if self._entries else None

    def append(self, change: WorkspaceChange) -> None:
        snapshot_size = sum(len(item.before or b"") for item in change.files)
        if snapshot_size > MAX_JOURNAL_BYTES:
            raise ToolArgumentError()
        if len(self._entries) >= self.max_entries:
            raise ToolRejectedError(ToolErrorCode.WORKSPACE_CONFLICT)
        self._entries.append(change)

    def ensure_capacity(self, changes=()) -> None:
        """在写盘前拒绝无法记录的批次，避免提交后再回滚。"""

        if len(self._entries) >= self.max_entries:
            raise ToolRejectedError(ToolErrorCode.WORKSPACE_CONFLICT)
        snapshot_size = sum(len(item.before or b"") for item in changes)
        if snapshot_size > MAX_JOURNAL_BYTES:
            raise ToolArgumentError()

    def pop(self, change_id: str) -> WorkspaceChange:
        if self.latest is None or self.latest.change_id != change_id:
            raise ToolRejectedError(ToolErrorCode.WORKSPACE_CONFLICT)
        return self._entries.pop()


class ApplyWorkspaceEditsTool:
    definition = ToolDefinition(
        "apply_workspace_edits",
        (
            "创建文件或按精确文本替换工作区文件；执行前必须审批。"
            "create 传 mode/path/content；replace 传 mode/path/expected_sha256/"
            "old_text/new_text，其中 expected_sha256 来自 read_workspace_file"
        ),
        {
            "type": "object",
            "properties": {
                "edits": {
                    "type": "array",
                    "description": "1 至 10 个结构化文件变更",
                    "minItems": 1,
                    "maxItems": MAX_EDIT_FILES,
                    "items": {
                        "type": "object",
                        "properties": {
                            "mode": {
                                "type": "string",
                                "enum": ["create", "replace"],
                            },
                            "path": {
                                "type": "string",
                                "description": "工作区内的相对文件路径",
                            },
                            "content": {
                                "type": "string",
                                "description": "create 模式的新文件完整内容",
                            },
                            "expected_sha256": {
                                "type": "string",
                                "description": (
                                    "replace 模式必填；read_workspace_file "
                                    "返回的完整文件 SHA-256"
                                ),
                                "pattern": "^[0-9a-f]{64}$",
                            },
                            "old_text": {
                                "type": "string",
                                "description": "replace 模式必填且须在文件中仅出现一次",
                            },
                            "new_text": {
                                "type": "string",
                                "description": "replace 模式替换后的文本，可为空字符串",
                            },
                        },
                        "required": ["mode", "path"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["edits"],
            "additionalProperties": False,
        },
        effect=ToolEffect.MUTATING,
        max_argument_bytes=64 * 1024,
    )

    def __init__(
        self,
        policy: WorkspacePolicy,
        journal: WorkspaceChangeJournal,
    ) -> None:
        self.policy = policy
        self.journal = journal
        self._lock = asyncio.Lock()
        self._approved_plans: dict[str, str] = {}

    async def preview(
        self,
        call_id: str,
        arguments: Mapping[str, object],
    ) -> ToolApprovalRequest:
        async with self._lock:
            changes, diff = self._plan(arguments)
            fingerprint = _fingerprint(self.definition.name, arguments, diff)
            self._approved_plans[_argument_digest(arguments)] = fingerprint
            return ToolApprovalRequest(
                call_id,
                self.definition.name,
                f"应用 {len(changes)} 个文件修改",
                tuple(item.relative for item in changes),
                diff,
                fingerprint,
            )

    async def invoke(self, arguments: Mapping[str, object]) -> object:
        async with self._lock:
            argument_digest = _argument_digest(arguments)
            expected = self._approved_plans.pop(argument_digest, None)
            try:
                changes, diff = self._plan(arguments)
            except ToolArgumentError as exc:
                if expected is not None:
                    raise ToolRejectedError(
                        ToolErrorCode.WORKSPACE_CONFLICT
                    ) from exc
                raise
            fingerprint = _fingerprint(self.definition.name, arguments, diff)
            if expected != fingerprint:
                raise ToolRejectedError(ToolErrorCode.WORKSPACE_CONFLICT)
            self.journal.ensure_capacity(changes)
            _commit_changes(changes)
            change = WorkspaceChange(
                uuid4().hex,
                tuple(changes),
                tuple(
                    hashlib.sha256(item.after).hexdigest() for item in changes
                ),
            )
            try:
                self.journal.append(change)
            except Exception:
                _restore_changes(changes)
                raise
            return {
                "change_id": change.change_id,
                "paths": [item.relative for item in changes],
            }

    def _plan(self, arguments: Mapping[str, object]) -> tuple[list[_FileChange], str]:
        _arguments(arguments, {"edits"})
        edits = arguments.get("edits")
        if not isinstance(edits, list) or not 1 <= len(edits) <= MAX_EDIT_FILES:
            raise ToolArgumentError()
        changes: list[_FileChange] = []
        seen: set[str] = set()
        for edit in edits:
            if not isinstance(edit, dict) or not isinstance(edit.get("mode"), str):
                raise ToolArgumentError()
            mode = edit["mode"]
            try:
                relative = self.policy.normalize(edit.get("path"), writing=True)
                path = self.policy.resolve_write_file(
                    relative,
                    creating=mode == "create",
                )
            except WorkspacePathError as exc:
                raise _path_error(exc) from exc
            if relative in seen:
                raise ToolArgumentError()
            seen.add(relative)
            if mode == "create":
                if set(edit) != {"mode", "path", "content"} or not isinstance(
                    edit.get("content"), str
                ):
                    raise ToolArgumentError()
                before = None
                after = edit["content"].encode("utf-8")
                file_mode = 0o644
            elif mode == "replace":
                if set(edit) != {
                    "mode",
                    "path",
                    "expected_sha256",
                    "old_text",
                    "new_text",
                }:
                    raise ToolArgumentError()
                old_text = edit.get("old_text")
                new_text = edit.get("new_text")
                expected_hash = edit.get("expected_sha256")
                if (
                    not isinstance(old_text, str)
                    or not old_text
                    or not isinstance(new_text, str)
                    or not isinstance(expected_hash, str)
                ):
                    raise ToolArgumentError()
                before, current = _read_text(path, max_bytes=MAX_EDIT_FILE_BYTES)
                if hashlib.sha256(before).hexdigest() != expected_hash:
                    raise ToolRejectedError(ToolErrorCode.WORKSPACE_CONFLICT)
                if current.count(old_text) != 1:
                    raise ToolRejectedError(ToolErrorCode.WORKSPACE_CONFLICT)
                after = current.replace(old_text, new_text, 1).encode("utf-8")
                file_mode = stat.S_IMODE(path.stat().st_mode)
            else:
                raise ToolArgumentError()
            if b"\x00" in after or len(after) > MAX_EDIT_FILE_BYTES or before == after:
                raise ToolArgumentError()
            changes.append(_FileChange(path, relative, before, after, file_mode))
        changes.sort(key=lambda item: item.relative)
        diff = "".join(
            _diff(item.relative, item.before, item.after) for item in changes
        )
        if not diff or len(diff.encode("utf-8")) > MAX_DIFF_BYTES:
            raise ToolArgumentError()
        return changes, diff


class UndoWorkspaceChangeTool:
    definition = ToolDefinition(
        "undo_workspace_change",
        "撤销当前进程中最近一次工作区修改；执行前必须审批",
        {
            "type": "object",
            "properties": {"change_id": {"type": "string"}},
            "required": ["change_id"],
            "additionalProperties": False,
        },
        effect=ToolEffect.MUTATING,
    )

    def __init__(
        self,
        policy: WorkspacePolicy,
        journal: WorkspaceChangeJournal,
    ) -> None:
        self.policy = policy
        self.journal = journal
        self._lock = asyncio.Lock()
        self._approved: dict[str, str] = {}

    async def preview(
        self,
        call_id: str,
        arguments: Mapping[str, object],
    ) -> ToolApprovalRequest:
        async with self._lock:
            change, diff = self._plan(arguments)
            fingerprint = _fingerprint(self.definition.name, arguments, diff)
            self._approved[_argument_digest(arguments)] = fingerprint
            return ToolApprovalRequest(
                call_id,
                self.definition.name,
                "撤销最近一次工作区修改",
                tuple(item.relative for item in change.files),
                diff,
                fingerprint,
            )

    async def invoke(self, arguments: Mapping[str, object]) -> object:
        async with self._lock:
            change, diff = self._plan(arguments)
            expected = self._approved.pop(_argument_digest(arguments), None)
            if expected != _fingerprint(self.definition.name, arguments, diff):
                raise ToolRejectedError(ToolErrorCode.WORKSPACE_CONFLICT)
            _undo_files(change.files)
            self.journal.pop(change.change_id)
            return {
                "change_id": change.change_id,
                "undone": True,
                "paths": [item.relative for item in change.files],
            }

    def _plan(self, arguments: Mapping[str, object]) -> tuple[WorkspaceChange, str]:
        _arguments(arguments, {"change_id"})
        change_id = arguments.get("change_id")
        if not isinstance(change_id, str) or not change_id:
            raise ToolArgumentError()
        change = self.journal.latest
        if change is None or change.change_id != change_id:
            raise ToolRejectedError(ToolErrorCode.WORKSPACE_CONFLICT)
        for item, expected_hash in zip(change.files, change.result_hashes):
            try:
                self.policy.resolve_write_file(item.relative, creating=False)
                current = item.path.read_bytes()
            except (OSError, WorkspacePathError) as exc:
                raise ToolRejectedError(ToolErrorCode.WORKSPACE_CONFLICT) from exc
            if hashlib.sha256(current).hexdigest() != expected_hash:
                raise ToolRejectedError(ToolErrorCode.WORKSPACE_CONFLICT)
        diff = "".join(
            _diff(item.relative, item.after, item.before) for item in change.files
        )
        if len(diff.encode("utf-8")) > MAX_DIFF_BYTES:
            raise ToolArgumentError()
        return change, diff


def create_workspace_registry(
    policy: WorkspacePolicy,
    journal: WorkspaceChangeJournal | None = None,
    skill_catalog: "SkillCatalog | None" = None,
):
    """创建仅由 TUI 入口显式加载的固定 Workspace 工具白名单。"""

    # 延迟导入打破 Workspace Policy 与固定检查实现之间的模块环。
    from tools.builtin import GetCurrentTimeTool
    from tools.project_checks import RunProjectCheckTool
    from tools.registry import ToolRegistry

    active_journal = WorkspaceChangeJournal() if journal is None else journal
    tools = [
            GetCurrentTimeTool(),
            ListWorkspaceFilesTool(policy),
            SearchWorkspaceTextTool(policy),
            ReadWorkspaceFileTool(policy),
            GetWorkspaceGitStatusTool(policy),
            GetWorkspaceGitDiffTool(policy),
            ApplyWorkspaceEditsTool(policy, active_journal),
            RunProjectCheckTool(policy),
            UndoWorkspaceChangeTool(policy, active_journal),
    ]
    if skill_catalog is not None and skill_catalog.count:
        from tools.skills import (
            LoadSkillTool,
            ReadSkillResourceTool,
            RunSkillScriptTool,
        )

        tools.extend(
            (
                LoadSkillTool(skill_catalog),
                ReadSkillResourceTool(skill_catalog),
                RunSkillScriptTool(skill_catalog),
            )
        )
    return ToolRegistry(tuple(tools))


def _path_error(error: WorkspacePathError) -> Exception:
    if error.protected:
        return ToolRejectedError(ToolErrorCode.PROTECTED_PATH)
    return ToolArgumentError()


def _argument_digest(arguments: Mapping[str, object]) -> str:
    serialized = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(serialized).hexdigest()


def _fingerprint(tool_name: str, arguments: Mapping[str, object], diff: str) -> str:
    value = tool_name + "\x00" + _argument_digest(arguments) + "\x00" + diff
    return hashlib.sha256(value.encode()).hexdigest()


def _diff(relative: str, before: bytes | None, after: bytes | None) -> str:
    old = "" if before is None else before.decode("utf-8")
    new = "" if after is None else after.decode("utf-8")
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{relative}",
            tofile=f"b/{relative}",
        )
    )


def _write_atomic(path: Path, content: bytes, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".agent-edit-",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _commit_changes(changes: list[_FileChange]) -> None:
    # 在准备落盘前统一重验，缩小审批后外部修改造成的竞态窗口。
    for item in changes:
        if item.before is None:
            if item.path.exists() or item.path.is_symlink():
                raise ToolRejectedError(ToolErrorCode.WORKSPACE_CONFLICT)
        else:
            try:
                current = item.path.read_bytes()
            except OSError as exc:
                raise ToolRejectedError(ToolErrorCode.WORKSPACE_CONFLICT) from exc
            if hashlib.sha256(current).digest() != hashlib.sha256(item.before).digest():
                raise ToolRejectedError(ToolErrorCode.WORKSPACE_CONFLICT)
    applied: list[_FileChange] = []
    try:
        for item in changes:
            if item.before is None:
                _write_new_file(item.path, item.after, item.mode)
            else:
                _write_atomic(item.path, item.after, item.mode)
            applied.append(item)
    except Exception:
        _restore_changes(applied)
        raise


def _restore_changes(changes) -> None:
    for item in reversed(tuple(changes)):
        if item.before is None:
            try:
                item.path.unlink()
            except FileNotFoundError:
                pass
        else:
            _write_atomic(item.path, item.before, item.mode)


def _undo_files(changes) -> None:
    """撤销批次；若中途失败，则把已撤销文件恢复到撤销前状态。"""

    applied: list[_FileChange] = []
    try:
        for item in reversed(tuple(changes)):
            if item.before is None:
                item.path.unlink()
            else:
                _write_atomic(item.path, item.before, item.mode)
            applied.append(item)
    except Exception:
        for item in reversed(applied):
            if item.before is None:
                _write_new_file(item.path, item.after, item.mode)
            else:
                _write_atomic(item.path, item.after, item.mode)
        raise


def _write_new_file(path: Path, content: bytes, mode: int) -> None:
    """用硬链接发布新文件，确保竞态中绝不覆盖突然出现的目标。"""

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".agent-edit-",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise ToolRejectedError(ToolErrorCode.WORKSPACE_CONFLICT) from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
