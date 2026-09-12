"""评测报告的安全序列化、Markdown 渲染和基线比较。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping

from app.evaluation.contracts import EvaluationConfigError, EvaluationReport
from app.evaluation.observer import sanitize_value


MAX_REPORT_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ReportPaths:
    json_path: Path
    markdown_path: Path


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    comparable: bool
    regressed: bool
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]


def safe_report_dict(report: EvaluationReport) -> dict[str, object]:
    """移除完整请求上下文后递归脱敏，生成唯一可落盘表示。"""

    payload = report.to_dict()
    suite_path = payload.get("suite_path")
    if isinstance(suite_path, str):
        payload["suite_path"] = Path(suite_path).name
    for case in payload.get("cases", []):
        if not isinstance(case, dict):
            continue
        for trial in case.get("trials", []):
            if not isinstance(trial, dict):
                continue
            trace = trial.get("trace")
            if not isinstance(trace, dict):
                continue
            messages = trace.pop("request_messages", [])
            if isinstance(messages, list):
                roles: list[str] = []
                digests: list[str] = []
                for message in messages:
                    if isinstance(message, dict):
                        role = message.get("role", "unknown")
                        roles.append(str(getattr(role, "value", role)))
                        content = message.get("content", "")
                        digests.append(hashlib.sha256(str(content).encode("utf-8")).hexdigest())
                trace["request_message_roles"] = roles
                trace["request_message_sha256"] = digests
                trace["request_message_count"] = len(messages)
    sanitized = sanitize_value(payload)
    if not isinstance(sanitized, dict):
        raise EvaluationConfigError("report serialization failed")
    return sanitized


def write_report(report: EvaluationReport, output_directory: Path) -> ReportPaths:
    payload = safe_report_dict(report)
    stem = report_stem(report)
    return write_report_payload(payload, output_directory, stem)


def write_report_payload(payload: Mapping[str, object], output_directory: Path, stem: str) -> ReportPaths:
    """以原子替换写入同名 JSON 和中文 Markdown。"""

    safe_payload = sanitize_value(dict(payload))
    if not isinstance(safe_payload, dict):
        raise EvaluationConfigError("report serialization failed")
    try:
        directory = Path(output_directory)
        directory.mkdir(parents=True, exist_ok=True)
        json_path = directory / f"{stem}.json"
        markdown_path = directory / f"{stem}.md"
        if json_path.exists() or markdown_path.exists():
            raise EvaluationConfigError("report target already exists")
        encoded = json.dumps(safe_payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        _atomic_write(json_path, encoded)
        _atomic_write(markdown_path, render_markdown(safe_payload))
    except EvaluationConfigError:
        raise
    except OSError as exc:
        if "json_path" in locals():
            json_path.unlink(missing_ok=True)
        raise EvaluationConfigError("unable to write evaluation report") from exc
    return ReportPaths(json_path, markdown_path)


def report_stem(report: EvaluationReport) -> str:
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    components = (_safe_component(report.suite_name), _safe_component(report.provider), _safe_component(report.model), report.run_id[:8])
    return "-".join((timestamp, *components))


def render_markdown(payload: Mapping[str, object]) -> str:
    """从机器报告单向生成便于排查的中文摘要。"""

    cases = payload.get("cases", [])
    lines = [
        "# Agent 评测报告",
        "",
        f"- 运行 ID：`{payload.get('run_id', '-')}`",
        f"- 套件：`{payload.get('suite_name', '-')}`",
        f"- 模式：`{payload.get('mode', '-')}`",
        f"- 模型：`{payload.get('provider', '-')}/{payload.get('model', '-')}`",
        f"- 结果：{'通过' if payload.get('passed') else '失败'}",
        f"- 通过率：{payload.get('pass_rate', 0)}%",
        f"- 得分：{payload.get('score', 0)} / 100",
        f"- Token：{payload.get('total_tokens') if payload.get('total_tokens') is not None else '不可用'}",
        f"- 总耗时：{payload.get('duration_ms', 0)} ms",
        "",
        "## Case 结果",
        "",
        "| Case | 标签 | 结果 | 通过率 | 均分 |",
        "|---|---|---:|---:|---:|",
    ]
    if isinstance(cases, list):
        for case in cases:
            if not isinstance(case, dict):
                continue
            tags = _md_cell(", ".join(str(item) for item in case.get("tags", [])) or "-")
            lines.append(
                f"| `{_md_cell(case.get('case_id', '-'))}` | {tags} | "
                f"{'通过' if case.get('passed') else '失败'} | "
                f"{case.get('pass_rate', 0)}% | {case.get('score_mean', 0)} |"
            )
    failures = _failed_assertions(cases)
    lines.extend(("", "## 失败详情", ""))
    if not failures:
        lines.append("无。")
    else:
        for case_id, trial, assertion in failures:
            lines.extend(
                (
                    f"### {_md_cell(case_id)} / Trial {_md_cell(trial)}",
                    "",
                    f"- 断言：`{_md_cell(assertion.get('name', '-'))}`",
                    f"- 维度：`{_md_cell(assertion.get('dimension', '-'))}`",
                    f"- 证据：{_md_cell(assertion.get('evidence', '-'))}",
                    "",
                )
            )
    fingerprint = payload.get("fingerprint", {})
    lines.extend(("", "## Harness 指纹", "", "```json", json.dumps(fingerprint, ensure_ascii=False, sort_keys=True, indent=2), "```", ""))
    return "\n".join(lines)


def load_report(path: Path | str) -> dict[str, object]:
    report_path = Path(path)
    try:
        with report_path.open("rb") as handle:
            content = handle.read(MAX_REPORT_BYTES + 1)
        if len(content) > MAX_REPORT_BYTES:
            raise EvaluationConfigError(f"{report_path}: report is too large")
        payload = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvaluationConfigError(f"{report_path}: invalid report") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("cases"), list):
        raise EvaluationConfigError(f"{report_path}: unsupported report schema")
    return payload


def compare_reports(baseline: Mapping[str, object], candidate: Mapping[str, object]) -> ComparisonResult:
    reasons: list[str] = []
    warnings: list[str] = []
    if baseline.get("mode") != candidate.get("mode"):
        return ComparisonResult(False, False, (), ("运行模式不同，无法直接比较",))
    if baseline.get("mode") == "live" and (baseline.get("provider"), baseline.get("model")) != (candidate.get("provider"), candidate.get("model")):
        return ComparisonResult(False, False, (), ("真实评测的供应商或模型不同，无法直接比较",))
    baseline_score = _number(baseline.get("score"), "baseline score")
    candidate_score = _number(candidate.get("score"), "candidate score")
    baseline_rate = _number(baseline.get("pass_rate"), "baseline pass rate")
    candidate_rate = _number(candidate.get("pass_rate"), "candidate pass rate")
    if baseline_score - candidate_score > 3:
        reasons.append(f"总体得分下降 {round(baseline_score - candidate_score, 2)} 分")
    if baseline_rate - candidate_rate > 5:
        reasons.append(f"通过率下降 {round(baseline_rate - candidate_rate, 2)} 个百分点")
    baseline_cases = _case_map(baseline)
    candidate_cases = _case_map(candidate)
    for case_id in sorted(set(baseline_cases) & set(candidate_cases)):
        old = baseline_cases[case_id]
        new = candidate_cases[case_id]
        if "safety" in old.get("tags", []) and old.get("passed") is True and new.get("passed") is not True:
            reasons.append(f"安全 Case 从通过退化为失败：{case_id}")
    added = sorted(set(candidate_cases) - set(baseline_cases))
    removed = sorted(set(baseline_cases) - set(candidate_cases))
    if added:
        warnings.append(f"候选新增 Case：{', '.join(added)}")
    if removed:
        warnings.append(f"候选缺少 Case：{', '.join(removed)}")
    if baseline.get("fingerprint") != candidate.get("fingerprint"):
        warnings.append("Harness 指纹发生变化")
    return ComparisonResult(True, bool(reasons), tuple(reasons), tuple(warnings))


def comparison_markdown(result: ComparisonResult) -> str:
    lines = ["# Agent 评测基线比较", "", f"- 可比较：{'是' if result.comparable else '否'}", f"- 回归：{'是' if result.regressed else '否'}"]
    if result.reasons:
        lines.extend(("", "## 回归原因", ""))
        lines.extend(f"- {item}" for item in result.reasons)
    if result.warnings:
        lines.extend(("", "## 提示", ""))
        lines.extend(f"- {item}" for item in result.warnings)
    return "\n".join(lines) + "\n"


def _atomic_write(path: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


def _safe_component(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return normalized[:64] or "unknown"


def _md_cell(value: object) -> str:
    """转义报告中的 Markdown 表格和行内动态文本。"""

    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _failed_assertions(cases: object):
    failures = []
    if not isinstance(cases, list):
        return failures
    for case in cases:
        if not isinstance(case, dict):
            continue
        for trial in case.get("trials", []):
            if not isinstance(trial, dict):
                continue
            for assertion in trial.get("assertions", []):
                if isinstance(assertion, dict) and assertion.get("passed") is False:
                    failures.append((case.get("case_id", "-"), trial.get("trial", "-"), assertion))
    return failures


def _number(value: object, name: str) -> float:
    if type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= 100:
        raise EvaluationConfigError(f"{name} is invalid")
    return float(value)


def _case_map(report: Mapping[str, object]) -> dict[str, dict[str, object]]:
    result = {}
    cases = report.get("cases")
    if not isinstance(cases, list):
        raise EvaluationConfigError("report cases are invalid")
    for item in cases:
        if not isinstance(item, dict) or not isinstance(item.get("case_id"), str):
            raise EvaluationConfigError("report case is invalid")
        if item["case_id"] in result:
            raise EvaluationConfigError("report contains duplicate case ids")
        result[item["case_id"]] = item
    return result
