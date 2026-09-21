import asyncio
import json

import pytest

from app.evaluation.contracts import EvaluationConfigError, load_suite
from app.evaluation.runner import run_suite
from app.runtime.model_budget import ModelBudget


def test_runner_uses_real_session_and_isolated_workspace(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("必须使用中文", encoding="utf-8")
    suite_path = tmp_path / "core.jsonl"
    suite_path.write_text(
        json.dumps(
            {
                "id": "direct",
                "input": "回答",
                "replay_steps": [{"output_text": "中文完成", "token_usage": {"input_tokens": 2, "output_tokens": 1}}],
                "expected": {
                    "output_contains": ["中文"],
                    "system_prompt_contains": ["必须使用中文"],
                    "max_model_steps": 1,
                    "max_total_tokens": 3,
                },
            },
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )

    report = asyncio.run(run_suite(load_suite(suite_path), project))

    assert report.passed is True
    assert report.score == 100
    assert report.total_tokens == 3
    assert report.cases[0].trials[0].trace.request_messages[0].role.value == "system"


def test_runner_requires_explicit_live_configuration(tmp_path):
    path = tmp_path / "live.jsonl"
    path.write_text('{"id":"live","input":"x","expected":{}}\n', encoding="utf-8")
    suite = load_suite(path, require_replay=False)

    with pytest.raises(EvaluationConfigError, match="requires provider and model"):
        asyncio.run(run_suite(suite, tmp_path, live=True))


def test_runner_continues_after_one_environment_failure(tmp_path):
    path = tmp_path / "suite.jsonl"
    cases = [
        {
            "id": "broken",
            "input": "x",
            "setup": {"preferences": ["这不是允许的开发偏好"]},
            "replay_steps": [{"output_text": "不会运行"}],
            "expected": {},
        },
        {
            "id": "healthy",
            "input": "x",
            "replay_steps": [{"output_text": "运行成功"}],
            "expected": {"output_contains": ["成功"]},
        },
    ]
    path.write_text("\n".join(json.dumps(case, ensure_ascii=False) for case in cases) + "\n", encoding="utf-8")

    report = asyncio.run(run_suite(load_suite(path), tmp_path))

    assert report.cases[0].passed is False
    assert report.cases[0].trials[0].trace.error_code == "evaluation_infrastructure"
    assert report.cases[1].passed is True


def test_runner_replays_summary_then_business_with_frozen_model_budget(tmp_path):
    summary = {"goal": "已确认目标", "decisions": [], "constraints": [],
               "completed": [], "pending": [], "references": [], "uncertainties": []}
    messages = [message for index in range(4) for message in (
        {"role": "user", "content": f"问{index}" + "中" * 180},
        {"role": "assistant", "content": f"答{index}" + "文" * 180},
    )]
    path = tmp_path / "compression.jsonl"
    path.write_text(json.dumps({
        "id": "summary-replay", "input": "继续", "setup": {"messages": messages},
        "replay_turns": [
            [{"output_text": json.dumps(summary, ensure_ascii=False)}],
            [{"output_text": "业务回答"}],
        ],
        "expected": {"output_contains": ["业务回答"], "system_prompt_contains": ["已确认目标"]},
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    budget = ModelBudget(context_window_tokens=12_800, trigger_percent=5,
                         target_percent=2, recent_turns=1)

    report = asyncio.run(run_suite(load_suite(path), tmp_path, model_budget=budget))

    assert report.passed
    assert report.cases[0].trials[0].trace.request_messages[-1].content == "继续"


def test_runner_rejects_irreducible_replay_input_without_calling_model(tmp_path):
    path = tmp_path / "oversized.jsonl"
    path.write_text(json.dumps({
        "id": "too-long", "input": "中" * 5000,
        "replay_steps": [{"output_text": "不应执行"}],
        "expected": {"status": "error", "error_code": "context_limit"},
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    report = asyncio.run(run_suite(load_suite(path), tmp_path,
                                   model_budget=ModelBudget(context_window_tokens=12_800)))
    assert report.cases[0].trials[0].trace.error_code == "context_limit"
