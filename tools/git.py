"""TUI 专用的受控 Git 暂存、提交和推送工具。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import signal
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit

from tools.contracts import (
    GIT_APPROVAL_WARNING_TEXT,
    GitApprovalRequest,
    ToolArgumentError,
    ToolDefinition,
    ToolEffect,
    ToolErrorCode,
    ToolRejectedError,
)
from tools.workspace import WorkspacePathError, WorkspacePolicy


MAX_GIT_OUTPUT_BYTES = 64 * 1024
MAX_GIT_PATHS = 20
GIT_TIMEOUT_SECONDS = 30
_COMMIT_PATTERN = re.compile(
    r"^(feat|fix|refactor|docs|test|chore|perf|build|ci|revert): (?=.*[\u4e00-\u9fff]).{1,180}$"
)
_REMOTE_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_REMOTE_BRANCH_PATTERN = re.compile(
    r"^(?![./])(?!.*(?:\.\.|//|@\{|\\|[ ~^:?*\[]))(?!.*(?:/|\.)$)"
    r"[A-Za-z0-9._/-]{1,240}$"
)
_SCP_REMOTE_PATTERN = re.compile(
    r"^(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9.-]+:[^\s]+$"
)


class GitCommandRunner:
    """以固定 cwd、最小环境和有界输出执行宿主构造的 Git argv。"""

    def __init__(self, policy: WorkspacePolicy) -> None:
        self.policy = policy

    async def run(
        self,
        arguments: Sequence[str],
        *,
        extra_env: Mapping[str, str] | None = None,
        allow_failure: bool = False,
    ) -> str:
        git = shutil.which("git")
        if git is None:
            raise ToolRejectedError(ToolErrorCode.GIT_UNAVAILABLE)
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "LANG": "C.UTF-8",
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
        }
        # 允许 Git 使用用户已有凭据配置，但不继承可改写 Git 行为的环境变量。
        for name in ("HOME", "SSH_AUTH_SOCK", "TMPDIR"):
            if os.environ.get(name):
                environment[name] = os.environ[name]
        if extra_env:
            environment.update(extra_env)
        fixed = (
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgSign=false",
            "-c",
            "core.pager=cat",
            "-c",
            "color.ui=false",
            "-c",
            "core.quotePath=false",
            "-c",
            "core.sshCommand=ssh",
        )
        try:
            process = await asyncio.create_subprocess_exec(
                git,
                *fixed,
                *arguments,
                cwd=self.policy.root,
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            raise ToolRejectedError(ToolErrorCode.GIT_UNAVAILABLE) from exc
        try:
            output = await asyncio.wait_for(
                process.communicate(), timeout=GIT_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError as exc:
            await _stop_process(process)
            raise ToolRejectedError(ToolErrorCode.GIT_FAILED) from exc
        except asyncio.CancelledError:
            await _stop_process(process)
            raise
        content = output[0]
        if len(content) > MAX_GIT_OUTPUT_BYTES:
            raise ToolRejectedError(ToolErrorCode.GIT_FAILED)
        if process.returncode != 0 and not allow_failure:
            raise ToolRejectedError(ToolErrorCode.GIT_FAILED)
        return content.decode("utf-8", errors="replace")


class GitStageTool:
    definition = ToolDefinition(
        "git_stage",
        "暂存明确指定的已修改或未跟踪文件；执行前必须审批",
        {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": MAX_GIT_PATHS,
                }
            },
            "required": ["paths"],
            "additionalProperties": False,
        },
        effect=ToolEffect.EXECUTING,
    )

    def __init__(
        self,
        policy: WorkspacePolicy,
        runner: GitCommandRunner | None = None,
    ) -> None:
        self.policy = policy
        self.runner = runner or GitCommandRunner(policy)
        self._approved: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def preview(
        self,
        call_id: str,
        arguments: Mapping[str, object],
    ) -> GitApprovalRequest:
        async with self._lock:
            paths, fingerprint, diff = await self._plan(arguments)
            self._approved[_argument_digest(arguments)] = fingerprint
            return GitApprovalRequest(
                call_id,
                self.definition.name,
                "暂存 Git 文件",
                "stage",
                "文件：" + "、".join(paths),
                diff,
                GIT_APPROVAL_WARNING_TEXT,
                False,
                fingerprint,
            )

    async def invoke(self, arguments: Mapping[str, object]) -> object:
        async with self._lock:
            expected = self._approved.pop(_argument_digest(arguments), None)
            if expected is None:
                raise ToolRejectedError(ToolErrorCode.GIT_CONFLICT)
            try:
                paths, fingerprint, _ = await self._plan(arguments)
            except ToolRejectedError as exc:
                raise ToolRejectedError(ToolErrorCode.GIT_CONFLICT) from exc
            if expected != fingerprint:
                raise ToolRejectedError(ToolErrorCode.GIT_CONFLICT)
            await self.runner.run(("add", "--", *paths))
            return {"paths": list(paths), "staged": True}

    async def _plan(self, arguments: Mapping[str, object]):
        paths = _stage_paths(self.policy, arguments)
        index_path = await _index_path(self.policy, self.runner)
        index_digest = _file_digest(index_path)
        before = await self.runner.run(
            ("diff", "--cached", "--no-ext-diff", "--no-color", "--", *paths)
        )
        with tempfile.TemporaryDirectory(prefix="tsi-git-index-") as directory:
            temporary_index = Path(directory) / "index"
            if index_path.is_file():
                shutil.copyfile(index_path, temporary_index)
            environment = {"GIT_INDEX_FILE": str(temporary_index)}
            await self.runner.run(("add", "--", *paths), extra_env=environment)
            after = await self.runner.run(
                ("diff", "--cached", "--no-ext-diff", "--no-color", "--", *paths),
                extra_env=environment,
            )
        if after == before:
            raise ToolRejectedError(ToolErrorCode.GIT_NOTHING_TO_STAGE)
        if not after:
            raise ToolRejectedError(ToolErrorCode.GIT_NOTHING_TO_STAGE)
        state = [index_digest, after]
        state.extend(f"{path}:{_file_digest(self.policy.root / path)}" for path in paths)
        return paths, _digest("\0".join(state)), after


class GitCommitTool:
    definition = ToolDefinition(
        "git_commit",
        "用符合 type: 中文描述 的信息提交当前暂存内容；执行前必须审批",
        {
            "type": "object",
            "properties": {"message": {"type": "string", "minLength": 6, "maxLength": 200}},
            "required": ["message"],
            "additionalProperties": False,
        },
        effect=ToolEffect.EXECUTING,
    )

    def __init__(
        self,
        policy: WorkspacePolicy,
        runner: GitCommandRunner | None = None,
    ) -> None:
        self.policy = policy
        self.runner = runner or GitCommandRunner(policy)
        self._approved: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def preview(
        self,
        call_id: str,
        arguments: Mapping[str, object],
    ) -> GitApprovalRequest:
        async with self._lock:
            message, fingerprint, summary, diff = await self._plan(arguments)
            self._approved[_argument_digest(arguments)] = fingerprint
            return GitApprovalRequest(
                call_id,
                self.definition.name,
                "创建 Git 提交",
                "commit",
                summary,
                f"提交信息：{message}\n\n{diff}",
                GIT_APPROVAL_WARNING_TEXT,
                False,
                fingerprint,
            )

    async def invoke(self, arguments: Mapping[str, object]) -> object:
        async with self._lock:
            expected = self._approved.pop(_argument_digest(arguments), None)
            if expected is None:
                raise ToolRejectedError(ToolErrorCode.GIT_CONFLICT)
            try:
                message, fingerprint, summary, _ = await self._plan(arguments)
            except ToolRejectedError as exc:
                raise ToolRejectedError(ToolErrorCode.GIT_CONFLICT) from exc
            if expected != fingerprint:
                raise ToolRejectedError(ToolErrorCode.GIT_CONFLICT)
            await self.runner.run(("commit", "--no-verify", "-m", message))
            commit = (
                await self.runner.run(("rev-parse", "--short=12", "HEAD"))
            ).strip()
            branch = summary.removeprefix("分支：").split(" · ", 1)[0]
            return {"commit": commit, "branch": branch, "message": message}

    async def _plan(self, arguments: Mapping[str, object]):
        message = _commit_message(arguments)
        branch = (await self.runner.run(("symbolic-ref", "--short", "HEAD"))).strip()
        head = (
            await self.runner.run(("rev-parse", "HEAD"), allow_failure=True)
        ).strip()
        names = (
            await self.runner.run(
                ("diff", "--cached", "--name-only", "--no-renames")
            )
        ).splitlines()
        diff = await self.runner.run(
            ("diff", "--cached", "--no-ext-diff", "--no-color")
        )
        if not names or not diff:
            raise ToolRejectedError(ToolErrorCode.GIT_NOTHING_TO_COMMIT)
        index_digest = _file_digest(await _index_path(self.policy, self.runner))
        fingerprint = _digest("\0".join((message, branch, head, index_digest, diff)))
        return message, fingerprint, f"分支：{branch} · 文件：{'、'.join(names)}", diff


class GitPushTool:
    definition = ToolDefinition(
        "git_push",
        "非强制推送当前分支到已配置上游；执行前必须审批并访问网络",
        {"type": "object", "properties": {}, "additionalProperties": False},
        effect=ToolEffect.EXECUTING,
    )

    def __init__(
        self,
        policy: WorkspacePolicy,
        runner: GitCommandRunner | None = None,
    ) -> None:
        self.policy = policy
        self.runner = runner or GitCommandRunner(policy)
        self._approved: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def preview(
        self,
        call_id: str,
        arguments: Mapping[str, object],
    ) -> GitApprovalRequest:
        async with self._lock:
            plan = await self._plan(arguments)
            self._approved[_argument_digest(arguments)] = plan[0]
            return GitApprovalRequest(
                call_id, self.definition.name, "推送 Git 分支", "push",
                plan[1], plan[2], GIT_APPROVAL_WARNING_TEXT, True, plan[0],
            )

    async def invoke(self, arguments: Mapping[str, object]) -> object:
        async with self._lock:
            expected = self._approved.pop(_argument_digest(arguments), None)
            if expected is None:
                raise ToolRejectedError(ToolErrorCode.GIT_CONFLICT)
            try:
                plan = await self._plan(arguments)
            except ToolRejectedError as exc:
                raise ToolRejectedError(ToolErrorCode.GIT_CONFLICT) from exc
            fingerprint, _, _, remote, remote_branch, commits = plan
            if expected != fingerprint:
                raise ToolRejectedError(ToolErrorCode.GIT_CONFLICT)
            await self.runner.run(
                (
                    "push",
                    "--porcelain",
                    "--",
                    remote,
                    f"HEAD:refs/heads/{remote_branch}",
                )
            )
            return {"remote": remote, "branch": remote_branch, "commits": len(commits)}

    async def _plan(self, arguments: Mapping[str, object]):
        if arguments:
            raise ToolArgumentError()
        branch = (await self.runner.run(("symbolic-ref", "--short", "HEAD"))).strip()
        remote = (
            await self.runner.run(
                ("config", "--get", f"branch.{branch}.remote"),
                allow_failure=True,
            )
        ).strip()
        merge = (
            await self.runner.run(
                ("config", "--get", f"branch.{branch}.merge"),
                allow_failure=True,
            )
        ).strip()
        if (
            not _REMOTE_PATTERN.fullmatch(remote)
            or remote.startswith("-")
            or not merge.startswith("refs/heads/")
        ):
            raise ToolRejectedError(ToolErrorCode.GIT_NO_UPSTREAM)
        remote_branch = merge.removeprefix("refs/heads/")
        if not _REMOTE_BRANCH_PATTERN.fullmatch(remote_branch):
            raise ToolRejectedError(ToolErrorCode.GIT_NO_UPSTREAM)
        remote_url = (
            await self.runner.run(("remote", "get-url", "--push", remote))
        ).strip()
        display = _safe_remote_display(remote_url)
        upstream = f"{remote}/{remote_branch}"
        commits = tuple(
            filter(
                None,
                (
                    await self.runner.run(
                        ("rev-list", "--reverse", f"{upstream}..HEAD")
                    )
                ).splitlines(),
            )
        )
        if not commits:
            raise ToolRejectedError(ToolErrorCode.GIT_NOTHING_TO_PUSH)
        head = (await self.runner.run(("rev-parse", "HEAD"))).strip()
        log = await self.runner.run(("log", "--format=%h %s", f"{upstream}..HEAD"))
        log = "\n".join(_safe_line(line) for line in log.splitlines())
        fingerprint = _digest(
            "\0".join((head, upstream, _digest(remote_url), *commits))
        )
        summary = f"分支：{branch} → {upstream} · 远端：{display}"
        return fingerprint, summary, log, remote, remote_branch, commits


def _stage_paths(policy: WorkspacePolicy, arguments: Mapping[str, object]) -> tuple[str, ...]:
    if set(arguments) != {"paths"}:
        raise ToolArgumentError()
    raw = arguments.get("paths")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_GIT_PATHS:
        raise ToolArgumentError()
    if any(not isinstance(value, str) for value in raw):
        raise ToolArgumentError()
    if len(set(raw)) != len(raw):
        raise ToolArgumentError()
    paths = []
    for value in raw:
        if value.startswith(("-", ":")) or any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            raise ToolArgumentError()
        try:
            target = policy.resolve_write_file(value, creating=False)
            relative = policy.relative(target)
        except WorkspacePathError as exc:
            code = ToolErrorCode.PROTECTED_PATH if exc.protected else None
            if code:
                raise ToolRejectedError(code) from exc
            raise ToolArgumentError() from exc
        paths.append(relative)
    return tuple(sorted(paths))


def _commit_message(arguments: Mapping[str, object]) -> str:
    if set(arguments) != {"message"}:
        raise ToolArgumentError()
    message = arguments.get("message")
    if (
        not isinstance(message, str)
        or not 6 <= len(message) <= 200
        or any(ord(character) < 32 or ord(character) == 127 for character in message)
        or not _COMMIT_PATTERN.fullmatch(message)
    ):
        raise ToolArgumentError()
    return message


async def _index_path(policy: WorkspacePolicy, runner: GitCommandRunner) -> Path:
    value = (await runner.run(("rev-parse", "--git-path", "index"))).strip()
    if not value or "\x00" in value:
        raise ToolRejectedError(ToolErrorCode.GIT_UNAVAILABLE)
    path = Path(value)
    return path if path.is_absolute() else policy.root / path


def _safe_remote_display(value: str) -> str:
    if "://" not in value and _SCP_REMOTE_PATTERN.fullmatch(value):
        host = value.split(":", 1)[0].rsplit("@", 1)[-1]
        return f"{host}:<redacted>"
    parsed = urlsplit(value)
    try:
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError as exc:
        raise ToolRejectedError(ToolErrorCode.GIT_REMOTE_UNSAFE) from exc
    if (
        parsed.scheme not in {"https", "ssh"}
        or not parsed.hostname
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ToolRejectedError(ToolErrorCode.GIT_REMOTE_UNSAFE)
    return f"{parsed.scheme}://{parsed.hostname}{port}/<redacted>"


def _argument_digest(arguments: Mapping[str, object]) -> str:
    import json

    return _digest(
        json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _file_digest(path: Path) -> str:
    try:
        if not path.is_file():
            return "missing"
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        raise ToolRejectedError(ToolErrorCode.GIT_UNAVAILABLE) from exc


def _digest(value: str | bytes) -> str:
    content = value.encode() if isinstance(value, str) else value
    return hashlib.sha256(content).hexdigest()


def _safe_line(value: str) -> str:
    return "".join(character if character.isprintable() else "?" for character in value)[:512]


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
        process.kill()
        await process.wait()
