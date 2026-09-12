import asyncio
import json

from app.evaluation import __main__ as evaluation_cli
from app.evaluation.__main__ import _all_live_trials_failed, main
from app.evaluation.contracts import ReplayStep, load_suite
from app.evaluation.replay import ReplayProvider
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


def test_judge_cli_writes_new_report_without_overwriting_source(
    tmp_path,
    monkeypatch,
):
    report = {
        "version": 1,
        "run_id": "run",
        "suite_name": "core",
        "mode": "replay",
        "provider": "replay",
        "model": "scripted-v1",
        "passed": True,
        "pass_rate": 100,
        "score": 100,
        "cases": [
            {
                "case_id": "one",
                "input_text": "问题",
                "expected": {},
                "trials": [
                    {"trace": {"output_text": "回答", "tool_calls": []}}
                ],
            }
        ],
    }
    source = tmp_path / "source.json"
    source.write_text(json.dumps(report), encoding="utf-8")
    judgement = (
        '{"correctness":{"score":5,"reason":"正确"},'
        '"completeness":{"score":5,"reason":"完整"},'
        '"relevance":{"score":5,"reason":"相关"}}'
    )
    provider = ReplayProvider(((ReplayStep(judgement),),))
    monkeypatch.setattr(
        evaluation_cli,
        "create_provider_for_model",
        lambda provider_name, model: provider,
    )

    code = main(
        [
            "judge",
            "--report",
            str(source),
            "--provider",
            "aliyun",
            "--model",
            "judge-model",
            "--output-dir",
            str(tmp_path / "judged"),
        ]
    )

    assert code == 0
    assert source.read_text(encoding="utf-8") == json.dumps(report)
    assert len(list((tmp_path / "judged").glob("*-judged.json"))) == 1


class _TimeoutTurn:
    async def next(self, tool_results=(), *, on_text_delta=None):
        raise ProviderTimeoutError()

    def replace_tools(self, tools):
        """超时发生在首步，动态工具替换不会触发。"""


class _TimeoutProvider:
    name = "deepseek"
    model = "timeout-model"
    api_key_configured = True

    def create_turn(self, messages, tools, *, request_id):
        return _TimeoutTurn()


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
