import asyncio
import json

import pytest

from app.runtime.chat import ChatErrorCode, ChatRuntimeError
from app.runtime.skill_runtime import SkillRuntime
from tools import ToolCall, ToolExecutionContext
from tools.skills import load_skill_catalog
from tools.workspace import WorkspacePolicy


def write_skill(root, directory, name):
    skill = root / directory
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} description\n---\n",
        encoding="utf-8",
    )


def test_runtime_publishes_install_only_to_next_snapshot(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    codex = tmp_path / "codex-skills"
    write_skill(codex, "source-folder", "demo-skill")
    runtime = SkillRuntime(
        project,
        "# Project rules",
        WorkspacePolicy(project),
        load_skill_catalog(project),
        codex_skills_root=codex,
    )
    before = runtime.snapshot()
    before_names = tuple(item.name for item in before.registry.definitions)
    assert "install_skill" in before_names
    assert "load_skill" not in before_names

    call = ToolCall(
        "install",
        "install_skill",
        json.dumps(
            {
                "source_type": "codex_home",
                "source": "source-folder",
                "expected_name": "demo-skill",
            }
        ),
    )

    async def approve(_request):
        return True

    result = asyncio.run(
        before.registry.execute(
            call,
            ToolExecutionContext(approval_handler=approve),
        )
    )
    assert result.is_error is False
    # 已经开始的快照仍然绑定旧 Catalog。
    assert "load_skill" not in tuple(
        item.name for item in before.registry.definitions
    )

    after = runtime.snapshot()
    after_names = tuple(item.name for item in after.registry.definitions)
    assert after.version == before.version + 1
    assert "load_skill" in after_names
    assert "demo-skill" in after.system_prompt
    assert runtime.status().skills_count == 1


def test_runtime_does_not_watch_manual_skill_changes(tmp_path):
    runtime = SkillRuntime(
        tmp_path,
        None,
        WorkspacePolicy(tmp_path),
        load_skill_catalog(tmp_path),
        codex_skills_root=tmp_path / "codex-skills",
    )
    before = runtime.snapshot()
    write_skill(tmp_path / ".agents/skills", "manual-skill", "manual-skill")

    after = runtime.snapshot()

    assert after.version == before.version
    assert "load_skill" not in tuple(item.name for item in after.registry.definitions)
    assert runtime.status().skills_count == 0


def test_runtime_lists_only_published_skill_summaries_in_name_order(tmp_path):
    write_skill(tmp_path / ".agents/skills", "zeta-skill", "zeta-skill")
    write_skill(tmp_path / ".agents/skills", "alpha-skill", "alpha-skill")
    runtime = SkillRuntime(
        tmp_path,
        None,
        WorkspacePolicy(tmp_path),
        load_skill_catalog(tmp_path),
        codex_skills_root=tmp_path / "codex-skills",
    )

    summaries = runtime.available_skills()

    assert tuple(summary.name for summary in summaries) == (
        "alpha-skill",
        "zeta-skill",
    )
    assert summaries[0].description == "alpha-skill description"
    assert summaries[0].relative_entrypoint == (
        ".agents/skills/alpha-skill/SKILL.md"
    )

    write_skill(tmp_path / ".agents/skills", "manual-skill", "manual-skill")
    assert tuple(summary.name for summary in runtime.available_skills()) == (
        "alpha-skill",
        "zeta-skill",
    )


def test_runtime_explicit_skill_applies_only_to_matching_request_snapshot(tmp_path):
    write_skill(tmp_path / ".agents/skills", "demo-skill", "demo-skill")
    skill_file = tmp_path / ".agents/skills/demo-skill/SKILL.md"
    skill_file.write_text(
        "---\nname: demo-skill\ndescription: demo\n---\n\nPRIVATE INSTRUCTIONS\n",
        encoding="utf-8",
    )
    runtime = SkillRuntime(
        tmp_path,
        "# Project rules",
        WorkspacePolicy(tmp_path),
        load_skill_catalog(tmp_path),
        codex_skills_root=tmp_path / "codex-skills",
    )

    regular = runtime.snapshot("普通请求")
    explicit = runtime.snapshot("请使用 $demo-skill 完成")

    assert "PRIVATE INSTRUCTIONS" not in regular.system_prompt
    assert "PRIVATE INSTRUCTIONS" in explicit.system_prompt
    assert explicit.version == regular.version


def test_runtime_rejects_too_many_explicit_skills_with_safe_input_error(tmp_path):
    for index in range(4):
        write_skill(
            tmp_path / ".agents/skills",
            f"skill-{index}",
            f"skill-{index}",
        )
    runtime = SkillRuntime(
        tmp_path,
        None,
        WorkspacePolicy(tmp_path),
        load_skill_catalog(tmp_path),
        codex_skills_root=tmp_path / "codex-skills",
    )

    with pytest.raises(ChatRuntimeError) as captured:
        runtime.snapshot("$skill-0 $skill-1 $skill-2 $skill-3")

    assert captured.value.code is ChatErrorCode.INVALID_INPUT
    assert str(tmp_path) not in captured.value.user_message
