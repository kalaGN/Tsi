"""TUI 本地命令的唯一目录与解析入口。"""

from dataclasses import dataclass
from enum import Enum


class LocalCommand(str, Enum):
    """应用支持且不会发送给模型的本地命令。"""

    CLEAR = "/clear"
    SKILLS = "/skills"
    QUIT = "/quit"


@dataclass(frozen=True)
class CommandSpec:
    """命令候选展示所需的稳定元数据。"""

    command: LocalCommand
    description: str


COMMAND_SPECS = (
    CommandSpec(LocalCommand.CLEAR, "清空对话、上下文和历史"),
    CommandSpec(LocalCommand.SKILLS, "查看可用技能"),
    CommandSpec(LocalCommand.QUIT, "退出 TUI"),
)


def parse_local_command(input_text: str) -> LocalCommand | None:
    """识别允许首尾空白的完整命令，普通输入返回 ``None``。"""

    normalized = input_text.strip()
    try:
        return LocalCommand(normalized)
    except ValueError:
        return None


def suggest_local_commands(input_text: str) -> tuple[CommandSpec, ...]:
    """返回未完成的无空白命令前缀对应候选。"""

    if not input_text.startswith("/") or any(
        character.isspace() for character in input_text
    ):
        return ()
    if any(spec.command.value == input_text for spec in COMMAND_SPECS):
        return ()
    return tuple(
        spec for spec in COMMAND_SPECS if spec.command.value.startswith(input_text)
    )
