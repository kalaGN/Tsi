import asyncio
import json

import pytest

from app.evaluation.__main__ import _all_live_trials_failed, main
from app.evaluation.contracts import load_suite
from budget_support import BudgetedTestTurn, estimate_test_request
from app.evaluation.runner import run_suite
from app.services.llm.contracts import ProviderTimeoutError


def test_run_cli_writes_report_and_returns_success(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    suite = tmp_path / "core.jsonl"
    suite.write_text(
        json.dumps({"id": "direct", "input": "x", "replay_steps": [{"output_text": "ok"}], "expected": {"output_contains": ["ok"]}}) + "\n",
        encoding="utf-8",
    )
    report_dir = tmp_path / "reports"

    code = main(["run", "--suite", str(suite), "--project-root", str(project), "--report-dir", str(report_dir)])

    assert code == 0
    assert len(list(report_dir.glob("*.json"))) == 1
    assert len(list(report_dir.glob("*.md"))) == 1


def test_run_cli_rejects_live_without_model(tmp_path):
    suite = tmp_path / "live.jsonl"
    suite.write_text('{"id":"live","input":"x","expected":{}}\n', encoding="utf-8")

    assert main(["run", "--suite", str(suite), "--project-root", str(tmp_path), "--live"]) == 2


def test_compare_cli_returns_regression_exit_code(tmp_path):
    base = {"version": 1, "mode": "replay", "score": 100, "pass_rate": 100, "fingerprint": {}, "cases": []}
    candidate = {**base, "score": 90}
    baseline_path = tmp_path / "base.json"
    candidate_path = tmp_path / "candidate.json"
    baseline_path.write_text(json.dumps(base), encoding="utf-8")
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")

    assert main(["compare", "--baseline", str(baseline_path), "--candidate", str(candidate_path)]) == 1


def test_removed_judge_command_is_not_exposed():
    with pytest.raises(SystemExit) as captured:
        main(["judge"])
    assert captured.value.code == 2


class _TimeoutTurn:
    async def next(self, tool_results=(), *, on_text_delta=None):
        raise ProviderTimeoutError()

    def replace_tools(self, tools):
        """超时发生在首步，动态工具替换不会触发。"""


class _TimeoutProvider:
    estimate_request = staticmethod(estimate_test_request)
    name = "deepseek"
    model = "timeout-model"
    api_key_configured = True

    def create_turn(self, messages, tools, *, request_id, **budget):
        return BudgetedTestTurn(_TimeoutTurn(), messages, tools, **budget)


def test_live_report_distinguishes_total_provider_failure(tmp_path):
    suite_path = tmp_path / "live.jsonl"
    suite_path.write_text(
        '{"id":"live","input":"x","expected":{}}\n',
        encoding="utf-8",
    )
    report = asyncio.run(
        run_suite(
            load_suite(suite_path, require_replay=False),
            tmp_path,
            live=True,
            provider_name="deepseek",
            model="timeout-model",
            provider_factory=lambda provider, model: _TimeoutProvider(),
        )
    )

    assert _all_live_trials_failed(report) is True
