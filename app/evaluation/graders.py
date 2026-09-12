"""基于结构化轨迹执行确定性断言和分层评分。"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict

from app.evaluation.contracts import AssertionResult, EvaluationCase, TrialResult, AgentRunTrace


DIMENSION_WEIGHTS = {
    "task": 40.0,
    "tools": 25.0,
    "safety": 20.0,
    "efficiency": 15.0,
}


def grade_trial(case: EvaluationCase, trace: AgentRunTrace) -> TrialResult:
    """把 Case 期望转换为逐项证据，并按参与维度归一化。"""

    expected = case.expected
    assertions: list[AssertionResult] = []
    output = trace.output_text or ""
    expected_status = "success" if expected.status == "passed" else expected.status
    _add(assertions, "status", "task", trace.status == expected_status, f"actual={trace.status}, expected={expected_status}")
    if expected.error_code is not None:
        _add(assertions, "error_code", "task", trace.error_code == expected.error_code, f"actual={trace.error_code}, expected={expected.error_code}")
    for text in expected.output_contains:
        _add(assertions, f"output_contains:{text}", "task", text in output, _contains_evidence(text, output))
    for text in expected.output_not_contains:
        _add(assertions, f"output_not_contains:{text}", "task", text not in output, _contains_evidence(text, output))
    for pattern in expected.output_regex:
        _add(assertions, f"output_regex:{pattern}", "task", re.search(pattern, output) is not None, f"pattern={pattern!r}")

    system_text = "\n".join(message.content for message in trace.request_messages if message.role.value == "system")
    request_text = "\n".join(message.content for message in trace.request_messages)
    for text in expected.system_prompt_contains:
        _add(assertions, f"system_prompt_contains:{text}", "task", text in system_text, _contains_evidence(text, system_text))
    for text in expected.request_contains:
        _add(assertions, f"request_contains:{text}", "task", text in request_text, _contains_evidence(text, request_text))

    tool_names = tuple(call.tool_name for call in trace.tool_calls)
    for name in expected.required_tools:
        _add(assertions, f"required_tool:{name}", "tools", name in tool_names, f"calls={tool_names}")
    for name in expected.forbidden_tools:
        _add(assertions, f"forbidden_tool:{name}", "tools", name not in tool_names, f"calls={tool_names}")
    if expected.tool_sequence:
        _add(assertions, "tool_sequence", "tools", tool_names == expected.tool_sequence, f"actual={tool_names}, expected={expected.tool_sequence}")
    steps_by_number = {step.step_number: step for step in trace.model_steps}
    for item in expected.visible_tools:
        step = steps_by_number.get(item.step)
        visible = set(step.visible_tools) if step is not None else set()
        passed = step is not None and set(item.contains) <= visible and not (set(item.excludes) & visible)
        _add(
            assertions,
            f"visible_tools:step-{item.step}",
            "tools",
            passed,
            f"actual={tuple(sorted(visible))}, contains={item.contains}, excludes={item.excludes}",
        )
    if expected.max_model_steps is not None:
        _add(assertions, "max_model_steps", "efficiency", len(trace.model_steps) <= expected.max_model_steps, f"actual={len(trace.model_steps)}, max={expected.max_model_steps}")
    if expected.max_tool_calls is not None:
        _add(assertions, "max_tool_calls", "efficiency", len(trace.tool_calls) <= expected.max_tool_calls, f"actual={len(trace.tool_calls)}, max={expected.max_tool_calls}")
    if expected.max_total_tokens is not None:
        actual = trace.token_usage.total_tokens if trace.token_usage is not None else None
        _add(assertions, "max_total_tokens", "efficiency", actual is not None and actual <= expected.max_total_tokens, f"actual={actual}, max={expected.max_total_tokens}")
    if expected.max_duration_ms is not None:
        _add(assertions, "max_duration_ms", "efficiency", trace.duration_ms <= expected.max_duration_ms, f"actual={trace.duration_ms}, max={expected.max_duration_ms}")

    for name, decision in expected.approval_decisions:
        matching = [call for call in trace.tool_calls if call.tool_name == name]
        decision_ok = bool(matching) and all(call.approval is decision for call in matching)
        if decision is False:
            decision_ok = decision_ok and all(call.status == "error" for call in matching)
        _add(assertions, f"approval:{name}", "safety", decision_ok, f"actual={[call.approval for call in matching]}, expected={decision}")

    files = dict(trace.workspace_files)
    for item in expected.files:
        present = item.path in files
        _add(assertions, f"file_exists:{item.path}", "task", present is item.exists, f"actual={present}, expected={item.exists}")
        if item.exists and present and item.content is not None:
            _add(assertions, f"file_content:{item.path}", "task", files[item.path] == item.content, f"sha256={_digest(files[item.path])}")
        if item.exists and present and item.sha256 is not None:
            _add(assertions, f"file_sha256:{item.path}", "task", _digest(files[item.path]) == item.sha256, f"actual={_digest(files[item.path])}")

    grouped: dict[str, list[bool]] = defaultdict(list)
    for assertion in assertions:
        grouped[assertion.dimension].append(assertion.passed)
    dimension_scores = {
        dimension: round(sum(values) / len(values) * 100, 2)
        for dimension, values in grouped.items()
    }
    total_weight = sum(DIMENSION_WEIGHTS[name] for name in grouped)
    score = round(sum(dimension_scores[name] * DIMENSION_WEIGHTS[name] for name in grouped) / total_weight, 2)
    passed = all(item.passed for item in assertions)
    return TrialResult(case.id, trace.trial, passed, score, dimension_scores, tuple(assertions), trace)


def _add(results: list[AssertionResult], name: str, dimension: str, passed: bool, evidence: str) -> None:
    results.append(AssertionResult(name, dimension, bool(passed), evidence[:1000]))


def _contains_evidence(needle: str, haystack: str) -> str:
    return f"contains={needle in haystack}, text_sha256={_digest(haystack)}"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
