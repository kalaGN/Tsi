from app.evaluation.fingerprint import build_harness_fingerprint
from app.runtime.memory import MemoryPolicy
from app.runtime.tool_loop import DEFAULT_TOOL_LOOP_LIMITS
from tools import create_default_registry


def test_harness_fingerprint_is_stable_and_changes_with_agents(tmp_path):
    (tmp_path / "AGENTS.md").write_text("规则一", encoding="utf-8")
    first = build_harness_fingerprint(tmp_path, create_default_registry().definitions, MemoryPolicy(), DEFAULT_TOOL_LOOP_LIMITS)
    second = build_harness_fingerprint(tmp_path, create_default_registry().definitions, MemoryPolicy(), DEFAULT_TOOL_LOOP_LIMITS)
    (tmp_path / "AGENTS.md").write_text("规则二", encoding="utf-8")
    changed = build_harness_fingerprint(tmp_path, create_default_registry().definitions, MemoryPolicy(), DEFAULT_TOOL_LOOP_LIMITS)

    assert first == second
    assert first.agents_sha256 != changed.agents_sha256
    assert first.git_head is None


def test_fingerprint_does_not_follow_agents_symlink(tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("宿主内容", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").symlink_to(outside)

    fingerprint = build_harness_fingerprint(
        project,
        (),
        MemoryPolicy(),
        DEFAULT_TOOL_LOOP_LIMITS,
    )

    assert fingerprint.agents_sha256 is None
