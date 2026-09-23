"""Web 长任务从本机 API 穿过真实会话流和验收器。"""

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.runtime.task_runs import TaskRunStore
from app.webui.router import create_webui_router
from test_webui import create_service


def test_web_task_requires_evidence_and_survives_restart(tmp_path):
    (tmp_path / "answer.txt").write_text("已存在", encoding="utf-8")
    service = create_service(tmp_path)
    service.task_store = TaskRunStore(tmp_path / "task-runs")
    app = FastAPI()
    app.include_router(create_webui_router(service))
    client = TestClient(app)

    created = client.post("/ui/api/tasks", json={
        "goal": "确认 answer.txt", "conditions": [
            {"kind": "file_exists", "target": "answer.txt", "expected_sha256": None},
        ],
    }, headers={"Origin": "http://testserver"})
    assert created.status_code == 200
    task_id = created.json()["id"]
    response = client.post(f"/ui/api/tasks/{task_id}/run", json={}, headers={"Origin": "http://testserver"})
    events = [json.loads(line) for line in response.text.splitlines()]
    assert response.status_code == 200
    assert events[-3]["result"]["status"] == "passed"
    assert events[-2]["task"]["state"] == "completed"
    assert "确认 answer.txt" not in client.get(f"/ui/api/tasks/{task_id}").text
    assert TaskRunStore(tmp_path / "task-runs").load(task_id).state == "completed"
    assert client.post(f"/ui/api/tasks/{task_id}/run", json={}).status_code == 409


def test_web_task_rejects_cross_origin_and_invalid_conditions(tmp_path):
    service = create_service(tmp_path)
    service.task_store = TaskRunStore(tmp_path / "task-runs")
    app = FastAPI()
    app.include_router(create_webui_router(service))
    client = TestClient(app)
    payload = {"goal": "检查", "conditions": []}
    assert client.post("/ui/api/tasks", json=payload, headers={"Origin": "https://other.example"}).status_code == 403
    assert client.post("/ui/api/tasks", json={
        "goal": "检查", "conditions": [{"kind": "file_exists", "target": "../outside", "expected_sha256": None}],
    }).status_code == 422


def test_web_task_controls_are_served_with_chat_page(tmp_path):
    service = create_service(tmp_path)
    service.task_store = TaskRunStore(tmp_path / "task-runs")
    app = FastAPI()
    app.include_router(create_webui_router(service))
    page = TestClient(app).get("/ui")
    assert page.status_code == 200
    assert 'id="task-form"' in page.text
    assert 'id="task-resume"' in page.text
