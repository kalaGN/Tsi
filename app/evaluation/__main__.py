"""`python -m app.evaluation` 的命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from app.evaluation.contracts import EvaluationConfigError, load_suite
from app.evaluation.llm_judge import JudgeUnavailableError, judge_report
from app.evaluation.report import (
    compare_reports,
    comparison_markdown,
    load_report,
    write_report,
    write_report_payload,
)
from app.evaluation.runner import run_suite
from app.services.llm.factory import create_provider_for_model
from app.services.llm.contracts import LlmProviderError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.evaluation", description="Tsi 助手本地评测")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="运行评测套件")
    run_parser.add_argument("--suite", required=True, type=Path)
    run_parser.add_argument("--project-root", type=Path, default=Path.cwd())
    run_parser.add_argument("--report-dir", type=Path)
    run_parser.add_argument("--trials", type=int, default=1)
    run_parser.add_argument("--live", action="store_true")
    run_parser.add_argument("--provider", choices=("deepseek", "aliyun"))
    run_parser.add_argument("--model")

    compare_parser = subparsers.add_parser("compare", help="比较候选报告和基线")
    compare_parser.add_argument("--baseline", required=True, type=Path)
    compare_parser.add_argument("--candidate", required=True, type=Path)

    judge_parser = subparsers.add_parser("judge", help="为报告追加独立模型评分")
    judge_parser.add_argument("--report", required=True, type=Path)
    judge_parser.add_argument("--provider", required=True, choices=("deepseek", "aliyun"))
    judge_parser.add_argument("--model", required=True)
    judge_parser.add_argument("--output-dir", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            return _run(args)
        if args.command == "compare":
            return _compare(args)
        return _judge(args)
    except EvaluationConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except LlmProviderError as exc:
        print(exc.user_message, file=sys.stderr)
        return 3


def _run(args: argparse.Namespace) -> int:
    project_root = args.project_root.resolve()
    if args.live:
        load_dotenv(project_root / ".env", override=False)
    suite = load_suite(args.suite, require_replay=not args.live)
    print(
        f"计划运行：{len(suite.cases)} 个 Case × {args.trials} 个 Trial"
        f"（{'真实模型' if args.live else '离线回放'}）"
    )
    report = asyncio.run(
        run_suite(
            suite,
            project_root,
            trials=args.trials,
            live=args.live,
            provider_name=args.provider,
            model=args.model,
        )
    )
    output = args.report_dir or project_root / "evals" / "reports"
    paths = write_report(report, output)
    print(f"结果：{'通过' if report.passed else '失败'}，得分 {report.score}，通过率 {report.pass_rate}%")
    print(f"JSON：{paths.json_path}")
    print(f"Markdown：{paths.markdown_path}")
    if args.live and _all_live_trials_failed(report):
        return 3
    return 0 if report.passed else 1


def _compare(args: argparse.Namespace) -> int:
    result = compare_reports(load_report(args.baseline), load_report(args.candidate))
    print(comparison_markdown(result), end="")
    if not result.comparable:
        return 2
    return 1 if result.regressed else 0


def _judge(args: argparse.Namespace) -> int:
    project_root = Path.cwd()
    load_dotenv(project_root / ".env", override=False)
    provider = create_provider_for_model(args.provider, args.model)
    payload = load_report(args.report)
    try:
        judged, failures = asyncio.run(judge_report(payload, provider))
    except JudgeUnavailableError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    output = args.output_dir or args.report.parent
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    stem = f"{timestamp}-{args.report.stem}-judged"
    paths = write_report_payload(judged, output, stem)
    print(f"Judge 完成，失败 {failures} 项")
    print(f"JSON：{paths.json_path}")
    print(f"Markdown：{paths.markdown_path}")
    return 0


def _all_live_trials_failed(report) -> bool:
    """区分模型质量失败与完全没有得到有效真实模型结果。"""

    trials = [trial for case in report.cases for trial in case.trials]
    provider_failures = {
        "configuration",
        "timeout",
        "connection",
        "authentication",
        "upstream",
        "invalid_response",
    }
    return bool(trials) and all(
        trial.trace.status == "error"
        and trial.trace.error_code in provider_failures
        for trial in trials
    )


if __name__ == "__main__":
    raise SystemExit(main())
