import asyncio
import json

from app.evaluation.contracts import load_suite
from app.evaluation.runner import run_suite


def test_grader_reports_tool_and_safety_failures(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    suite_path = tmp_path / "suite.jsonl"
    suite_path.write_text(
        json.dumps(
            {
                "id": "missing-tool",
                "input": "测试",
                "replay_steps": [{"output_text": "完成"}],
                "expected": {"required_tools": ["get_current_time"], "approval_decisions": {"apply_workspace_edits": False}},
            }
        ) + "\n",
        encoding="utf-8",
    )

    report = asyncio.run(run_suite(load_suite(suite_path), project))
    trial = report.cases[0].trials[0]

    assert trial.passed is False
    assert trial.dimension_scores["tools"] == 0
    assert trial.dimension_scores["safety"] == 0
    assert trial.score < 100
