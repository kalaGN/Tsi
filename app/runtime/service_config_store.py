"""已接入外部服务的私有配置；公开接口只返回脱敏字段状态。"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.runtime.model_budget import strict_json
from local_paths import data_root


DEFAULT_SERVICE_CONFIG_PATH = data_root() / "service-config.json"
MAX_SERVICE_CONFIG_BYTES = 16 * 1024
MAX_SECRET_BYTES = 4096
# 新服务必须由代码显式登记，并在实际工具中消费对应字段。
SERVICE_FIELDS = {"serper": {"name": "Serper 网络搜索", "fields": {"api_key": "API Key"}}}


class ServiceConfigError(Exception):
    """不携带路径或密钥的配置读写错误。"""


class ServiceConfigConflict(ServiceConfigError):
    """其他页面已先保存服务配置。"""


class ServiceConfigValidation(ServiceConfigError):
    """输入中的服务、字段或密钥无效。"""


@dataclass(frozen=True, repr=False)
class ServiceConfig:
    revision: int
    services: dict[str, dict[str, str]]

    def secret(self, service_id: str, field_id: str) -> str:
        """只供已接入工具读取，不进入页面响应。"""

        return self.services[service_id][field_id]

    def public_payload(self) -> dict[str, object]:
        """页面只能看到代码支持的服务和字段是否已配置。"""

        return {
            "revision": self.revision,
            "services": [
                {
                    "id": service_id,
                    "name": definition["name"],
                    "fields": [
                        {"id": field_id, "name": field_name, "configured": bool(self.services[service_id][field_id])}
                        for field_id, field_name in definition["fields"].items()
                    ],
                }
                for service_id, definition in SERVICE_FIELDS.items()
            ],
        }


def _defaults() -> ServiceConfig:
    return ServiceConfig(0, {
        service_id: {field_id: "" for field_id in definition["fields"]}
        for service_id, definition in SERVICE_FIELDS.items()
    })


def _valid_secret(value: object, *, allow_empty: bool) -> bool:
    if not isinstance(value, str) or (not allow_empty and not value):
        return False
    try:
        return len(value.encode("utf-8")) <= MAX_SECRET_BYTES and all(
            33 <= ord(character) <= 126 for character in value
        )
    except UnicodeError:
        return False


def _decode(value: object) -> ServiceConfig:
    if not isinstance(value, dict) or set(value) != {"version", "revision", "services"}:
        raise ValueError("invalid service config")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("invalid service config version")
    if type(value["revision"]) is not int or value["revision"] < 0:
        raise ValueError("invalid service config revision")
    raw_services = value["services"]
    if not isinstance(raw_services, dict) or set(raw_services) - set(SERVICE_FIELDS):
        raise ValueError("invalid services")
    services = _defaults().services
    for service_id, fields in raw_services.items():
        expected_fields = SERVICE_FIELDS[service_id]["fields"]
        if not isinstance(fields, dict) or set(fields) - set(expected_fields):
            raise ValueError("invalid service fields")
        for field_id, secret in fields.items():
            if not _valid_secret(secret, allow_empty=True):
                raise ValueError("invalid service secret")
            services[service_id][field_id] = secret
    return ServiceConfig(value["revision"], services)


class ServiceConfigStore:
    """严格读取并原子保存服务配置；不导入旧环境变量。"""

    def __init__(self, path: Path = DEFAULT_SERVICE_CONFIG_PATH) -> None:
        self.path = Path(path)

    def load(self) -> ServiceConfig:
        descriptor = None
        try:
            try:
                info = self.path.lstat()
            except FileNotFoundError:
                return _defaults()
            parent_info = self.path.parent.lstat()
            if not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_mode & 0o077:
                raise ValueError("unsafe service config directory")
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError("unsafe service config")
            descriptor = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > MAX_SERVICE_CONFIG_BYTES:
                raise ValueError("unsafe service config")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = None
                raw = stream.read(MAX_SERVICE_CONFIG_BYTES + 1)
            if len(raw) > MAX_SERVICE_CONFIG_BYTES:
                raise ValueError("oversized service config")
            return _decode(strict_json(raw.decode("utf-8")))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ServiceConfigError("服务配置无法读取，请检查本机配置文件及权限。") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def secret(self, service_id: str, field_id: str) -> str:
        """每次工具调用重新读取，以便保存后立即生效。"""

        if not isinstance(service_id, str) or service_id not in SERVICE_FIELDS or not isinstance(field_id, str) or field_id not in SERVICE_FIELDS[service_id]["fields"]:
            raise ServiceConfigValidation("不支持的服务配置字段。")
        return self.load().secret(service_id, field_id)

    def save(
        self, *, expected_revision: int, service_id: str, field_id: str,
        action: str, value: str | None = None,
    ) -> ServiceConfig:
        """仅更新一个已知字段；set 和 clear 均需显式操作。"""

        current = self.load()
        if type(expected_revision) is not int or expected_revision != current.revision:
            raise ServiceConfigConflict("服务配置已更新，请重新加载后保存。")
        if not isinstance(service_id, str) or service_id not in SERVICE_FIELDS or not isinstance(field_id, str) or field_id not in SERVICE_FIELDS[service_id]["fields"]:
            raise ServiceConfigValidation("不支持的服务配置字段。")
        if not isinstance(action, str) or action not in {"set", "clear"} or (action == "set" and not _valid_secret(value, allow_empty=False)):
            raise ServiceConfigValidation("服务配置值无效。")
        if action == "clear" and value is not None:
            raise ServiceConfigValidation("删除密钥时不能携带新值。")
        services = {name: dict(fields) for name, fields in current.services.items()}
        services[service_id][field_id] = value if action == "set" else ""
        updated = ServiceConfig(current.revision + 1, services)
        encoded = (json.dumps({
            "version": 1, "revision": updated.revision, "services": updated.services,
        }, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > MAX_SERVICE_CONFIG_BYTES:
            raise ServiceConfigValidation("服务配置过大。")

        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.path.parent.is_symlink() or not self.path.parent.is_dir():
                raise OSError("unsafe directory")
            os.chmod(self.path.parent, 0o700)
            descriptor, temporary_name = tempfile.mkstemp(dir=self.path.parent, prefix=".service-config-", suffix=".tmp")
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
                raise OSError("unsafe target")
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            raise ServiceConfigError("服务配置保存失败，原配置未被主动重置。") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return updated
