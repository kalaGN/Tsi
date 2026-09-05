"""经逐次审批从受限来源原子安装项目 Skill。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol
from urllib.parse import quote, unquote, urlsplit

import httpx

from tools.contracts import (
    SKILL_INSTALL_APPROVAL_WARNING_TEXT,
    SkillInstallApprovalRequest,
    ToolArgumentError,
    ToolDefinition,
    ToolEffect,
    ToolErrorCode,
    ToolRejectedError,
)
from tools.skills import (
    MAX_RESOURCE_COUNT,
    MAX_RESOURCE_FILE_BYTES,
    MAX_RESOURCE_TOTAL_BYTES,
    MAX_SKILL_FILE_BYTES,
    SkillCatalog,
    SkillLoadError,
    load_skill_catalog,
)


MAX_INSTALL_FILES = MAX_RESOURCE_COUNT + 1
MAX_INSTALL_DIRECTORIES = 128
MAX_GITHUB_REQUESTS = 70
MAX_GITHUB_RESPONSE_BYTES = 256 * 1024
MAX_GITHUB_NETWORK_BYTES = 4 * 1024 * 1024
GITHUB_TIMEOUT_SECONDS = 30.0
_SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_GITHUB_COMPONENT_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_SOURCE_DIRECTORY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")


@dataclass(frozen=True)
class SkillInstallRequest:
    """校验后的安装参数和只用于展示的来源。"""

    source_type: str
    source: str
    expected_name: str
    source_display: str


@dataclass(frozen=True)
class GitHubSkillLocation:
    """从规范 GitHub 目录 URL 提取的固定仓库位置。"""

    owner: str
    repository: str
    ref: str
    directory: str
    display_url: str


class SkillSourceFetcher(Protocol):
    """把一个已校验来源物化到空候选目录。"""

    async def fetch(self, request: SkillInstallRequest, destination: Path) -> None:
        ...


class GitHubContentsFetcher:
    """匿名递归读取 GitHub Contents API，不信任响应中的下载地址。"""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def fetch(self, request: SkillInstallRequest, destination: Path) -> None:
        location = parse_github_skill_url(request.source)
        state = {"requests": 0, "network_bytes": 0, "files": 0, "dirs": 0}
        try:
            async with asyncio.timeout(GITHUB_TIMEOUT_SECONDS):
                if self._client is not None:
                    await self._fetch_entry(
                        self._client,
                        location,
                        location.directory,
                        destination,
                        state,
                    )
                    return
                timeout = httpx.Timeout(GITHUB_TIMEOUT_SECONDS)
                async with httpx.AsyncClient(
                    timeout=timeout,
                    follow_redirects=False,
                    trust_env=False,
                    headers={
                        "Accept": "application/vnd.github+json",
                        "User-Agent": "Tsi-Skill-Installer/1.0",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                ) as client:
                    await self._fetch_entry(
                        client,
                        location,
                        location.directory,
                        destination,
                        state,
                    )
        except TimeoutError as exc:
            raise ToolRejectedError(ToolErrorCode.SKILL_DOWNLOAD_TIMEOUT) from exc

    async def _fetch_entry(
        self,
        client: httpx.AsyncClient,
        location: GitHubSkillLocation,
        remote_path: str,
        destination: Path,
        state: dict[str, int],
    ) -> None:
        payload = await self._request_json(client, location, remote_path, state)
        if isinstance(payload, list):
            state["dirs"] += 1
            if state["dirs"] > MAX_INSTALL_DIRECTORIES:
                raise ToolRejectedError(ToolErrorCode.SKILL_PACKAGE_INVALID)
            destination.mkdir(parents=True, exist_ok=True)
            entries: list[tuple[str, str]] = []
            seen_names: set[str] = set()
            if len(payload) > MAX_INSTALL_FILES + MAX_INSTALL_DIRECTORIES:
                raise ToolRejectedError(ToolErrorCode.SKILL_PACKAGE_INVALID)
            for entry in payload:
                if not isinstance(entry, dict):
                    raise ToolRejectedError(ToolErrorCode.SKILL_PACKAGE_INVALID)
                name = entry.get("name")
                entry_type = entry.get("type")
                if (
                    not _valid_remote_name(name)
                    or name in seen_names
                    or entry_type not in {"file", "dir"}
                    or "target" in entry
                    or "submodule_git_url" in entry
                ):
                    raise ToolRejectedError(ToolErrorCode.SKILL_PACKAGE_INVALID)
                seen_names.add(name)
                entries.append((name, entry_type))
            for name, _entry_type in sorted(entries):
                child_remote = f"{remote_path}/{name}"
                await self._fetch_entry(
                    client,
                    location,
                    child_remote,
                    destination / name,
                    state,
                )
            return

        if (
            not isinstance(payload, dict)
            or payload.get("type") != "file"
            or "target" in payload
            or "submodule_git_url" in payload
        ):
            raise ToolRejectedError(ToolErrorCode.SKILL_PACKAGE_INVALID)
        content = payload.get("content")
        encoding = payload.get("encoding")
        if not isinstance(content, str) or encoding != "base64":
            raise ToolRejectedError(ToolErrorCode.SKILL_PACKAGE_INVALID)
        try:
            decoded = base64.b64decode("".join(content.split()), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ToolRejectedError(ToolErrorCode.SKILL_PACKAGE_INVALID) from exc
        state["files"] += 1
        if state["files"] > MAX_INSTALL_FILES:
            raise ToolRejectedError(ToolErrorCode.SKILL_PACKAGE_INVALID)
        _check_candidate_file_size(destination.name, decoded, state)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(decoded)

    async def _request_json(
        self,
        client: httpx.AsyncClient,
        location: GitHubSkillLocation,
        remote_path: str,
        state: dict[str, int],
    ) -> object:
        state["requests"] += 1
        if state["requests"] > MAX_GITHUB_REQUESTS:
            raise ToolRejectedError(ToolErrorCode.SKILL_PACKAGE_INVALID)
        url = (
            "https://api.github.com/repos/"
            f"{quote(location.owner, safe='')}/{quote(location.repository, safe='')}"
            f"/contents/{quote(remote_path, safe='/')}"
        )
        try:
            async with client.stream("GET", url, params={"ref": location.ref}) as response:
                if response.status_code != 200:
                    raise ToolRejectedError(ToolErrorCode.SKILL_SOURCE_UNAVAILABLE)
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    state["network_bytes"] += len(chunk)
                    if (
                        size > MAX_GITHUB_RESPONSE_BYTES
                        or state["network_bytes"] > MAX_GITHUB_NETWORK_BYTES
                    ):
                        raise ToolRejectedError(ToolErrorCode.SKILL_PACKAGE_INVALID)
                    chunks.append(chunk)
        except httpx.TimeoutException as exc:
            raise ToolRejectedError(ToolErrorCode.SKILL_DOWNLOAD_TIMEOUT) from exc
        except ToolRejectedError:
            raise
        except httpx.HTTPError as exc:
            raise ToolRejectedError(ToolErrorCode.SKILL_SOURCE_UNAVAILABLE) from exc
        try:
            return json.loads(b"".join(chunks))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ToolRejectedError(ToolErrorCode.SKILL_PACKAGE_INVALID) from exc


class LocalCodexSkillFetcher:
    """从启动时固定的个人 Codex Skill 根无跟随复制普通文件。"""

    def __init__(self, codex_skills_root: Path) -> None:
        self._root = Path(codex_skills_root)

    async def fetch(self, request: SkillInstallRequest, destination: Path) -> None:
        await asyncio.to_thread(
            _copy_local_skill,
            self._root,
            request.source,
            destination,
        )


class SkillInstaller:
    """串行执行候选获取、原子提交、全量刷新和失败回滚。"""

    def __init__(
        self,
        startup_directory: Path,
        publisher: Callable[[SkillCatalog], int],
        *,
        codex_skills_root: Path | None = None,
        github_fetcher: SkillSourceFetcher | None = None,
    ) -> None:
        self._root = Path(startup_directory).resolve(strict=True)
        self._publisher = publisher
        self._local_fetcher = LocalCodexSkillFetcher(
            codex_skills_root or (Path.home() / ".codex" / "skills")
        )
        self._github_fetcher = github_fetcher or GitHubContentsFetcher()
        self._lock = asyncio.Lock()

    async def install(self, request: SkillInstallRequest) -> dict[str, object]:
        async with self._lock:
            skills_root, skills_root_identity = _prepare_skill_parent(self._root)
            target = skills_root / request.expected_name
            if target.exists() or target.is_symlink():
                raise ToolRejectedError(ToolErrorCode.SKILL_ALREADY_EXISTS)
            temporary_root = Path(
                tempfile.mkdtemp(prefix=".tsi-skill-install-", dir=self._root)
            )
            candidate = (
                temporary_root / ".agents" / "skills" / request.expected_name
            )
            installed_identity: tuple[int, int] | None = None
            try:
                fetcher = (
                    self._github_fetcher
                    if request.source_type == "github"
                    else self._local_fetcher
                )
                await fetcher.fetch(request, candidate)
                try:
                    catalog = load_skill_catalog(temporary_root)
                except SkillLoadError as exc:
                    raise ToolRejectedError(
                        ToolErrorCode.SKILL_PACKAGE_INVALID
                    ) from exc
                if (
                    catalog.count != 1
                    or request.expected_name not in catalog.skills
                ):
                    raise ToolRejectedError(ToolErrorCode.SKILL_PACKAGE_INVALID)

                current_skills_root, current_identity = _prepare_skill_parent(
                    self._root
                )
                if (
                    current_identity != skills_root_identity
                    or current_skills_root != skills_root
                ):
                    raise ToolRejectedError(ToolErrorCode.SKILL_REFRESH_FAILED)
                if target.exists() or target.is_symlink():
                    raise ToolRejectedError(ToolErrorCode.SKILL_ALREADY_EXISTS)
                try:
                    candidate.rename(target)
                except FileExistsError as exc:
                    raise ToolRejectedError(
                        ToolErrorCode.SKILL_ALREADY_EXISTS
                    ) from exc
                info = target.lstat()
                installed_identity = (info.st_dev, info.st_ino)
                try:
                    project_catalog = load_skill_catalog(self._root)
                    version = self._publisher(project_catalog)
                except Exception as exc:
                    _remove_installed_target(
                        target,
                        installed_identity,
                        skills_root_identity,
                    )
                    installed_identity = None
                    raise ToolRejectedError(
                        ToolErrorCode.SKILL_REFRESH_FAILED
                    ) from exc
                return {
                    "name": request.expected_name,
                    "path": f".agents/skills/{request.expected_name}",
                    "source_type": request.source_type,
                    "runtime_version": version,
                    "active_from": "next_request",
                }
            finally:
                shutil.rmtree(temporary_root, ignore_errors=True)


class InstallSkillTool:
    """向模型暴露受审批保护的 Skill 安装用例。"""

    definition = ToolDefinition(
        name="install_skill",
        description=(
            "从公开 GitHub Skill 目录或 ~/.codex/skills 直属目录安装 Codex Skill；"
            "每次安装都需用户审批，成功后下一次请求生效"
        ),
        parameters={
            "type": "object",
            "properties": {
                "source_type": {
                    "type": "string",
                    "enum": ["github", "codex_home"],
                },
                "source": {"type": "string"},
                "expected_name": {"type": "string"},
            },
            "required": ["source_type", "source", "expected_name"],
            "additionalProperties": False,
        },
        effect=ToolEffect.MUTATING,
    )

    def __init__(self, installer: SkillInstaller) -> None:
        self._installer = installer
        self._approved_fingerprints: dict[str, str] = {}

    async def preview(
        self,
        call_id: str,
        arguments: Mapping[str, object],
    ) -> SkillInstallApprovalRequest:
        request = parse_install_request(arguments)
        argument_key, fingerprint = _install_fingerprint(request)
        self._approved_fingerprints[argument_key] = fingerprint
        return SkillInstallApprovalRequest(
            call_id=call_id,
            tool_name=self.definition.name,
            title="安装 Skill",
            source_type=request.source_type,
            source_display=request.source_display,
            target_path=f".agents/skills/{request.expected_name}",
            network_access=request.source_type == "github",
            warning_text=SKILL_INSTALL_APPROVAL_WARNING_TEXT,
            fingerprint=fingerprint,
        )

    async def invoke(self, arguments: Mapping[str, object]) -> object:
        request = parse_install_request(arguments)
        argument_key, fingerprint = _install_fingerprint(request)
        approved = self._approved_fingerprints.pop(argument_key, None)
        if approved != fingerprint:
            raise ToolRejectedError(ToolErrorCode.WORKSPACE_CONFLICT)
        return await self._installer.install(request)


def parse_install_request(arguments: Mapping[str, object]) -> SkillInstallRequest:
    """严格区分 URL 和个人目录名，不允许自由路径解释。"""

    if set(arguments) != {"source_type", "source", "expected_name"}:
        raise ToolArgumentError()
    source_type = arguments.get("source_type")
    source = arguments.get("source")
    expected_name = arguments.get("expected_name")
    if (
        source_type not in {"github", "codex_home"}
        or not isinstance(source, str)
        or not source
        or len(source.encode("utf-8")) > 2048
        or not isinstance(expected_name, str)
        or not _SKILL_NAME_PATTERN.fullmatch(expected_name)
    ):
        raise ToolArgumentError()
    if source_type == "github":
        location = parse_github_skill_url(source)
        source_display = location.display_url
    else:
        if not _SOURCE_DIRECTORY_PATTERN.fullmatch(source) or source in {".", ".."}:
            raise ToolArgumentError()
        source_display = f"~/.codex/skills/{source}"
    return SkillInstallRequest(
        source_type=source_type,
        source=source,
        expected_name=expected_name,
        source_display=source_display,
    )


def parse_github_skill_url(value: str) -> GitHubSkillLocation:
    """只解析公开 GitHub 的规范 Skill 目录 URL。"""

    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ToolArgumentError() from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.netloc != "github.com"
        or parsed.query
        or parsed.fragment
        or "//" in parsed.path
    ):
        raise ToolArgumentError()
    try:
        parts = tuple(unquote(part) for part in parsed.path.split("/") if part)
    except UnicodeDecodeError as exc:
        raise ToolArgumentError() from exc
    if len(parts) < 5 or parts[2] != "tree":
        raise ToolArgumentError()
    owner, repository, _, ref, *directory_parts = parts
    if (
        owner in {".", ".."}
        or repository in {".", ".."}
        or ref in {".", ".."}
        or not _GITHUB_COMPONENT_PATTERN.fullmatch(owner)
        or not _GITHUB_COMPONENT_PATTERN.fullmatch(repository)
        or not _GITHUB_COMPONENT_PATTERN.fullmatch(ref)
        or not directory_parts
        or any(not _valid_remote_name(part) for part in directory_parts)
    ):
        raise ToolArgumentError()
    directory = PurePosixPath(*directory_parts).as_posix()
    display_url = f"https://github.com/{owner}/{repository}/tree/{ref}/{directory}"
    return GitHubSkillLocation(owner, repository, ref, directory, display_url)


def _copy_local_skill(root: Path, source_name: str, destination: Path) -> None:
    try:
        root_info = root.lstat()
    except OSError as exc:
        raise ToolRejectedError(ToolErrorCode.SKILL_SOURCE_UNAVAILABLE) from exc
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ToolRejectedError(ToolErrorCode.SKILL_SOURCE_UNAVAILABLE)
    state = {"files": 0, "dirs": 0, "resource_bytes": 0}
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        root_descriptor = os.open(root, directory_flags)
        try:
            source_descriptor = os.open(
                source_name,
                directory_flags,
                dir_fd=root_descriptor,
            )
        finally:
            os.close(root_descriptor)
    except OSError as exc:
        raise ToolRejectedError(ToolErrorCode.SKILL_SOURCE_UNAVAILABLE) from exc
    try:
        _copy_plain_directory(source_descriptor, destination, state)
    finally:
        os.close(source_descriptor)


def _copy_plain_directory(
    source_descriptor: int,
    destination: Path,
    state: dict[str, int],
) -> None:
    state["dirs"] += 1
    if state["dirs"] > MAX_INSTALL_DIRECTORIES:
        raise ToolRejectedError(ToolErrorCode.SKILL_PACKAGE_INVALID)
    destination.mkdir(parents=True, exist_ok=False)
    try:
        entries = []
        with os.scandir(source_descriptor) as iterator:
            for entry in iterator:
                entries.append(entry.name)
                if len(entries) > MAX_INSTALL_FILES + MAX_INSTALL_DIRECTORIES:
                    raise ToolRejectedError(
                        ToolErrorCode.SKILL_PACKAGE_INVALID
                    )
        entries.sort()
    except ToolRejectedError:
        raise
    except OSError as exc:
        raise ToolRejectedError(ToolErrorCode.SKILL_SOURCE_UNAVAILABLE) from exc
    for name in entries:
        if not _valid_remote_name(name):
            raise ToolRejectedError(ToolErrorCode.SKILL_PACKAGE_INVALID)
        try:
            info = os.stat(name, dir_fd=source_descriptor, follow_symlinks=False)
        except OSError as exc:
            raise ToolRejectedError(ToolErrorCode.SKILL_SOURCE_UNAVAILABLE) from exc
        target = destination / name
        if stat.S_ISLNK(info.st_mode):
            raise ToolRejectedError(ToolErrorCode.SKILL_PACKAGE_INVALID)
        if stat.S_ISDIR(info.st_mode):
            try:
                child_descriptor = os.open(
                    name,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=source_descriptor,
                )
            except OSError as exc:
                raise ToolRejectedError(
                    ToolErrorCode.SKILL_SOURCE_UNAVAILABLE
                ) from exc
            try:
                _copy_plain_directory(child_descriptor, target, state)
            finally:
                os.close(child_descriptor)
        elif stat.S_ISREG(info.st_mode):
            content = _read_plain_file(source_descriptor, name, info)
            state["files"] += 1
            if state["files"] > MAX_INSTALL_FILES:
                raise ToolRejectedError(ToolErrorCode.SKILL_PACKAGE_INVALID)
            _check_candidate_file_size(name, content, state)
            target.write_bytes(content)
        else:
            raise ToolRejectedError(ToolErrorCode.SKILL_PACKAGE_INVALID)


def _read_plain_file(
    parent_descriptor: int,
    name: str,
    before: os.stat_result,
) -> bytes:
    limit = MAX_SKILL_FILE_BYTES if name == "SKILL.md" else MAX_RESOURCE_FILE_BYTES
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
            ):
                raise ToolRejectedError(ToolErrorCode.SKILL_PACKAGE_INVALID)
            content = os.read(descriptor, limit + 1)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except ToolRejectedError:
        raise
    except OSError as exc:
        raise ToolRejectedError(ToolErrorCode.SKILL_SOURCE_UNAVAILABLE) from exc
    if (
        len(content) > limit
        or opened.st_size != len(content)
        or opened.st_size != after.st_size
        or opened.st_mtime_ns != after.st_mtime_ns
    ):
        raise ToolRejectedError(ToolErrorCode.SKILL_PACKAGE_INVALID)
    return content


def _check_candidate_file_size(
    filename: str,
    content: bytes,
    state: dict[str, int],
) -> None:
    limit = MAX_SKILL_FILE_BYTES if filename == "SKILL.md" else MAX_RESOURCE_FILE_BYTES
    if len(content) > limit:
        raise ToolRejectedError(ToolErrorCode.SKILL_PACKAGE_INVALID)
    if filename != "SKILL.md":
        state["resource_bytes"] = state.get("resource_bytes", 0) + len(content)
        if state["resource_bytes"] > MAX_RESOURCE_TOTAL_BYTES:
            raise ToolRejectedError(ToolErrorCode.SKILL_PACKAGE_INVALID)


def _prepare_skill_parent(root: Path) -> tuple[Path, tuple[int, int]]:
    """创建或验证固定父目录，拒绝通过 `.agents` 符号链接逃逸。"""

    current = root
    try:
        for component in (".agents", "skills"):
            current = current / component
            try:
                current.mkdir(mode=0o755)
            except FileExistsError:
                pass
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ToolRejectedError(ToolErrorCode.SKILL_REFRESH_FAILED)
        final_info = current.lstat()
    except ToolRejectedError:
        raise
    except OSError as exc:
        raise ToolRejectedError(ToolErrorCode.SKILL_REFRESH_FAILED) from exc
    return current, (final_info.st_dev, final_info.st_ino)


def _remove_installed_target(
    target: Path,
    identity: tuple[int, int],
    parent_identity: tuple[int, int],
) -> None:
    """只回滚本次 rename 的目录，避免误删并发替换后的用户内容。"""

    try:
        parent_info = target.parent.lstat()
        if (
            (parent_info.st_dev, parent_info.st_ino) != parent_identity
            or stat.S_ISLNK(parent_info.st_mode)
            or not stat.S_ISDIR(parent_info.st_mode)
        ):
            return
        info = target.lstat()
        if (info.st_dev, info.st_ino) != identity or not stat.S_ISDIR(info.st_mode):
            return
        shutil.rmtree(target)
    except OSError:
        return


def _valid_remote_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value not in {".", ".."}
        and len(value.encode("utf-8")) <= 255
        and "/" not in value
        and "\\" not in value
        and "\x00" not in value
    )


def _install_fingerprint(request: SkillInstallRequest) -> tuple[str, str]:
    canonical = json.dumps(
        {
            "source_type": request.source_type,
            "source": request.source,
            "expected_name": request.expected_name,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    argument_key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    fingerprint = hashlib.sha256(
        f"install_skill\x00{canonical}\x00{SKILL_INSTALL_APPROVAL_WARNING_TEXT}".encode(
            "utf-8"
        )
    ).hexdigest()
    return argument_key, fingerprint
