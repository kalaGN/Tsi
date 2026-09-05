"""TUI 本地命令目录与解析测试。"""

from app.tui.commands import (
    COMMAND_SPECS,
    LocalCommand,
    parse_local_command,
    suggest_local_commands,
)


def test_command_catalog_contains_each_supported_command_once() -> None:
    commands = tuple(spec.command for spec in COMMAND_SPECS)

    assert commands == (
        LocalCommand.CLEAR,
        LocalCommand.SKILLS,
        LocalCommand.QUIT,
    )
    assert len(commands) == len(set(commands))
    assert all(spec.description for spec in COMMAND_SPECS)


def test_parse_local_command_accepts_complete_command_with_outer_whitespace() -> None:
    assert parse_local_command(" \n/skills\t") is LocalCommand.SKILLS


def test_parse_local_command_rejects_partial_unknown_and_regular_input() -> None:
    assert parse_local_command("/sk") is None
    assert parse_local_command("/unknown") is None
    assert parse_local_command("你好") is None


def test_suggest_local_commands_returns_only_unfinished_prefix_matches() -> None:
    assert tuple(
        spec.command for spec in suggest_local_commands("/s")
    ) == (LocalCommand.SKILLS,)
    assert suggest_local_commands("/") == COMMAND_SPECS
    assert suggest_local_commands("/skills") == ()
    assert suggest_local_commands(" /s") == ()
    assert suggest_local_commands("/s ") == ()
    assert suggest_local_commands("/unknown") == ()
