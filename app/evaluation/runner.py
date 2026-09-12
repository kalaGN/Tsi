"""评测 Suite、Case 与 Trial 的隔离执行编排。"""

from __future__ import annotations

import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, pstdev
from uuid import uuid4

from app.evaluation.contracts import (
    AgentRunTrace,
    CaseResult,
    EvaluationConfigError,
    EvaluationReport,
    EvaluationSuite,
    HarnessFingerprint,
    MAX_TRIALS,
)
from app.evaluation.environment import create_evaluation_environment
from app.evaluation.graders import grade_trial
from app.evaluation.observer import TraceCollector
from app.evaluation.replay import ReplayProvider
from app.runtime.chat import ChatRuntimeError
from app.runtime.memory import MemoryPolicy
from app.runtime.tool_loop import WORKSPACE_TOOL_LOOP_LIMITS, ToolLoopLimits
from app.services.llm.contracts import LlmProvider
from app.services.llm.factory import create_provider_for_model


def run_id() -> str:
    return uuid4().hex


async def run_suite(
    suite: EvaluationSuite,
    project_root: Path,
    *,
    trials: int = 1,
    live: bool = False,
    provider_name: str | None = None,
    model: str | None = None,
    provider_factory=create_provider_for_model,
    memory_policy: MemoryPolicy = MemoryPolicy(),
    tool_loop_limits: ToolLoopLimits = WORKSPACE_TOOL_LOOP_LIMITS,
) -> EvaluationReport:
    """串行执行完整 Suite，单个 Trial 失败不阻断其余 Case。"""

    if type(trials) is not int or not 1 <= trials <= MAX_TRIALS:
        raise EvaluationConfigError("trials must be between 1 and 20")
    if live and (not provider_name or not model):
        raise EvaluationConfigError("live mode requires provider and model")
    if not live and (provider_name is not None or model is not None):
        raise EvaluationConfigError("provider and model require live mode")

    active_run_id = run_id()
    started = time.monotonic()
    case_results: list[CaseResult] = []
    report_fingerprint = None
    actual_provider = provider_name or "replay"
    actual_model = model or "scripted-v1"
    for case in suite.cases:
        trial_results = []
        for trial_number in range(1, trials + 1):
            provider: LlmProvider
            replay = None
            if live:
                provider = provider_factory(provider_name, model)
            else:
                replay = ReplayProvider(case.replay_turns)
                provider = replay
            try:
                environment = create_evaluation_environment(
                    Path(project_root),
                    case.setup,
                    provider,
                    memory_policy=memory_policy,
                    tool_loop_limits=tool_loop_limits,
                )
            except Exception as exc:
                trace = _infrastructure_trace(
                    active_run_id,
                    case.id,
                    trial_number,
                    "live" if live else "replay",
                    actual_provider,
                    actual_model,
                    exc,
                )
                report_fingerprint = report_fingerprint or trace.fingerprint
                trial_results.append(grade_trial(case, trace))
                continue
            if (
                report_fingerprint is None
                or report_fingerprint.tools_sha256 == "0" * 64
            ):
                report_fingerprint = environment.fingerprint
            collector = TraceCollector(
                run_id=active_run_id,
                case_id=case.id,
                trial=trial_number,
                mode="live" if live else "replay",
                provider=actual_provider,
                model=actual_model,
                fingerprint=environment.fingerprint,
                workspace_root=environment.workspace,
            )
            try:
                await environment.session.send(
                    case.input,
                    on_tool_approval=environment.approve,
                    trace_observer=collector,
                )
                if replay is not None:
                    replay.assert_consumed()
            except ChatRuntimeError as exc:
                collector.force_failure(exc.code.value, exc.user_message)
                if replay is not None:
                    try:
                        replay.assert_consumed()
                    except EvaluationConfigError as exc:
                        collector.force_failure("evaluation_config", str(exc))
            except EvaluationConfigError as exc:
                collector.force_failure("evaluation_config", str(exc))
            except Exception as exc:
                collector.force_failure(
                    "evaluation_infrastructure",
                    f"{type(exc).__name__}: {exc}",
                )
            finally:
                workspace_files = environment.capture_files(case.expected.files)
                warnings: list[str] = []
                try:
                    environment.close()
                except OSError:
                    warnings.append("Unable to clean evaluation temporary directory")
                trace = collector.finalize(
                    workspace_files=workspace_files,
                    warnings=tuple(warnings),
                )
            trial_results.append(grade_trial(case, trace))
        scores = [item.score for item in trial_results]
        passed_trials = sum(item.passed for item in trial_results)
        case_results.append(
            CaseResult(
                case.id,
                case.tags,
                case.input,
                asdict(case.expected),
                passed_trials == len(trial_results),
                round(passed_trials / len(trial_results) * 100, 2),
                round(mean(scores), 2),
                round(min(scores), 2),
                round(max(scores), 2),
                round(pstdev(scores), 2),
                tuple(trial_results),
            )
        )
    if report_fingerprint is None:
        raise EvaluationConfigError("suite produced no trials")
    total_trials = sum(len(case.trials) for case in case_results)
    passed_trials = sum(sum(trial.passed for trial in case.trials) for case in case_results)
    usages = [trial.trace.token_usage for case in case_results for trial in case.trials]
    total_tokens = None if any(item is None for item in usages) else sum(item.total_tokens for item in usages if item is not None)
    return EvaluationReport(
        1,
        active_run_id,
        suite.name,
        suite.path,
        "live" if live else "replay",
        actual_provider,
        actual_model,
        datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        trials,
        all(case.passed for case in case_results),
        round(passed_trials / total_trials * 100, 2),
        round(mean(case.score_mean for case in case_results), 2),
        total_tokens,
        round((time.monotonic() - started) * 1000, 2),
        report_fingerprint,
        tuple(case_results),
    )


def _infrastructure_trace(
    active_run_id: str,
    case_id: str,
    trial: int,
    mode: str,
    provider: str,
    model: str,
    error: Exception,
) -> AgentRunTrace:
    """让单个环境装配失败进入报告，而不是中断剩余 Case。"""

    timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    empty_digest = "0" * 64
    fingerprint = HarnessFingerprint(None, False, None, empty_digest, empty_digest, empty_digest)
    return AgentRunTrace(
        active_run_id,
        case_id,
        trial,
        mode,
        provider,
        model,
        None,
        timestamp,
        timestamp,
        0.0,
        "error",
        None,
        "evaluation_infrastructure",
        f"{type(error).__name__}: {error}",
        (),
        (),
        (),
        None,
        fingerprint,
    )
