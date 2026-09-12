"""评测用例、轨迹、断言与报告的数据契约。"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping

from app.services.llm.contracts import ChatMessage, ChatRole, TokenUsage
from tools.contracts import ToolCall


MAX_CASES = 500
MAX_SUITE_BYTES = 16 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024
MAX_TEXT_CHARS = 32 * 1024
MAX_SETUP_BYTES = 1024 * 1024
MAX_TRIALS = 20
_CASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TAG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_BLOCKED_PATH_PARTS = {".git", ".agents", "logs", "data", "__pycache__"}
_BLOCKED_EXACT_PATHS = {"AGENTS.md"}


class EvaluationConfigError(ValueError):
    """评测配置错误，正文可安全展示给本地开发者。"""


@dataclass(frozen=True, slots=True)
class ReplayStep:
    """回放 Provider 的单个确定性模型步骤。"""

    output_text: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    token_usage: TokenUsage | None = None
    upstream_status: int = 200


@dataclass(frozen=True, slots=True)
class CaseSetup:
    """单个 Case 在临时环境中的受控初始状态。"""

    files: tuple[tuple[str, str], ...] = ()
    messages: tuple[ChatMessage, ...] = ()
    summary: str | None = None
    summarized_message_count: int = 0
    preferences: tuple[str, ...] = ()
    approvals: tuple[tuple[str, bool], ...] = ()


@dataclass(frozen=True, slots=True)
class FileExpectation:
    """Trial 完成后对临时工作区文件的断言。"""

    path: str
    exists: bool = True
    content: str | None = None
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class VisibleToolsExpectation:
    """指定模型步骤应看到或不应看到的工具。"""

    step: int
    contains: tuple[str, ...] = ()
    excludes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CaseExpectation:
    """确定性 Grader 支持的全部预期。"""

    output_contains: tuple[str, ...] = ()
    output_not_contains: tuple[str, ...] = ()
    output_regex: tuple[str, ...] = ()
    system_prompt_contains: tuple[str, ...] = ()
    request_contains: tuple[str, ...] = ()
    required_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    tool_sequence: tuple[str, ...] = ()
    approval_decisions: tuple[tuple[str, bool], ...] = ()
    visible_tools: tuple[VisibleToolsExpectation, ...] = ()
    files: tuple[FileExpectation, ...] = ()
    status: str = "success"
    error_code: str | None = None
    max_model_steps: int | None = None
    max_tool_calls: int | None = None
    max_total_tokens: int | None = None
    max_duration_ms: float | None = None


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    """一个可独立重复运行的 Agent 评测 Case。"""

    id: str
    input: str
    tags: tuple[str, ...] = ()
    setup: CaseSetup = CaseSetup()
    replay_turns: tuple[tuple[ReplayStep, ...], ...] = ()
    expected: CaseExpectation = CaseExpectation()


@dataclass(frozen=True, slots=True)
class EvaluationSuite:
    """从单个 JSONL 文件加载的有序 Case 集合。"""

    name: str
    path: str
    cases: tuple[EvaluationCase, ...]


@dataclass(frozen=True, slots=True)
class HarnessFingerprint:
    """用于解释评测差异的稳定 Harness 元数据。"""

    git_head: str | None
    git_dirty: bool
    agents_sha256: str | None
    skills_sha256: str
    tools_sha256: str
    policy_sha256: str


@dataclass(frozen=True, slots=True)
class ModelStepTrace:
    step_number: int
    duration_ms: float
    upstream_status: int
    output_chars: int
    tool_names: tuple[str, ...]
    visible_tools: tuple[str, ...]
    token_usage: TokenUsage | None


@dataclass(frozen=True, slots=True)
class ToolCallTrace:
    step_number: int
    call_id: str
    tool_name: str
    arguments: object
    approval: bool | None
    status: str | None
    duration_ms: float | None
    result: object | None


@dataclass(frozen=True, slots=True)
class AgentRunTrace:
    run_id: str
    case_id: str
    trial: int
    mode: str
    provider: str
    model: str
    request_id: str | None
    started_at: str
    completed_at: str
    duration_ms: float
    status: str
    output_text: str | None
    error_code: str | None
    error_message: str | None
    request_messages: tuple[ChatMessage, ...]
    model_steps: tuple[ModelStepTrace, ...]
    tool_calls: tuple[ToolCallTrace, ...]
    token_usage: TokenUsage | None
    fingerprint: HarnessFingerprint
    workspace_files: tuple[tuple[str, str], ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AssertionResult:
    name: str
    dimension: str
    passed: bool
    evidence: str


@dataclass(frozen=True, slots=True)
class TrialResult:
    case_id: str
    trial: int
    passed: bool
    score: float
    dimension_scores: Mapping[str, float]
    assertions: tuple[AssertionResult, ...]
    trace: AgentRunTrace
    judge: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class CaseResult:
    case_id: str
    tags: tuple[str, ...]
    input_text: str
    expected: Mapping[str, object]
    passed: bool
    pass_rate: float
    score_mean: float
    score_min: float
    score_max: float
    score_stddev: float
    trials: tuple[TrialResult, ...]


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    version: int
    run_id: str
    suite_name: str
    suite_path: str
    mode: str
    provider: str
    model: str
    created_at: str
    trials_per_case: int
    passed: bool
    pass_rate: float
    score: float
    total_tokens: int | None
    duration_ms: float
    fingerprint: HarnessFingerprint
    cases: tuple[CaseResult, ...]
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """转换为基础类型；具体落盘前仍需执行报告级脱敏。"""

        return asdict(self)


def load_suite(path: Path | str, *, require_replay: bool = True) -> EvaluationSuite:
    """严格加载 JSONL Suite，不允许跳过损坏行。"""

    suite_path = Path(path)
    cases: list[EvaluationCase] = []
    seen: set[str] = set()
    try:
        with suite_path.open("rb") as handle:
            content = handle.read(MAX_SUITE_BYTES + 1)
    except OSError as exc:
        raise EvaluationConfigError(f"{suite_path}: unable to read suite") from exc
    if len(content) > MAX_SUITE_BYTES:
        raise EvaluationConfigError(f"{suite_path}: suite is too large")
    lines = content.splitlines()
    for line_number, raw_line in enumerate(lines, 1):
        if not raw_line.strip():
            continue
        if len(raw_line) > MAX_LINE_BYTES:
            raise _line_error(suite_path, line_number, None, "line is too large")
        try:
            payload = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _line_error(suite_path, line_number, None, "invalid JSON") from exc
        case_id = payload.get("id") if isinstance(payload, dict) else None
        try:
            case = _parse_case(payload, require_replay=require_replay)
        except (TypeError, ValueError, re.error) as exc:
            raise _line_error(suite_path, line_number, case_id, str(exc)) from exc
        if case.id in seen:
            raise _line_error(suite_path, line_number, case.id, "duplicate case id")
        seen.add(case.id)
        cases.append(case)
        if len(cases) > MAX_CASES:
            raise EvaluationConfigError(f"{suite_path}: suite has too many cases")
    if not cases:
        raise EvaluationConfigError(f"{suite_path}: suite is empty")
    return EvaluationSuite(suite_path.stem, str(suite_path), tuple(cases))


def _parse_case(payload: object, *, require_replay: bool) -> EvaluationCase:
    data = _object(payload, "case")
    _fields(data, {"id", "input", "tags", "setup", "replay_steps", "replay_turns", "expected"}, {"id", "input", "expected"})
    case_id = _text(data["id"], "id", max_chars=128)
    if not _CASE_ID_PATTERN.fullmatch(case_id):
        raise ValueError("id is invalid")
    input_text = _text(data["input"], "input")
    tags = _unique_text_list(data.get("tags", []), "tags", max_items=32, max_chars=64)
    if any(not _TAG_PATTERN.fullmatch(tag) for tag in tags):
        raise ValueError("tag is invalid")
    setup = _parse_setup(data.get("setup", {}))
    if "replay_steps" in data and "replay_turns" in data:
        raise ValueError("replay_steps and replay_turns are mutually exclusive")
    if "replay_steps" in data:
        replay_turns = (_parse_steps(data["replay_steps"]),)
    elif "replay_turns" in data:
        raw_turns = _list(data["replay_turns"], "replay_turns", max_items=32)
        replay_turns = tuple(_parse_steps(item) for item in raw_turns)
    else:
        replay_turns = ()
    if require_replay and not replay_turns:
        raise ValueError("replay steps are required")
    call_ids = [call.call_id for turn in replay_turns for step in turn for call in step.tool_calls]
    if len(call_ids) != len(set(call_ids)):
        raise ValueError("replay tool call ids contain duplicates")
    return EvaluationCase(case_id, input_text, tags, setup, replay_turns, _parse_expected(data["expected"]))


def _parse_setup(payload: object) -> CaseSetup:
    data = _object(payload, "setup")
    _fields(data, {"files", "messages", "summary", "summarized_message_count", "preferences", "approvals"})
    raw_files = _object(data.get("files", {}), "setup.files")
    if len(raw_files) > 100:
        raise ValueError("setup has too many files")
    files: list[tuple[str, str]] = []
    total_bytes = 0
    for raw_path, raw_content in raw_files.items():
        path = validate_relative_path(raw_path)
        content = _bounded_text(raw_content, f"setup.files.{path}")
        total_bytes += len(content.encode("utf-8"))
        files.append((path, content))
    if total_bytes > MAX_SETUP_BYTES:
        raise ValueError("setup files are too large")
    messages = _parse_messages(data.get("messages", []))
    summary = data.get("summary")
    if summary is not None:
        summary = _text(summary, "setup.summary")
    boundary = _integer(data.get("summarized_message_count", 0), "setup.summarized_message_count", minimum=0)
    if boundary > len(messages) or boundary % 2:
        raise ValueError("setup summary boundary is invalid")
    preferences = _unique_text_list(data.get("preferences", []), "setup.preferences", max_items=50, max_chars=500)
    raw_approvals = _object(data.get("approvals", {}), "setup.approvals")
    approvals: list[tuple[str, bool]] = []
    for name, decision in raw_approvals.items():
        _tool_name(name)
        if type(decision) is not bool:
            raise ValueError("setup approval decision must be boolean")
        approvals.append((name, decision))
    return CaseSetup(tuple(sorted(files)), messages, summary, boundary, preferences, tuple(sorted(approvals)))


def _parse_messages(payload: object) -> tuple[ChatMessage, ...]:
    items = _list(payload, "setup.messages", max_items=200)
    messages: list[ChatMessage] = []
    for index, item in enumerate(items):
        data = _object(item, f"setup.messages[{index}]")
        _fields(data, {"role", "content"}, {"role", "content"})
        expected = ChatRole.USER if index % 2 == 0 else ChatRole.ASSISTANT
        try:
            role = ChatRole(data["role"])
        except (TypeError, ValueError) as exc:
            raise ValueError("setup message role is invalid") from exc
        if role is not expected:
            raise ValueError("setup messages must contain complete alternating turns")
        messages.append(ChatMessage(role, _text(data["content"], "setup message content")))
    if len(messages) % 2:
        raise ValueError("setup messages must contain complete alternating turns")
    return tuple(messages)


def _parse_steps(payload: object) -> tuple[ReplayStep, ...]:
    items = _list(payload, "replay steps", min_items=1, max_items=64)
    steps: list[ReplayStep] = []
    for index, item in enumerate(items):
        data = _object(item, f"replay step {index}")
        _fields(data, {"output_text", "tool_calls", "token_usage", "upstream_status"})
        output = data.get("output_text")
        if output is not None:
            output = _text(output, "replay output")
        raw_calls = _list(data.get("tool_calls", []), "tool_calls", max_items=16)
        calls: list[ToolCall] = []
        for call_index, raw_call in enumerate(raw_calls):
            call = _object(raw_call, f"tool_calls[{call_index}]")
            _fields(call, {"call_id", "name", "arguments"}, {"call_id", "name", "arguments"})
            call_id = _text(call["call_id"], "call_id", max_chars=128)
            if any(not character.isprintable() for character in call_id):
                raise ValueError("call_id is invalid")
            name = _tool_name(call["name"])
            arguments = _object(call["arguments"], "tool arguments")
            arguments_json = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            calls.append(ToolCall(call_id, name, arguments_json))
        if bool(output) == bool(calls):
            raise ValueError("replay step must contain exactly one of output_text or tool_calls")
        usage = _parse_usage(data.get("token_usage"))
        status = _integer(data.get("upstream_status", 200), "upstream_status", minimum=100, maximum=599)
        steps.append(ReplayStep(output, tuple(calls), usage, status))
    return tuple(steps)


def _parse_usage(payload: object) -> TokenUsage | None:
    if payload is None:
        return None
    data = _object(payload, "token_usage")
    _fields(data, {"input_tokens", "output_tokens", "total_tokens"}, {"input_tokens", "output_tokens"})
    input_tokens = _integer(data["input_tokens"], "input_tokens", minimum=0)
    output_tokens = _integer(data["output_tokens"], "output_tokens", minimum=0)
    total_tokens = data.get("total_tokens", input_tokens + output_tokens)
    return TokenUsage(input_tokens, output_tokens, _integer(total_tokens, "total_tokens", minimum=0))


def _parse_expected(payload: object) -> CaseExpectation:
    data = _object(payload, "expected")
    allowed = {
        "output_contains", "output_not_contains", "output_regex", "system_prompt_contains", "request_contains",
        "required_tools", "forbidden_tools", "tool_sequence", "approval_decisions", "visible_tools", "files", "status", "error_code",
        "max_model_steps", "max_tool_calls", "max_total_tokens", "max_duration_ms",
    }
    _fields(data, allowed)
    patterns = _unique_text_list(data.get("output_regex", []), "output_regex", max_items=32, max_chars=256)
    for pattern in patterns:
        re.compile(pattern)
    required = _tool_list(data.get("required_tools", []), "required_tools")
    forbidden = _tool_list(data.get("forbidden_tools", []), "forbidden_tools")
    if set(required) & set(forbidden):
        raise ValueError("required_tools and forbidden_tools overlap")
    sequence = _tool_list(data.get("tool_sequence", []), "tool_sequence", unique=False)
    raw_decisions = _object(data.get("approval_decisions", {}), "approval_decisions")
    decisions: list[tuple[str, bool]] = []
    for name, decision in raw_decisions.items():
        _tool_name(name)
        if type(decision) is not bool:
            raise ValueError("approval decision must be boolean")
        decisions.append((name, decision))
    raw_files = _list(data.get("files", []), "expected.files", max_items=100)
    files = tuple(_parse_file_expectation(item) for item in raw_files)
    if len({item.path for item in files}) != len(files):
        raise ValueError("file expectations contain duplicate paths")
    raw_visible = _list(data.get("visible_tools", []), "visible_tools", max_items=64)
    visible = tuple(_parse_visible_tools(item) for item in raw_visible)
    if len({item.step for item in visible}) != len(visible):
        raise ValueError("visible_tools contains duplicate steps")
    status = data.get("status", "success")
    if status not in {"success", "error", "passed"}:
        raise ValueError("expected status is invalid")
    error_code = data.get("error_code")
    if error_code is not None:
        error_code = _text(error_code, "error_code", max_chars=64)
    if status in {"success", "passed"} and error_code is not None:
        raise ValueError("successful status cannot expect an error code")
    return CaseExpectation(
        _unique_text_list(data.get("output_contains", []), "output_contains"),
        _unique_text_list(data.get("output_not_contains", []), "output_not_contains"),
        patterns,
        _unique_text_list(data.get("system_prompt_contains", []), "system_prompt_contains"),
        _unique_text_list(data.get("request_contains", []), "request_contains"),
        required,
        forbidden,
        sequence,
        tuple(sorted(decisions)),
        visible,
        files,
        status,
        error_code,
        _optional_integer(data.get("max_model_steps"), "max_model_steps", minimum=1),
        _optional_integer(data.get("max_tool_calls"), "max_tool_calls", minimum=0),
        _optional_integer(data.get("max_total_tokens"), "max_total_tokens", minimum=0),
        _optional_number(data.get("max_duration_ms"), "max_duration_ms", minimum=0),
    )


def _parse_visible_tools(payload: object) -> VisibleToolsExpectation:
    data = _object(payload, "visible tools expectation")
    _fields(data, {"step", "contains", "excludes"}, {"step"})
    contains = _tool_list(data.get("contains", []), "visible_tools.contains")
    excludes = _tool_list(data.get("excludes", []), "visible_tools.excludes")
    if set(contains) & set(excludes):
        raise ValueError("visible tool expectations overlap")
    if not contains and not excludes:
        raise ValueError("visible tool expectation is empty")
    return VisibleToolsExpectation(
        _integer(data["step"], "visible_tools.step", minimum=1),
        contains,
        excludes,
    )


def _parse_file_expectation(payload: object) -> FileExpectation:
    data = _object(payload, "file expectation")
    _fields(data, {"path", "exists", "content", "sha256"}, {"path"})
    path = validate_relative_path(data["path"])
    exists = data.get("exists", True)
    if type(exists) is not bool:
        raise ValueError("file exists must be boolean")
    content = data.get("content")
    if content is not None:
        content = _bounded_text(content, "file content")
    digest = data.get("sha256")
    if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
        raise ValueError("file sha256 is invalid")
    if not exists and (content is not None or digest is not None):
        raise ValueError("missing file cannot assert content")
    return FileExpectation(path, exists, content, digest)


def validate_relative_path(value: object) -> str:
    path_text = _text(value, "path", max_chars=1024)
    path = PurePosixPath(path_text)
    parts = path.parts
    if (
        path.is_absolute()
        or not parts
        or "\\" in path_text
        or any(ord(character) < 32 or ord(character) == 127 for character in path_text)
        or path_text != path.as_posix()
        or path_text in _BLOCKED_EXACT_PATHS
        or any(part in {"", ".", ".."} for part in parts)
        or any(part in _BLOCKED_PATH_PARTS for part in parts)
        or any(part == ".env" or part.startswith(".env.") for part in parts)
    ):
        raise ValueError("path is unsafe")
    return path_text


def _object(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object")
    return value


def _list(value: object, name: str, *, min_items: int = 0, max_items: int = 500) -> list[object]:
    if not isinstance(value, list) or not min_items <= len(value) <= max_items:
        raise ValueError(f"{name} must be a bounded list")
    return value


def _fields(data: Mapping[str, object], allowed: set[str], required: set[str] | None = None) -> None:
    unknown = set(data) - allowed
    missing = (required or set()) - set(data)
    if unknown:
        raise ValueError(f"unknown field: {sorted(unknown)[0]}")
    if missing:
        raise ValueError(f"missing field: {sorted(missing)[0]}")


def _text(value: object, name: str, *, max_chars: int = MAX_TEXT_CHARS) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_chars or "\x00" in value:
        raise ValueError(f"{name} must be bounded nonblank text")
    return value


def _bounded_text(value: object, name: str, *, max_chars: int = MAX_TEXT_CHARS) -> str:
    """允许空正文，但拒绝 NUL 与无界内容。"""

    if not isinstance(value, str) or len(value) > max_chars or "\x00" in value:
        raise ValueError(f"{name} must be bounded text")
    return value


def _integer(value: object, name: str, *, minimum: int, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"{name} is invalid")
    return value


def _optional_integer(value: object, name: str, *, minimum: int) -> int | None:
    return None if value is None else _integer(value, name, minimum=minimum)


def _optional_number(value: object, name: str, *, minimum: float) -> float | None:
    if value is None:
        return None
    if type(value) not in {int, float} or value < minimum:
        raise ValueError(f"{name} is invalid")
    return float(value)


def _unique_text_list(value: object, name: str, *, max_items: int = 64, max_chars: int = MAX_TEXT_CHARS, unique: bool = True) -> tuple[str, ...]:
    items = _list(value, name, max_items=max_items)
    normalized = tuple(_text(item, name, max_chars=max_chars) for item in items)
    if unique and len(set(normalized)) != len(normalized):
        raise ValueError(f"{name} contains duplicates")
    return normalized


def _tool_name(value: object) -> str:
    name = _text(value, "tool name", max_chars=64)
    if not _TOOL_NAME_PATTERN.fullmatch(name):
        raise ValueError("tool name is invalid")
    return name


def _tool_list(value: object, name: str, *, unique: bool = True) -> tuple[str, ...]:
    items = _list(value, name, max_items=64)
    names = tuple(_tool_name(item) for item in items)
    if unique and len(set(names)) != len(names):
        raise ValueError(f"{name} contains duplicates")
    return names


def _line_error(path: Path, line: int, case_id: object, reason: str) -> EvaluationConfigError:
    label = case_id if isinstance(case_id, str) and case_id else "?"
    return EvaluationConfigError(f"{path}:{line}: case '{label}': {reason}")
