"""GET /tasks/{id}/logs 接口测试。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import repository
from app.api import app

client = TestClient(app)


@pytest.fixture()
def task_id():
    tid = repository.create_task(
        base_params={},
        group_override={},
        steps=[{"step_index": 1, "override": {}, "action": "mock"}],
    )
    yield tid
    repository.delete_task_by_id(tid)


def test_logs_empty_before_execution(task_id):
    resp = client.get(f"/tasks/{task_id}/logs")
    assert resp.status_code == 200
    assert resp.json()["logs"] == []


def test_logs_returns_log_after_report(task_id):
    repository.claim_by_id(task_id, "manual-claim")
    repository.mark_task_running(task_id, "manual-claim")
    client.post(
        f"/tasks/{task_id}/steps/1/report",
        json={"status": "success", "message": "show params", "worker_id": "manual-claim"},
    )

    resp = client.get(f"/tasks/{task_id}/logs")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["logs"]) == 1
    assert body["logs"][0]["step_index"] == 1
    assert body["logs"][0]["message"] == "show params"


def test_logs_missing_task_returns_404():
    resp = client.get("/tasks/999999999/logs")
    assert resp.status_code == 404
