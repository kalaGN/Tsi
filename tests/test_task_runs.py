"""长任务私有状态与安全恢复。"""

import os
import logging

import pytest

from app.runtime.task_runs import TaskRunConflict, TaskRunError, TaskRunStore
from app.observability.model_logging import LOGGER_NAME


def test_task_state_round_trip_and_restart_recovery(tmp_path):
    store = TaskRunStore(tmp_path / "runs")
    task = store.create("session-1", "project-1", "修改项目并验证", [{"kind": "project_check", "target": "compile", "expected_sha256": None}])
    assert task.state == "ready"
    running = store.transition(task.id, 0, "running")
    assert running.attempts == 1
    recovered = TaskRunStore(tmp_path / "runs").recover_interrupted()
    assert [item.state for item in recovered] == ["needs_review"]
    assert TaskRunStore(tmp_path / "runs").recover_interrupted() == ()
    assert "修改项目" not in str(recovered[0].public_payload())
    assert (tmp_path / "runs").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "runs" / f"{task.id}.json").stat().st_mode & 0o777 == 0o600


def test_task_conflict_invalid_transition_and_symlink_rejected(tmp_path):
    store = TaskRunStore(tmp_path / "runs")
    task = store.create("session-1", "project-1", "检查文件", [])
    with pytest.raises(TaskRunConflict):
        store.transition(task.id, 1, "running")
    with pytest.raises(TaskRunError):
        store.transition(task.id, 0, "completed")
    path = store.root / f"{task.id}.json"
    path.unlink()
    path.symlink_to(tmp_path / "outside.json")
    with pytest.raises(TaskRunError):
        store.load(task.id)


def test_task_corruption_and_loose_permissions_do_not_reset(tmp_path):
    store = TaskRunStore(tmp_path / "runs")
    task = store.create("session-1", "project-1", "检查文件", [])
    path = store.root / f"{task.id}.json"
    os.chmod(path, 0o644)
    with pytest.raises(TaskRunError):
        store.load(task.id)
    os.chmod(path, 0o600)
    path.write_text('{"version":1,"version":1}', encoding="utf-8")
    with pytest.raises(TaskRunError):
        store.recover_interrupted()


def test_task_rejects_unencodable_goal_without_creating_file(tmp_path):
    store = TaskRunStore(tmp_path / "runs")
    with pytest.raises(TaskRunError):
        store.create("session-1", "project-1", "bad\ud800", [])
    assert store.list() == ()


def test_task_state_log_does_not_copy_goal_or_condition(tmp_path):
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger(LOGGER_NAME)
    previous_level = logger.level
    handler = Capture()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        TaskRunStore(tmp_path / "runs").create("session-1", "project-1", "敏感目标", [
            {"kind": "file_exists", "target": "private.txt", "expected_sha256": None},
        ])
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
    task_logs = [record for record in records if getattr(record, "event", None) == "task_state_change"]
    assert len(task_logs) == 1
    assert "敏感目标" not in str(task_logs[0].__dict__)
    assert "private.txt" not in str(task_logs[0].__dict__)
