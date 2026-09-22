"""本机 Web 项目索引：项目只保存名称和 Workspace 路径。"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from app.runtime.model_budget import strict_json
from tools.workspace import WorkspacePolicy


DEFAULT_PROJECT_ID = "0" * 32
MAX_PROJECTS = 100
MAX_PROJECT_NAME = 80
MAX_PROJECT_PATH = 1024
MAX_INDEX_BYTES = 256 * 1024


class WebProjectError(Exception):
    """项目配置不可用；不向页面暴露底层磁盘异常。"""


class WebProjectNotFound(Exception):
    """项目 ID 不存在。"""


@dataclass(frozen=True)
class WebProjectRecord:
    id: str
    name: str
    path: str

    def to_payload(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name, "path": self.path}


def _project_id(value: object) -> bool:
    return isinstance(value, str) and len(value) == 32 and all(char in "0123456789abcdef" for char in value)


def _project_name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("项目名称无效。")
    name = value.strip()
    if not name or len(name) > MAX_PROJECT_NAME:
        raise ValueError("项目名称长度须为 1–80 个字符。")
    try:
        name.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("项目名称无效。") from exc
    if any(ord(char) < 32 for char in name):
        raise ValueError("项目名称不能包含控制字符。")
    return name


def _path_text(value: object) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("项目路径无效。")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise ValueError("项目路径无效。") from exc
    if size > MAX_PROJECT_PATH:
        raise ValueError("项目路径无效。")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("请输入已有目录的绝对路径。")
    return path


def normalize_project_path(value: object) -> str:
    """校验用户显式选择的目录，并以真实路径持久化。"""

    path = _path_text(value)
    if path.is_symlink():
        raise ValueError("项目路径不能是符号链接。")
    try:
        root = WorkspacePolicy(path).root
    except (OSError, ValueError) as exc:
        raise ValueError("项目路径必须是已存在且可访问的目录。") from exc
    if root == Path(root.anchor) or root == Path.home().resolve():
        raise ValueError("不能将文件系统根目录或家目录设为项目。")
    return str(root)


class WebProjectCatalog:
    """原子保存项目元数据；不读取或移动项目内的用户文件。"""

    def __init__(self, path: Path, default_path: Path) -> None:
        self.path = Path(path)
        self._records: tuple[WebProjectRecord, ...] = ()
        if self.path.is_symlink():
            raise WebProjectError("项目配置文件不可用。")
        if self.path.exists():
            self._load()
        else:
            default = WebProjectRecord(DEFAULT_PROJECT_ID, default_path.name or "默认项目", normalize_project_path(str(default_path)))
            self._save((default,))

    @property
    def default(self) -> WebProjectRecord:
        return self.require(DEFAULT_PROJECT_ID)

    def list_records(self) -> tuple[WebProjectRecord, ...]:
        return self._records

    def require(self, project_id: str) -> WebProjectRecord:
        if not _project_id(project_id):
            raise WebProjectNotFound("项目不存在。")
        record = next((item for item in self._records if item.id == project_id), None)
        if record is None:
            raise WebProjectNotFound("项目不存在。")
        return record

    def create(self, name: str, path: str) -> WebProjectRecord:
        if len(self._records) >= MAX_PROJECTS:
            raise ValueError("项目数量已达上限。")
        normalized = normalize_project_path(path)
        if any(item.path == normalized for item in self._records):
            raise ValueError("该路径已添加为项目。")
        record = WebProjectRecord(uuid.uuid4().hex, _project_name(name), normalized)
        self._save((*self._records, record))
        return record

    def update(self, project_id: str, *, name: str, path: str) -> WebProjectRecord:
        previous = self.require(project_id)
        normalized = normalize_project_path(path)
        if any(item.id != project_id and item.path == normalized for item in self._records):
            raise ValueError("该路径已添加为项目。")
        updated = replace(previous, name=_project_name(name), path=normalized)
        self._save(tuple(updated if item.id == project_id else item for item in self._records))
        return updated

    def _load(self) -> None:
        try:
            if self.path.stat().st_size > MAX_INDEX_BYTES:
                raise ValueError("项目索引过大")
            payload = strict_json(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or set(payload) != {"version", "projects"} or type(payload["version"]) is not int or payload["version"] != 1:
                raise ValueError("项目索引版本无效")
            raw = payload["projects"]
            if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_PROJECTS:
                raise ValueError("项目索引列表无效")
            records = []
            for item in raw:
                if not isinstance(item, dict) or set(item) != {"id", "name", "path"} or not _project_id(item["id"]):
                    raise ValueError("项目记录无效")
                path = _path_text(item["path"])
                records.append(WebProjectRecord(item["id"], _project_name(item["name"]), str(path)))
            if (
                len({item.id for item in records}) != len(records)
                or len({item.path for item in records}) != len(records)
                or DEFAULT_PROJECT_ID not in {item.id for item in records}
            ):
                raise ValueError("项目索引引用无效")
        except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
            raise WebProjectError("项目配置文件不可用。") from exc
        self._records = tuple(records)

    def _save(self, records: tuple[WebProjectRecord, ...]) -> None:
        temporary: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor, name = tempfile.mkstemp(dir=self.path.parent, prefix=".web-projects.", suffix=".tmp")
            temporary = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "projects": [item.to_payload() for item in records]}, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            if self.path.is_symlink():
                raise OSError("project index is symlink")
            os.replace(temporary, self.path)
        except (OSError, UnicodeError, TypeError, ValueError) as exc:
            raise WebProjectError("无法保存项目配置。") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self._records = records
