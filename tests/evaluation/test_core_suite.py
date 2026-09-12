import json
import asyncio
from pathlib import Path

from app.evaluation.contracts import load_suite
from app.evaluation.runner import run_suite
from app.evaluation.report import compare_reports, safe_report_dict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_core_replay_suite_passes_without_network():
    suite = load_suite(PROJECT_ROOT / "evals" / "cases" / "core.jsonl")

    report = asyncio.run(run_suite(suite, PROJECT_ROOT))

    assert len(report.cases) == 9
    assert report.passed is True
    assert report.pass_rate == 100
    assert report.score == 100
    baseline = json.loads((PROJECT_ROOT / "evals" / "baselines" / "core.json").read_text(encoding="utf-8"))
    comparison = compare_reports(baseline, safe_report_dict(report))
    assert comparison.comparable is True
    assert comparison.regressed is False


def test_live_suite_is_valid_without_replay_scripts():
    suite = load_suite(
        PROJECT_ROOT / "evals" / "cases" / "core-live.jsonl",
        require_replay=False,
    )

    assert len(suite.cases) == 6
    assert all(not case.replay_turns for case in suite.cases)
