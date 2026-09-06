"""从 TUI 启动目录安全加载可选项目系统提示词。"""

import os
import stat
from pathlib import Path


SYSTEM_PROMPT_FILENAME = "AGENTS.md"
MAX_SYSTEM_PROMPT_BYTES = 32 * 1024
SYSTEM_PROMPT_ERROR_MESSAGE = (
    "AGENTS.md must be a readable UTF-8 file no larger than 32 KiB"
)


class SystemPromptLoadError(Exception):
    """隐藏路径、正文和底层文件系统细节的加载错误。"""


def load_system_prompt(startup_directory: Path) -> str | None:
    """读取启动目录直属 AGENTS.md；缺失或空白时不启用提示词。"""

    agents_path = Path(startup_directory) / SYSTEM_PROMPT_FILENAME
    try:
        with agents_path.open("rb") as handle:
            # 设备或目录等特殊文件可能无限读取，只接受普通文件。
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise SystemPromptLoadError(SYSTEM_PROMPT_ERROR_MESSAGE)
            content_bytes = handle.read(MAX_SYSTEM_PROMPT_BYTES + 1)
    except FileNotFoundError:
        return None
    except SystemPromptLoadError:
        raise
    except OSError as exc:
        raise SystemPromptLoadError(SYSTEM_PROMPT_ERROR_MESSAGE) from exc

    if len(content_bytes) > MAX_SYSTEM_PROMPT_BYTES:
        raise SystemPromptLoadError(SYSTEM_PROMPT_ERROR_MESSAGE)
    try:
        content = content_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SystemPromptLoadError(SYSTEM_PROMPT_ERROR_MESSAGE) from exc
    return content if content.strip() else None


def compose_system_prompt(
    *prompts: str | None,
) -> str | None:
    """按固定顺序组合非空提示词，且不制造空 system 消息。"""

    parts = [
        part
        for part in prompts
        if isinstance(part, str) and part.strip()
    ]
    return "\n\n---\n\n".join(parts) if parts else None
