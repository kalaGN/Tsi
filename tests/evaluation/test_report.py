import asyncio
import json

import pytest

from app.evaluation.contracts import EvaluationConfigError, load_suite
from app.evaluation.report import compare_reports, load_report, render_markdown, safe_report_dict, write_report
from app.evaluation.runner import run_suite


def _report(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    suite_path = tmp_path / "core.jsonl"
    suite_path.write_text(
        json.dumps(
            {
                "id": "direct",
                "input": "包含 password=very-secret",
                "replay_steps": [{"output_text": "完成", "token_usage": {"input_tokens": 1, "output_tokens": 1}}],
                "expected": {"output_contains": ["完成"]},
            }
        ) + "\n",
        encoding="utf-8",
    )
    return asyncio.run(run_suite(load_suite(suite_path), project))


def test_report_omits_request_content_and_writes_two_formats(tmp_path):
    report = _report(tmp_path)
    payload = safe_report_dict(report)
    encoded = json.dumps(payload, ensure_ascii=False)

    assert "very-secret" not in encoded
    assert "request_messages" not in encoded

    paths = write_report(report, tmp_path / "reports")
    assert paths.json_path.name[:8].isdigit()
    assert load_report(paths.json_path)["run_id"] == report.run_id
    assert "# Agent 评测报告" in paths.markdown_path.read_text(encoding="utf-8")


def test_compare_reports_detects_score_rate_and_safety_regression():
    baseline = {
        "mode": "replay",
        "score": 100,
        "pass_rate": 100,
        "fingerprint": {"a": 1},
        "cases": [{"case_id": "safe", "tags": ["safety"], "passed": True}],
    }
    candidate = {
        "mode": "replay",
        "score": 90,
        "pass_rate": 80,
        "fingerprint": {"a": 2},
        "cases": [{"case_id": "safe", "tags": ["safety"], "passed": False}],
    }

    result = compare_reports(baseline, candidate)

    assert result.comparable is True
    assert result.regressed is True
    assert len(result.reasons) == 3
    assert "Harness 指纹发生变化" in result.warnings


def test_markdown_report_escapes_dynamic_table_content():
    markdown = render_markdown(
        {
            "cases": [
                {"case_id": "case|one", "tags": ["a|b"], "passed": True, "pass_rate": 100, "score_mean": 100}
            ]
        }
    )

    assert "`case\\|one`" in markdown
    assert "a\\|b" in markdown


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -1, 101])
def test_compare_reports_rejects_invalid_metrics(invalid):
    report = {
        "mode": "replay",
        "score": invalid,
        "pass_rate": 100,
        "fingerprint": {},
        "cases": [],
    }

    with pytest.raises(EvaluationConfigError, match="score is invalid"):
        compare_reports(report, {**report, "score": 100})


def test_load_report_rejects_oversized_input(tmp_path, monkeypatch):
    from app.evaluation import report as report_module

    path = tmp_path / "large.json"
    path.write_bytes(b"{}{}")
    monkeypatch.setattr(report_module, "MAX_REPORT_BYTES", 3)

    with pytest.raises(EvaluationConfigError, match="report is too large"):
        load_report(path)
