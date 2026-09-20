import json
from datetime import datetime, timedelta, timezone

from app.services.llm.contracts import TokenUsage
from app.webui.statistics import (
    MAX_MODEL_BUCKETS,
    WebRequestStatistic,
    WebStatisticsStore,
)


SHANGHAI = timezone(timedelta(hours=8))


class Clock:
    def __init__(self, current: datetime):
        self.current = current

    def __call__(self) -> datetime:
        return self.current


def test_statistics_aggregate_outcomes_usage_and_elapsed_time(tmp_path):
    clock = Clock(datetime(2026, 9, 20, 10, 30, tzinfo=SHANGHAI))
    store = WebStatisticsStore(tmp_path / "web-statistics.json", now=clock)

    store.record(
        WebRequestStatistic(
            "completed",
            "deepseek",
            "deepseek-v4-flash",
            2_000,
            TokenUsage(10, 4, 14),
        )
    )
    store.record(
        WebRequestStatistic(
            "completed",
            "deepseek",
            "deepseek-v4-flash",
            4_000,
        )
    )
    store.record(
        WebRequestStatistic(
            "failed",
            "aliyun",
            "qwen3-max",
            500,
        )
    )
    store.record(
        WebRequestStatistic(
            "cancelled",
            "aliyun",
            "qwen3-max",
            250,
        )
    )

    payload = store.snapshot()

    assert payload["period_days"] == 30
    assert payload["totals"] == {
        "requests": 4,
        "completed": 2,
        "failed": 1,
        "cancelled": 1,
        "success_rate": 50.0,
        "usage_coverage": 25.0,
        "input_tokens": 10,
        "output_tokens": 4,
        "total_tokens": 14,
        "average_elapsed_ms": 3_000.0,
    }
    assert len(payload["days"]) == 30
    assert payload["days"][-1] == {
        "date": "2026-09-20",
        **payload["totals"],
    }
    assert {
        (item["provider"], item["model"]) for item in payload["models"]
    } == {
        ("deepseek", "deepseek-v4-flash"),
        ("aliyun", "qwen3-max"),
    }


def test_statistics_persist_and_prune_days_to_latest_thirty(tmp_path):
    path = tmp_path / "web-statistics.json"
    clock = Clock(datetime(2026, 8, 1, 12, tzinfo=SHANGHAI))
    store = WebStatisticsStore(path, now=clock)
    store.record(WebRequestStatistic("completed", "deepseek", "m1", 100))

    clock.current = datetime(2026, 9, 20, 12, tzinfo=SHANGHAI)
    store.record(WebRequestStatistic("completed", "deepseek", "m1", 300))

    restored = WebStatisticsStore(path, now=clock).snapshot()
    persisted = json.loads(path.read_text(encoding="utf-8"))

    assert restored["totals"]["requests"] == 2
    assert restored["days"][0]["date"] == "2026-08-22"
    assert restored["days"][-1]["date"] == "2026-09-20"
    assert "2026-08-01" not in persisted["days"]
    assert path.stat().st_mode & 0o777 == 0o600


def test_statistics_corruption_is_quarantined_and_recovers_empty(tmp_path):
    path = tmp_path / "web-statistics.json"
    path.write_text('{"version":1,"totals":{"requests":-1}}', encoding="utf-8")
    clock = Clock(datetime(2026, 9, 20, 12, tzinfo=SHANGHAI))

    store = WebStatisticsStore(path, now=clock)

    assert store.snapshot()["totals"]["requests"] == 0
    assert not path.exists()
    assert len(list(tmp_path.glob("web-statistics.corrupt-*.json"))) == 1
    store.record(WebRequestStatistic("failed", "deepseek", "m1", 10))
    assert json.loads(path.read_text(encoding="utf-8"))["totals"]["failed"] == 1


def test_statistics_quarantine_invalid_model_label_type(tmp_path):
    path = tmp_path / "web-statistics.json"
    payload = {
        "version": 1,
        "updated_at": "2026-09-20T12:00:00+08:00",
        "totals": _empty_metrics(),
        "days": {},
        "models": [
            {
                "provider": 123,
                "model": "m1",
                "last_used_on": "2026-09-20",
                "metrics": _empty_metrics(),
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    clock = Clock(datetime(2026, 9, 20, 12, tzinfo=SHANGHAI))

    store = WebStatisticsStore(path, now=clock)

    assert store.snapshot()["models"] == []
    assert len(list(tmp_path.glob("web-statistics.corrupt-*.json"))) == 1


def test_statistics_bound_model_buckets_and_do_not_store_request_content(tmp_path):
    path = tmp_path / "web-statistics.json"
    clock = Clock(datetime(2026, 9, 20, 12, tzinfo=SHANGHAI))
    store = WebStatisticsStore(path, now=clock)

    for index in range(MAX_MODEL_BUCKETS + 5):
        store.record(
            WebRequestStatistic(
                "completed",
                "provider",
                f"model-{index}",
                index,
                TokenUsage(1, 1, 2),
            )
        )

    payload = store.snapshot()
    persisted_text = path.read_text(encoding="utf-8")

    assert len(payload["models"]) == MAX_MODEL_BUCKETS
    assert any(item["provider"] == "other" for item in payload["models"])
    assert payload["totals"]["requests"] == MAX_MODEL_BUCKETS + 5
    for forbidden in ("message", "content", "session_id", "request_id", "api_key"):
        assert forbidden not in persisted_text.lower()


def test_statistics_reject_invalid_event_without_changing_file(tmp_path):
    path = tmp_path / "web-statistics.json"
    clock = Clock(datetime(2026, 9, 20, 12, tzinfo=SHANGHAI))
    store = WebStatisticsStore(path, now=clock)

    try:
        store.record(WebRequestStatistic("completed", "deepseek", "m1", -1))
    except ValueError as exc:
        assert "elapsed" in str(exc)
    else:
        raise AssertionError("negative elapsed time must be rejected")

    assert not path.exists()


def _empty_metrics():
    return {
        "requests": 0,
        "completed": 0,
        "failed": 0,
        "cancelled": 0,
        "usage_known": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "completed_elapsed_ms": 0.0,
    }
