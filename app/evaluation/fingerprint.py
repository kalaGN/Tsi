"""生成不包含正文和绝对路径的稳定 Harness 指纹。"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from app.evaluation.contracts import HarnessFingerprint
from app.runtime.memory import MemoryPolicy
from app.runtime.tool_loop import ToolLoopLimits
from tools.contracts import ToolDefinition


def build_harness_fingerprint(
    project_root: Path,
    definitions: Sequence[ToolDefinition],
    memory_policy: MemoryPolicy,
    tool_loop_limits: ToolLoopLimits,
) -> HarnessFingerprint:
    """对影响行为的仓库输入和运行策略生成规范哈希。"""

    root = Path(project_root).resolve()
    agents = root / "AGENTS.md"
    tools_payload = [
        {
            "name": item.name,
            "description": item.description,
            "parameters": item.parameters,
            "effect": item.effect.value,
            "max_argument_bytes": item.max_argument_bytes,
            "max_result_bytes": item.max_result_bytes,
        }
        for item in definitions
    ]
    policy_payload = {
        "memory": asdict(memory_policy),
        "tool_loop": asdict(tool_loop_limits),
    }
    return HarnessFingerprint(
        git_head=_git_output(root, ("rev-parse", "HEAD")),
        git_dirty=bool(_git_output(root, ("status", "--porcelain"))),
        agents_sha256=(
            _file_digest(agents)
            if agents.is_file() and not agents.is_symlink()
            else None
        ),
        skills_sha256=_directory_digest(root / ".agents" / "skills"),
        tools_sha256=_json_digest(tools_payload),
        policy_sha256=_json_digest(policy_payload),
    )


def _git_output(root: Path, arguments: tuple[str, ...]) -> str | None:
    try:
        completed = subprocess.run(
            ("git", "-C", str(root), *arguments),
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _directory_digest(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.is_dir():
        return digest.hexdigest()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(path.stat().st_size.to_bytes(8, "big"))
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(64 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _json_digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
