import pytest

from app.evaluation.contracts import (
    CaseSetup,
    EvaluationConfigError,
    FileExpectation,
    ReplayStep,
)
from app.evaluation.environment import create_evaluation_environment
from app.evaluation.replay import ReplayProvider
from app.runtime.memory import MemoryPolicy
from app.runtime.tool_loop import DEFAULT_TOOL_LOOP_LIMITS


def test_environment_copies_harness_seeds_state_and_cleans_up(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# 测试规则", encoding="utf-8")
    setup = CaseSetup(
        files=(("notes.txt", "初始"),),
        preferences=("使用中文回复",),
    )
    environment = create_evaluation_environment(
        tmp_path,
        setup,
        ReplayProvider(((ReplayStep("完成"),),)),
        memory_policy=MemoryPolicy(),
        tool_loop_limits=DEFAULT_TOOL_LOOP_LIMITS,
    )
    temporary_root = environment.workspace.parent

    assert (environment.workspace / "AGENTS.md").read_text(encoding="utf-8") == "# 测试规则"
    assert environment.session.preferences[0].content == "使用中文回复"
    assert environment.capture_files((FileExpectation("notes.txt"),)) == (("notes.txt", "初始"),)

    environment.close()
    assert not temporary_root.exists()


def test_environment_rejects_symbolic_links_in_project_skills(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    skill = tmp_path / ".agents" / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: demo\ndescription: demo\n---\n",
        encoding="utf-8",
    )
    (skill / "leak.txt").symlink_to(outside)

    with pytest.raises(EvaluationConfigError, match="symbolic link"):
        create_evaluation_environment(
            tmp_path,
            CaseSetup(),
            ReplayProvider(((ReplayStep("完成"),),)),
            memory_policy=MemoryPolicy(),
            tool_loop_limits=DEFAULT_TOOL_LOOP_LIMITS,
        )
