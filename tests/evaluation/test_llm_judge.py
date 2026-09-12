import asyncio

import pytest

from app.evaluation.contracts import EvaluationConfigError, ReplayStep
from app.evaluation.llm_judge import judge_report
from app.evaluation.replay import ReplayProvider
from app.services.llm.contracts import TokenUsage


def _payload():
    return {
        "version": 1,
        "cases": [
            {
                "case_id": "one",
                "input_text": "问题",
                "expected": {},
                "trials": [{"trace": {"output_text": "回答", "tool_calls": []}}],
            }
        ],
    }


def test_judge_adds_strict_scores_and_separate_usage():
    response = '{"correctness":{"score":5,"reason":"正确"},"completeness":{"score":4,"reason":"完整"},"relevance":{"score":5,"reason":"相关"}}'
    provider = ReplayProvider(((ReplayStep(response, token_usage=TokenUsage(10, 5, 15)),),))

    judged, failures = asyncio.run(judge_report(_payload(), provider))

    assert failures == 0
    assert judged["cases"][0]["trials"][0]["judge"]["correctness"]["score"] == 5
    assert judged["judge"]["token_usage"]["total_tokens"] == 15
    judge_message = provider.created_messages[0][-1].content
    assert "arguments" not in judge_message
    assert "result" not in judge_message


def test_judge_rejects_all_invalid_responses():
    provider = ReplayProvider(((ReplayStep("not-json"),),))

    with pytest.raises(EvaluationConfigError, match="all judge calls failed"):
        asyncio.run(judge_report(_payload(), provider))
