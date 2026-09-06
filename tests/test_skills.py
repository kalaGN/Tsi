import asyncio
import json
import os
from pathlib import Path

import pytest

from tools import ToolCall, ToolExecutionContext, ToolRegistry
from tools.skills import (
    MAX_EXPLICIT_SKILLS,
    MAX_RESOURCE_COUNT,
    MAX_RESOURCE_FILE_BYTES,
    MAX_RESOURCE_TOTAL_BYTES,
    MAX_SKILLS,
    MAX_SKILL_FILE_BYTES,
    LoadSkillTool,
    ReadSkillResourceTool,
    RunSkillScriptTool,
    SkillLoadError,
    SkillReferenceError,
    build_explicit_skills_prompt,
    load_skill_catalog,
    resolve_skill_references,
)


def _write_skill(
    root: Path,
    name: str = "example-skill",
    *,
    description: str = "用于验证 Skill 加载",
    body: str = "# 示例\n\n按步骤执行。\n",
) -> Path:
    skill_root = root / ".agents" / "skills" / name
    skill_root.mkdir(parents=True, exist_ok=True)
    (skill_root / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "license: MIT\n"
        "metadata:\n"
        "  version: '1.0'\n"
        "unknown-codex-field: accepted\n"
        "---\n\n"
        f"{body}",
        encoding="utf-8",
    )
    return skill_root


def test_missing_skill_directory_returns_empty_catalog(tmp_path):
    catalog = load_skill_catalog(tmp_path)

    assert catalog.count == 0
    assert catalog.prompt is None


def test_catalog_loads_codex_skill_and_sorted_resource_snapshots(tmp_path):
    zeta = _write_skill(tmp_path, "zeta-skill", description="后加载")
    alpha = _write_skill(tmp_path, "alpha-skill", description="先加载")
    (alpha / "scripts").mkdir()
    (alpha / "scripts" / "run.py").write_text(
        "print('你好')\n", encoding="utf-8"
    )
    (alpha / "references").mkdir()
    (alpha / "references" / "api.md").write_text(
        "# 接口参考\n", encoding="utf-8"
    )
    (alpha / "assets").mkdir()
    (alpha / "assets" / "image.bin").write_bytes(b"\xff\x00")
    # 创建顺序不能影响 Catalog 和资源顺序。
    (zeta / "notes.txt").write_text("说明", encoding="utf-8")

    catalog = load_skill_catalog(tmp_path)

    assert tuple(catalog.skills) == ("alpha-skill", "zeta-skill")
    skill = catalog.skills["alpha-skill"]
    assert skill.relative_entrypoint == (
        ".agents/skills/alpha-skill/SKILL.md"
    )
    assert tuple(skill.resources) == (
        "assets/image.bin",
        "references/api.md",
        "scripts/run.py",
    )
    assert skill.resources["scripts/run.py"].text == "print('你好')\n"
    assert skill.resources["assets/image.bin"].text is None
    assert "alpha-skill" in catalog.prompt
    assert "先加载" in catalog.prompt
    assert skill.relative_entrypoint in catalog.prompt
    assert "按步骤执行" not in catalog.prompt


def test_explicit_skill_references_require_complete_boundary_and_catalog_match(
    tmp_path,
):
    _write_skill(tmp_path, "alpha-skill")
    _write_skill(tmp_path, "beta-skill")
    catalog = load_skill_catalog(tmp_path)

    references = resolve_skill_references(
        "$alpha-skill 请处理\n$unknown $alpha-skill $beta-skill",
        catalog,
    )

    assert tuple(skill.name for skill in references) == (
        "alpha-skill",
        "beta-skill",
    )
    assert resolve_skill_references(
        "金额$alpha-skill $alpha-skill, $alpha",
        catalog,
    ) == ()


def test_explicit_skill_references_enforce_distinct_count_limit(tmp_path):
    for index in range(MAX_EXPLICIT_SKILLS + 1):
        _write_skill(tmp_path, f"skill-{index}")
    catalog = load_skill_catalog(tmp_path)
    input_text = " ".join(f"$skill-{index}" for index in range(4))

    with pytest.raises(SkillReferenceError):
        resolve_skill_references(input_text, catalog)


def test_explicit_skill_prompt_contains_markdown_and_resource_index_only(tmp_path):
    skill_root = _write_skill(tmp_path, body="# 执行说明\n\n使用脚本。\n")
    (skill_root / "scripts").mkdir()
    (skill_root / "scripts" / "run.py").write_text(
        "print('资源正文不能预载')\n",
        encoding="utf-8",
    )
    catalog = load_skill_catalog(tmp_path)
    references = resolve_skill_references("使用 $example-skill 完成", catalog)

    prompt = build_explicit_skills_prompt(references)

    assert "example-skill" in prompt
    assert "# 执行说明" in prompt
    assert "scripts/run.py" in prompt
    assert "资源正文不能预载" not in prompt


def test_explicit_skill_prompt_enforces_total_byte_limit(tmp_path, monkeypatch):
    _write_skill(tmp_path)
    catalog = load_skill_catalog(tmp_path)
    monkeypatch.setattr("tools.skills.MAX_EXPLICIT_SKILLS_PROMPT_BYTES", 10)

    with pytest.raises(SkillReferenceError):
        build_explicit_skills_prompt((catalog.skills["example-skill"],))


@pytest.mark.parametrize(
    "frontmatter",
    [
        "name: duplicate\nname: duplicate\ndescription: x",
        "name: Bad_Name\ndescription: x",
        "name: wrong-directory\ndescription: x",
        "name: duplicate\ndescription: !!python/object:builtins.object {}",
        "- name\n- description",
    ],
)
def test_catalog_rejects_invalid_frontmatter_without_leaking_content(
    tmp_path,
    frontmatter,
):
    skill_root = tmp_path / ".agents" / "skills" / "duplicate"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        f"---\n{frontmatter}\n---\nSECRET-SKILL-CONTENT",
        encoding="utf-8",
    )

    with pytest.raises(SkillLoadError) as captured:
        load_skill_catalog(tmp_path)

    assert str(captured.value) == "Project skills are unavailable"
    assert "SECRET" not in str(captured.value)
    assert str(tmp_path) not in str(captured.value)


def test_catalog_rejects_oversized_skill_and_resource_symlink(tmp_path):
    skill_root = _write_skill(tmp_path)
    (skill_root / "SKILL.md").write_bytes(
        b"a" * (MAX_SKILL_FILE_BYTES + 1)
    )

    with pytest.raises(SkillLoadError):
        load_skill_catalog(tmp_path)

    (skill_root / "SKILL.md").unlink()
    _write_skill(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    (skill_root / "scripts").mkdir()
    (skill_root / "scripts" / "linked.py").symlink_to(outside)

    with pytest.raises(SkillLoadError):
        load_skill_catalog(tmp_path)


def test_catalog_rejects_symlinked_skill_directory(tmp_path):
    real_skill = tmp_path / "real-skill"
    _write_skill(real_skill)
    skills_root = tmp_path / ".agents" / "skills"
    skills_root.mkdir(parents=True)
    (skills_root / "linked-skill").symlink_to(
        real_skill / ".agents" / "skills" / "example-skill",
        target_is_directory=True,
    )

    with pytest.raises(SkillLoadError):
        load_skill_catalog(tmp_path)


def test_catalog_enforces_skill_and_resource_quantity_limits(tmp_path):
    for index in range(MAX_SKILLS + 1):
        _write_skill(tmp_path, f"skill-{index}")

    with pytest.raises(SkillLoadError):
        load_skill_catalog(tmp_path)

    limited_root = tmp_path / "resource-count"
    skill_root = _write_skill(limited_root)
    (skill_root / "references").mkdir()
    for index in range(MAX_RESOURCE_COUNT + 1):
        (skill_root / "references" / f"{index}.txt").write_text(
            "x", encoding="utf-8"
        )

    with pytest.raises(SkillLoadError):
        load_skill_catalog(limited_root)


def test_catalog_enforces_resource_total_size_limit(tmp_path):
    skill_root = _write_skill(tmp_path)
    (skill_root / "assets").mkdir()
    file_count = MAX_RESOURCE_TOTAL_BYTES // MAX_RESOURCE_FILE_BYTES + 1
    for index in range(file_count):
        (skill_root / "assets" / f"{index}.bin").write_bytes(
            b"x" * MAX_RESOURCE_FILE_BYTES
        )

    with pytest.raises(SkillLoadError):
        load_skill_catalog(tmp_path)


def test_read_tools_use_snapshot_after_files_change(tmp_path):
    skill_root = _write_skill(tmp_path)
    (skill_root / "references").mkdir()
    resource = skill_root / "references" / "guide.md"
    resource.write_text("初始内容", encoding="utf-8")
    catalog = load_skill_catalog(tmp_path)
    skill_markdown = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    registry = ToolRegistry(
        (LoadSkillTool(catalog), ReadSkillResourceTool(catalog))
    )

    (skill_root / "SKILL.md").write_text("changed", encoding="utf-8")
    resource.write_text("变化内容", encoding="utf-8")
    loaded = asyncio.run(
        registry.execute(
            ToolCall("load-1", "load_skill", '{"name":"example-skill"}')
        )
    )
    read = asyncio.run(
        registry.execute(
            ToolCall(
                "read-1",
                "read_skill_resource",
                '{"name":"example-skill","path":"references/guide.md"}',
            )
        )
    )

    assert json.loads(loaded.output)["data"]["skill_markdown"] == (
        skill_markdown
    )
    assert json.loads(read.output)["data"]["content"] == "初始内容"


def test_skill_tool_required_fields_are_declared_properties(tmp_path):
    """避免向严格校验 Function Tool Schema 的 Provider 发送矛盾定义。"""

    catalog = load_skill_catalog(_write_skill(tmp_path).parents[2])
    definitions = (
        LoadSkillTool(catalog).definition,
        ReadSkillResourceTool(catalog).definition,
    )

    for definition in definitions:
        properties = definition.parameters.get("properties", {})
        assert set(definition.parameters.get("required", ())) <= set(properties)


def test_read_resource_rejects_binary_and_path_traversal(tmp_path):
    skill_root = _write_skill(tmp_path)
    (skill_root / "assets").mkdir()
    (skill_root / "assets" / "binary.dat").write_bytes(b"\xff")
    registry = ToolRegistry(
        (ReadSkillResourceTool(load_skill_catalog(tmp_path)),)
    )

    for path in ("assets/binary.dat", "../SKILL.md", "/etc/passwd"):
        result = asyncio.run(
            registry.execute(
                ToolCall(
                    "read-invalid",
                    "read_skill_resource",
                    json.dumps({"name": "example-skill", "path": path}),
                )
            )
        )
        assert json.loads(result.output)["error"]["code"] == (
            "invalid_arguments"
        )


def test_skill_script_requires_approval_and_passes_arguments_without_shell(
    tmp_path,
    monkeypatch,
):
    skill_root = _write_skill(tmp_path)
    (skill_root / "scripts").mkdir()
    (skill_root / "scripts" / "args.py").write_text(
        "import json, os, sys\n"
        "print(json.dumps({'args': sys.argv[1:], "
        "'secret': os.getenv('TEST_SKILL_SECRET'), "
        "'home': os.getenv('HOME')}))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_SKILL_SECRET", "must-not-leak")
    registry = ToolRegistry(
        (RunSkillScriptTool(load_skill_catalog(tmp_path)),)
    )
    missing_arguments = asyncio.run(
        registry.execute(
            ToolCall(
                "run-missing-arguments",
                "run_skill_script",
                '{"name":"example-skill","path":"scripts/args.py"}',
            )
        )
    )
    assert json.loads(missing_arguments.output)["error"]["code"] == (
        "invalid_arguments"
    )
    call = ToolCall(
        "run-1",
        "run_skill_script",
        json.dumps(
            {
                "name": "example-skill",
                "path": "scripts/args.py",
                "arguments": ["$(touch should-not-exist)", "甲 乙"],
            },
            ensure_ascii=False,
        ),
    )

    denied = asyncio.run(registry.execute(call))
    assert json.loads(denied.output)["error"]["code"] == (
        "approval_unavailable"
    )

    async def approve(_request):
        return True

    approved = asyncio.run(
        registry.execute(
            call,
            ToolExecutionContext(approval_handler=approve),
        )
    )
    data = json.loads(approved.output)["data"]
    script_output = json.loads(data["stdout"])
    assert data["exit_code"] == 0
    assert script_output["args"] == ["$(touch should-not-exist)", "甲 乙"]
    assert script_output["secret"] is None
    assert script_output["home"] is None
    assert not (skill_root / "should-not-exist").exists()


def test_posix_shell_skill_script_uses_fixed_interpreter(tmp_path):
    skill_root = _write_skill(tmp_path)
    (skill_root / "scripts").mkdir()
    (skill_root / "scripts" / "show.sh").write_text(
        "printf '%s\\n' \"$1\"\n",
        encoding="utf-8",
    )
    registry = ToolRegistry(
        (RunSkillScriptTool(load_skill_catalog(tmp_path)),)
    )

    async def approve(request):
        assert request.command_text.startswith("/bin/sh scripts/show.sh ")
        return True

    result = asyncio.run(
        registry.execute(
            ToolCall(
                "shell-1",
                "run_skill_script",
                json.dumps(
                    {
                        "name": "example-skill",
                        "path": "scripts/show.sh",
                        "arguments": ["中文 参数"],
                    },
                    ensure_ascii=False,
                ),
            ),
            ToolExecutionContext(approval_handler=approve),
        )
    )

    data = json.loads(result.output)["data"]
    assert data == {"exit_code": 0, "stdout": "中文 参数\n", "stderr": ""}


def test_nonzero_script_exit_returns_bounded_output(tmp_path):
    skill_root = _write_skill(tmp_path)
    (skill_root / "scripts").mkdir()
    (skill_root / "scripts" / "fail.sh").write_text(
        "printf '失败原因\\n' >&2\nexit 7\n",
        encoding="utf-8",
    )
    registry = ToolRegistry(
        (RunSkillScriptTool(load_skill_catalog(tmp_path)),)
    )

    async def approve(_request):
        return True

    result = asyncio.run(
        registry.execute(
            ToolCall(
                "shell-fail",
                "run_skill_script",
                '{"name":"example-skill","path":"scripts/fail.sh",'
                '"arguments":[]}',
            ),
            ToolExecutionContext(approval_handler=approve),
        )
    )

    assert json.loads(result.output)["data"] == {
        "exit_code": 7,
        "stdout": "",
        "stderr": "失败原因\n",
    }


def test_script_changed_after_preview_is_not_executed(tmp_path):
    skill_root = _write_skill(tmp_path)
    (skill_root / "scripts").mkdir()
    script = skill_root / "scripts" / "run.py"
    script.write_text("print('original')\n", encoding="utf-8")
    registry = ToolRegistry(
        (RunSkillScriptTool(load_skill_catalog(tmp_path)),)
    )

    async def change_before_approval_returns(_request):
        script.write_text("print('changed')\n", encoding="utf-8")
        return True

    result = asyncio.run(
        registry.execute(
            ToolCall(
                "changed-1",
                "run_skill_script",
                '{"name":"example-skill","path":"scripts/run.py",'
                '"arguments":[]}',
            ),
            ToolExecutionContext(
                approval_handler=change_before_approval_returns
            ),
        )
    )

    assert json.loads(result.output)["error"]["code"] == "workspace_conflict"


def test_rejected_script_is_asked_again_on_next_call(tmp_path):
    skill_root = _write_skill(tmp_path)
    (skill_root / "scripts").mkdir()
    (skill_root / "scripts" / "run.py").write_text(
        "print('not executed')\n", encoding="utf-8"
    )
    registry = ToolRegistry(
        (RunSkillScriptTool(load_skill_catalog(tmp_path)),)
    )
    approvals = []

    async def reject(request):
        approvals.append(request.fingerprint)
        return False

    call = ToolCall(
        "rejected",
        "run_skill_script",
        '{"name":"example-skill","path":"scripts/run.py",'
        '"arguments":[]}',
    )
    context = ToolExecutionContext(approval_handler=reject)

    first = asyncio.run(registry.execute(call, context))
    second = asyncio.run(registry.execute(call, context))

    assert json.loads(first.output)["error"]["code"] == "approval_denied"
    assert json.loads(second.output)["error"]["code"] == "approval_denied"
    assert len(approvals) == 2


def test_script_timeout_and_output_overflow_return_fixed_errors(tmp_path):
    skill_root = _write_skill(tmp_path)
    (skill_root / "scripts").mkdir()
    (skill_root / "scripts" / "sleep.py").write_text(
        "import time\ntime.sleep(10)\n", encoding="utf-8"
    )
    (skill_root / "scripts" / "noisy.py").write_text(
        "print('x' * 1000)\n", encoding="utf-8"
    )
    catalog = load_skill_catalog(tmp_path)

    async def approve(_request):
        return True

    async def scenario():
        context = ToolExecutionContext(approval_handler=approve)
        timeout = await ToolRegistry(
            (RunSkillScriptTool(catalog, timeout_seconds=0.05),)
        ).execute(
            ToolCall(
                "timeout",
                "run_skill_script",
                '{"name":"example-skill","path":"scripts/sleep.py",'
                '"arguments":[]}',
            ),
            context,
        )
        overflow = await ToolRegistry(
            (RunSkillScriptTool(catalog, output_limit_bytes=64),)
        ).execute(
            ToolCall(
                "overflow",
                "run_skill_script",
                '{"name":"example-skill","path":"scripts/noisy.py",'
                '"arguments":[]}',
            ),
            context,
        )
        return timeout, overflow

    timeout, overflow = asyncio.run(scenario())

    assert json.loads(timeout.output)["error"]["code"] == "script_timeout"
    assert json.loads(overflow.output)["error"]["code"] == (
        "script_output_too_large"
    )


def test_cancelling_script_terminates_its_process_group(tmp_path):
    skill_root = _write_skill(tmp_path)
    (skill_root / "scripts").mkdir()
    marker = tmp_path / "child-finished"
    (skill_root / "scripts" / "spawn.py").write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', "
        "'import pathlib,time,sys; time.sleep(0.4); "
        "pathlib.Path(sys.argv[1]).write_text(\"done\")', sys.argv[1]])\n"
        "time.sleep(10)\n",
        encoding="utf-8",
    )
    tool = RunSkillScriptTool(load_skill_catalog(tmp_path))
    arguments = {
        "name": "example-skill",
        "path": "scripts/spawn.py",
        "arguments": [str(marker)],
    }

    async def scenario():
        await tool.preview("cancel", arguments)
        task = asyncio.create_task(tool.invoke(arguments))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.5)

    asyncio.run(scenario())

    assert not marker.exists()
