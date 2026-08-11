from pathlib import Path

import pytest

from app.runtime.system_prompt import (
    MAX_SYSTEM_PROMPT_BYTES,
    SystemPromptLoadError,
    load_system_prompt,
)


def test_missing_or_blank_agents_file_disables_system_prompt(tmp_path):
    assert load_system_prompt(tmp_path) is None

    (tmp_path / "AGENTS.md").write_text(" \n\t", encoding="utf-8")

    assert load_system_prompt(tmp_path) is None


def test_loader_preserves_utf8_agents_content_exactly(tmp_path):
    content = "# 项目规则\n\n- 回复使用中文\n"
    (tmp_path / "AGENTS.md").write_text(content, encoding="utf-8")

    assert load_system_prompt(tmp_path) == content


def test_loader_accepts_exact_byte_limit(tmp_path):
    content = "a" * MAX_SYSTEM_PROMPT_BYTES
    (tmp_path / "AGENTS.md").write_bytes(content.encode("utf-8"))

    assert load_system_prompt(tmp_path) == content


@pytest.mark.parametrize(
    "invalid_content",
    [
        b"a" * (MAX_SYSTEM_PROMPT_BYTES + 1),
        b"\xff",
    ],
)
def test_loader_rejects_oversized_or_non_utf8_agents_file(
    tmp_path,
    invalid_content,
):
    (tmp_path / "AGENTS.md").write_bytes(invalid_content)

    with pytest.raises(SystemPromptLoadError) as captured:
        load_system_prompt(tmp_path)

    assert str(captured.value) == (
        "AGENTS.md must be a readable UTF-8 file no larger than 32 KiB"
    )
    assert str(tmp_path) not in str(captured.value)


def test_loader_rejects_directory_instead_of_agents_file(tmp_path):
    (tmp_path / "AGENTS.md").mkdir()

    with pytest.raises(SystemPromptLoadError):
        load_system_prompt(tmp_path)


def test_loader_converts_read_failure_to_safe_error(tmp_path, monkeypatch):
    agents_path = tmp_path / "AGENTS.md"
    agents_path.write_text("private instructions", encoding="utf-8")
    original_open = Path.open

    def fail_agents_open(self, *args, **kwargs):
        if self == agents_path:
            raise PermissionError("sensitive operating system detail")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_agents_open)

    with pytest.raises(SystemPromptLoadError) as captured:
        load_system_prompt(tmp_path)

    assert "private instructions" not in str(captured.value)
    assert "sensitive operating system detail" not in str(captured.value)
