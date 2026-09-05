"""API /start 手动执行入口测试：claimed -> running 的看板演示路径。"""

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


def test_start_claimed_task_runs_successfully(task_id):
    claim = client.post(f"/tasks/{task_id}/claim")
    assert claim.status_code == 200

    resp = client.post(f"/tasks/{task_id}/start")
    assert resp.status_code == 200
    assert resp.json()["status"] == "running"

    task = repository.get_task_with_steps(task_id)
    assert task["status"] == "running"


def test_start_pending_task_returns_409(task_id):
    resp = client.post(f"/tasks/{task_id}/start")
    assert resp.status_code == 409


def test_start_non_manual_claimed_task_returns_403(task_id):
    repository.claim_by_id(task_id, "real-worker")
    resp = client.post(f"/tasks/{task_id}/start")
    assert resp.status_code == 403


def test_start_missing_task_returns_404():
    resp = client.post("/tasks/999999999/start")
    assert resp.status_code == 404
