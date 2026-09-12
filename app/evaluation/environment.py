"""构造与销毁单个 Trial 的隔离 Agent 运行环境。"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.evaluation.contracts import CaseSetup, EvaluationConfigError, FileExpectation, HarnessFingerprint
from app.evaluation.fingerprint import build_harness_fingerprint
from app.runtime.memory import ConversationState, MemoryPolicy, UserPreference, is_safe_preference_content
from app.runtime.session import ChatSession
from app.runtime.session_store import SessionStore
from app.runtime.skill_runtime import SkillRuntime
from app.runtime.system_prompt import SystemPromptLoadError, load_system_prompt
from app.runtime.tool_loop import ToolLoopLimits
from app.services.llm.contracts import LlmProvider
from tools.contracts import AnyToolApprovalRequest
from tools.skills import SkillLoadError, load_skill_catalog
from tools.workspace import WorkspacePolicy, create_intent_workspace_registry


_PREFERENCE_TIME = "2000-01-01T00:00:00Z"


@dataclass(slots=True)
class EvaluationEnvironment:
    """一次 Trial 独占的工作区、会话和审批策略。"""

    temporary: tempfile.TemporaryDirectory[str]
    workspace: Path
    session: ChatSession
    fingerprint: HarnessFingerprint
    approvals: dict[str, bool]

    async def approve(self, request: AnyToolApprovalRequest) -> bool:
        return self.approvals.get(request.tool_name, False)

    def capture_files(self, expectations: tuple[FileExpectation, ...]) -> tuple[tuple[str, str], ...]:
        """只捕获被断言的普通 UTF-8 文件，避免报告带出整个工作区。"""

        captured: list[tuple[str, str]] = []
        for expectation in expectations:
            path = self.workspace.joinpath(*expectation.path.split("/"))
            if not path.exists():
                continue
            if path.is_symlink() or not path.is_file():
                captured.append((expectation.path, "[NON_REGULAR_FILE]"))
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                captured.append((expectation.path, "[UNREADABLE_FILE]"))
                continue
            captured.append((expectation.path, content))
        return tuple(captured)

    def close(self) -> None:
        self.temporary.cleanup()


def create_evaluation_environment(
    project_root: Path,
    setup: CaseSetup,
    provider: LlmProvider,
    *,
    memory_policy: MemoryPolicy,
    tool_loop_limits: ToolLoopLimits,
) -> EvaluationEnvironment:
    """在系统临时目录装配真实 Session、SkillRuntime 和工具 Registry。"""

    temporary = tempfile.TemporaryDirectory(prefix="tsi-eval-")
    try:
        trial_root = Path(temporary.name)
        workspace = trial_root / "workspace"
        workspace.mkdir(mode=0o700)
        _copy_harness(Path(project_root), workspace)
        _write_setup_files(workspace, setup)
        try:
            agents_prompt = load_system_prompt(workspace)
            catalog = load_skill_catalog(workspace)
        except (SystemPromptLoadError, SkillLoadError) as exc:
            raise EvaluationConfigError(str(exc)) from exc
        policy = WorkspacePolicy(workspace)
        skill_runtime = SkillRuntime(
            workspace,
            agents_prompt,
            policy,
            catalog,
            registry_factory=create_intent_workspace_registry,
        )
        initial_snapshot = skill_runtime.snapshot()
        store = SessionStore(trial_root / "state" / "session.json")
        initial_state = ConversationState(
            messages=setup.messages,
            summary=setup.summary,
            summarized_message_count=setup.summarized_message_count,
            preferences=_preferences(setup.preferences),
        )
        if initial_state.messages or initial_state.summary or initial_state.preferences:
            store.save_state(initial_state)
        session = ChatSession.load(
            store,
            provider=provider,
            system_prompt=initial_snapshot.system_prompt,
            registry=initial_snapshot.registry,
            execution_snapshot_provider=skill_runtime.snapshot,
            tool_loop_limits=tool_loop_limits,
            memory_policy=memory_policy,
        )
        fingerprint = build_harness_fingerprint(
            Path(project_root),
            initial_snapshot.registry.definitions,
            memory_policy,
            tool_loop_limits,
        )
        return EvaluationEnvironment(
            temporary,
            workspace,
            session,
            fingerprint,
            dict(setup.approvals),
        )
    except Exception:
        temporary.cleanup()
        raise


def _copy_harness(project_root: Path, workspace: Path) -> None:
    agents = project_root / "AGENTS.md"
    if agents.is_file() and not agents.is_symlink():
        shutil.copy2(agents, workspace / "AGENTS.md")
    skills = project_root / ".agents" / "skills"
    if skills.is_dir() and not skills.is_symlink():
        _copy_plain_tree(skills, workspace / ".agents" / "skills")


def _copy_plain_tree(source: Path, target: Path) -> None:
    """复制普通文件树并拒绝任何可能逃出项目的符号链接。"""

    target.mkdir(parents=True)
    for path in sorted(source.rglob("*"), key=lambda item: item.relative_to(source).as_posix()):
        if path.is_symlink():
            raise EvaluationConfigError("project skills contain a symbolic link")
        relative = path.relative_to(source)
        destination = target / relative
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
        else:
            raise EvaluationConfigError("project skills contain a non-regular entry")


def _write_setup_files(workspace: Path, setup: CaseSetup) -> None:
    for relative, content in setup.files:
        target = workspace.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _preferences(contents: tuple[str, ...]) -> tuple[UserPreference, ...]:
    preferences: list[UserPreference] = []
    for content in contents:
        if not is_safe_preference_content(content):
            raise EvaluationConfigError("setup preference is unsafe")
        preference_id = hashlib.sha256(content.casefold().encode("utf-8")).hexdigest()[:16]
        preferences.append(UserPreference(preference_id, content, "explicit", _PREFERENCE_TIME))
    return tuple(preferences)
