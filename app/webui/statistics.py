"""Web UI 请求统计的有界聚合与本地原子持久化。"""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

from app.observability.model_logging import log_web_statistics_error
from app.services.llm.contracts import TokenUsage


STATISTICS_VERSION = 1
RETENTION_DAYS = 30
MAX_MODEL_BUCKETS = 100
MAX_LABEL_LENGTH = 128
OTHER_PROVIDER = "other"
OTHER_MODEL = "其他模型"


class WebStatisticsStoreError(Exception):
    """统计文件暂时无法读取或写入。"""


@dataclass(frozen=True)
class WebRequestStatistic:
    """一次已接受 Web 请求的唯一终态统计。"""

    outcome: Literal["completed", "failed", "cancelled"]
    provider: str
    model: str
    elapsed_ms: float
    token_usage: TokenUsage | None = None


@dataclass
class _Metrics:
    requests: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    usage_known: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    completed_elapsed_ms: float = 0.0

    def record(self, event: WebRequestStatistic) -> None:
        self.requests += 1
        setattr(self, event.outcome, getattr(self, event.outcome) + 1)
        if event.outcome == "completed":
            self.completed_elapsed_ms += event.elapsed_ms
        if event.token_usage is not None:
            self.usage_known += 1
            self.input_tokens += event.token_usage.input_tokens
            self.output_tokens += event.token_usage.output_tokens
            self.total_tokens += event.token_usage.total_tokens

    def merge(self, other: "_Metrics") -> None:
        for name in (
            "requests",
            "completed",
            "failed",
            "cancelled",
            "usage_known",
            "input_tokens",
            "output_tokens",
            "total_tokens",
        ):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.completed_elapsed_ms += other.completed_elapsed_ms

    def to_storage(self) -> dict[str, int | float]:
        return {
            "requests": self.requests,
            "completed": self.completed,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "usage_known": self.usage_known,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "completed_elapsed_ms": round(self.completed_elapsed_ms, 3),
        }

    def to_payload(self) -> dict[str, int | float]:
        success_rate = self.completed * 100 / self.requests if self.requests else 0.0
        coverage = self.usage_known * 100 / self.requests if self.requests else 0.0
        average = (
            self.completed_elapsed_ms / self.completed if self.completed else 0.0
        )
        return {
            "requests": self.requests,
            "completed": self.completed,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "success_rate": round(success_rate, 1),
            "usage_coverage": round(coverage, 1),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "average_elapsed_ms": round(average, 2),
        }


@dataclass
class _ModelMetrics:
    provider: str
    model: str
    last_used_on: str
    metrics: _Metrics = field(default_factory=_Metrics)


@dataclass
class _StatisticsState:
    updated_at: str
    totals: _Metrics = field(default_factory=_Metrics)
    days: dict[str, _Metrics] = field(default_factory=dict)
    models: list[_ModelMetrics] = field(default_factory=list)


class WebStatisticsStore:
    """维护 Web 统计快照；损坏数据隔离后从空状态继续。"""

    def __init__(
        self,
        path: Path,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self._now = now or (lambda: datetime.now().astimezone())
        self._lock = threading.RLock()
        self._state = self._load_or_empty()

    def record(self, event: WebRequestStatistic) -> None:
        """校验并原子记录一次终态，不接受请求正文或会话标识。"""

        normalized = _validate_event(event)
        with self._lock:
            instant = self._current_time()
            day = instant.date().isoformat()
            self._state.totals.record(normalized)
            self._state.days.setdefault(day, _Metrics()).record(normalized)
            self._model_bucket(
                normalized.provider,
                normalized.model,
                day,
            ).metrics.record(normalized)
            self._state.updated_at = instant.isoformat()
            self._prune_days(instant.date())
            self._save()

    def snapshot(self) -> dict[str, object]:
        """返回页面所需的 30 日连续序列与低基数模型聚合。"""

        with self._lock:
            instant = self._current_time()
            today = instant.date()
            days: list[dict[str, object]] = []
            for offset in range(RETENTION_DAYS - 1, -1, -1):
                day = (today - timedelta(days=offset)).isoformat()
                metrics = self._state.days.get(day, _Metrics())
                days.append({"date": day, **metrics.to_payload()})
            models = sorted(
                self._state.models,
                key=lambda item: (
                    -item.metrics.requests,
                    -date.fromisoformat(item.last_used_on).toordinal(),
                    item.provider,
                    item.model,
                ),
            )
            return {
                "generated_at": instant.isoformat(),
                "period_days": RETENTION_DAYS,
                "totals": self._state.totals.to_payload(),
                "days": days,
                "models": [
                    {
                        "provider": item.provider,
                        "model": item.model,
                        "last_used_on": item.last_used_on,
                        **item.metrics.to_payload(),
                    }
                    for item in models
                ],
            }

    def _load_or_empty(self) -> _StatisticsState:
        if not self.path.exists():
            return self._empty_state()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return _decode_state(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
            self._quarantine(exc)
            return self._empty_state()

    def _empty_state(self) -> _StatisticsState:
        return _StatisticsState(updated_at=self._current_time().isoformat())

    def _current_time(self) -> datetime:
        instant = self._now()
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("statistics clock must be timezone-aware")
        return instant

    def _model_bucket(self, provider: str, model: str, day: str) -> _ModelMetrics:
        for item in self._state.models:
            if item.provider == provider and item.model == model:
                item.last_used_on = day
                return item

        if len(self._state.models) >= MAX_MODEL_BUCKETS:
            self._make_room_for_model(day)
        item = _ModelMetrics(provider, model, day)
        self._state.models.append(item)
        return item

    def _make_room_for_model(self, day: str) -> None:
        other = next(
            (
                item
                for item in self._state.models
                if item.provider == OTHER_PROVIDER and item.model == OTHER_MODEL
            ),
            None,
        )
        removable = sorted(
            (
                item
                for item in self._state.models
                if not (
                    item.provider == OTHER_PROVIDER and item.model == OTHER_MODEL
                )
            ),
            key=lambda item: (item.last_used_on, item.provider, item.model),
        )
        remove_count = 1 if other is not None else 2
        evicted = removable[:remove_count]
        if not evicted:
            return
        for item in evicted:
            self._state.models.remove(item)
        if other is None:
            other = _ModelMetrics(OTHER_PROVIDER, OTHER_MODEL, day)
            self._state.models.append(other)
        for item in evicted:
            other.metrics.merge(item.metrics)
            other.last_used_on = max(other.last_used_on, item.last_used_on)

    def _prune_days(self, today: date) -> None:
        oldest = today - timedelta(days=RETENTION_DAYS - 1)
        self._state.days = {
            day: metrics
            for day, metrics in self._state.days.items()
            if oldest <= date.fromisoformat(day) <= today
        }

    def _save(self) -> None:
        payload = {
            "version": STATISTICS_VERSION,
            "updated_at": self._state.updated_at,
            "totals": self._state.totals.to_storage(),
            "days": {
                day: metrics.to_storage()
                for day, metrics in sorted(self._state.days.items())
            },
            "models": [
                {
                    "provider": item.provider,
                    "model": item.model,
                    "last_used_on": item.last_used_on,
                    "metrics": item.metrics.to_storage(),
                }
                for item in self._state.models
            ],
        }
        temporary_path: Path | None = None
        descriptor: int | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.path.parent, 0o700)
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            os.chmod(temporary_path, 0o600)
            handle = os.fdopen(descriptor, "w", encoding="utf-8")
            descriptor = None
            with handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
            os.chmod(self.path, 0o600)
            _fsync_directory(self.path.parent)
        except OSError as exc:
            raise WebStatisticsStoreError("Unable to save web statistics") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _quarantine(self, exc: Exception) -> None:
        suffix = self._current_time().strftime("%Y%m%dT%H%M%S%f")
        quarantine = self.path.with_name(
            f"{self.path.stem}.corrupt-{suffix}{self.path.suffix}"
        )
        try:
            os.replace(self.path, quarantine)
        except OSError:
            pass
        log_web_statistics_error(
            request_id="statistics-file",
            operation="load",
            error_type=type(exc).__name__,
        )


def _validate_event(event: WebRequestStatistic) -> WebRequestStatistic:
    if event.outcome not in {"completed", "failed", "cancelled"}:
        raise ValueError("statistics outcome is invalid")
    provider = _validate_label(event.provider, "provider")
    model = _validate_label(event.model, "model")
    if (
        not isinstance(event.elapsed_ms, (int, float))
        or isinstance(event.elapsed_ms, bool)
        or not math.isfinite(event.elapsed_ms)
        or event.elapsed_ms < 0
    ):
        raise ValueError("statistics elapsed time is invalid")
    if event.token_usage is not None:
        tokens = (
            event.token_usage.input_tokens,
            event.token_usage.output_tokens,
            event.token_usage.total_tokens,
        )
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in tokens
        ):
            raise ValueError("statistics token usage is invalid")
    return WebRequestStatistic(
        event.outcome,
        provider,
        model,
        float(event.elapsed_ms),
        event.token_usage,
    )


def _validate_label(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"statistics {field_name} is invalid")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > MAX_LABEL_LENGTH
        or any(ord(character) < 32 for character in normalized)
    ):
        raise ValueError(f"statistics {field_name} is invalid")
    return normalized


def _decode_state(payload: object) -> _StatisticsState:
    if not isinstance(payload, Mapping) or payload.get("version") != STATISTICS_VERSION:
        raise ValueError("statistics version is invalid")
    updated_at = _decode_timestamp(payload.get("updated_at"))
    totals = _decode_metrics(payload.get("totals"))
    days_payload = payload.get("days")
    models_payload = payload.get("models")
    if not isinstance(days_payload, Mapping) or not isinstance(models_payload, list):
        raise ValueError("statistics collections are invalid")
    if len(days_payload) > RETENTION_DAYS:
        raise ValueError("statistics day limit is invalid")
    days: dict[str, _Metrics] = {}
    for day, metrics in days_payload.items():
        if not isinstance(day, str):
            raise ValueError("statistics day is invalid")
        date.fromisoformat(day)
        days[day] = _decode_metrics(metrics)
    if len(models_payload) > MAX_MODEL_BUCKETS:
        raise ValueError("statistics model limit is invalid")
    models: list[_ModelMetrics] = []
    seen: set[tuple[str, str]] = set()
    for raw_model in models_payload:
        if not isinstance(raw_model, Mapping):
            raise ValueError("statistics model is invalid")
        provider = _validate_label(raw_model.get("provider"), "provider")
        model = _validate_label(raw_model.get("model"), "model")
        key = (provider, model)
        if key in seen:
            raise ValueError("statistics model is duplicated")
        seen.add(key)
        last_used_on = raw_model.get("last_used_on")
        if not isinstance(last_used_on, str):
            raise ValueError("statistics model date is invalid")
        date.fromisoformat(last_used_on)
        models.append(
            _ModelMetrics(
                provider,
                model,
                last_used_on,
                _decode_metrics(raw_model.get("metrics")),
            )
        )
    return _StatisticsState(updated_at, totals, days, models)


def _decode_metrics(payload: object) -> _Metrics:
    if not isinstance(payload, Mapping):
        raise ValueError("statistics metrics are invalid")
    integer_names = (
        "requests",
        "completed",
        "failed",
        "cancelled",
        "usage_known",
        "input_tokens",
        "output_tokens",
        "total_tokens",
    )
    values: dict[str, int] = {}
    for name in integer_names:
        value = payload.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("statistics counter is invalid")
        values[name] = value
    elapsed = payload.get("completed_elapsed_ms")
    if (
        not isinstance(elapsed, (int, float))
        or isinstance(elapsed, bool)
        or not math.isfinite(elapsed)
        or elapsed < 0
    ):
        raise ValueError("statistics duration is invalid")
    if values["requests"] != (
        values["completed"] + values["failed"] + values["cancelled"]
    ):
        raise ValueError("statistics outcome counters are inconsistent")
    if values["usage_known"] > values["requests"]:
        raise ValueError("statistics usage counter is inconsistent")
    return _Metrics(**values, completed_elapsed_ms=float(elapsed))


def _decode_timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("statistics timestamp is invalid")
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("statistics timestamp timezone is invalid")
    return value


def _fsync_directory(path: Path) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY)
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)
