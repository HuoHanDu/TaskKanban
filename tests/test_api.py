"""API 层集成测试：使用 FastAPI TestClient 覆盖真实 HTTP 路径。

需要本地 MySQL taskkanban 库可用；测试会创建真实任务并清理。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import repository
from app.api import app

client = TestClient(app)

MANUAL_WORKER = "manual-claim"
REPORT_WORKER = "api-report"


@pytest.fixture()
def task_id():
    tid = repository.create_task(
        base_params={"template_id": "T001", "channel": "email"},
        group_override={"channel": "sms", "priority": "high"},
        steps=[
            {"step_index": 1, "override": {}, "action": "mock"},
            {"step_index": 2, "override": {}, "action": "mock"},
        ],
    )
    yield tid
    repository.delete_task_by_id(tid)


def _get_task(tid: int) -> dict:
    resp = client.get(f"/tasks/{tid}")
    assert resp.status_code == 200
    return resp.json()


# ---------------------------------------------------------------
# 基础查询
# ---------------------------------------------------------------

def test_list_tasks_contains_created_task(task_id):
    resp = client.get("/tasks")
    assert resp.status_code == 200
    ids = [t["id"] for t in resp.json()["tasks"]]
    assert task_id in ids


def test_get_task_detail_returns_steps(task_id):
    data = _get_task(task_id)
    assert data["id"] == task_id
    assert data["status"] == "pending"
    assert data["base_params"]["template_id"] == "T001"
    assert [s["step_index"] for s in data["steps"]] == [1, 2]


def test_get_missing_task_returns_404():
    resp = client.get("/tasks/999999999")
    assert resp.status_code == 404


# ---------------------------------------------------------------
# 手动认领 API
# ---------------------------------------------------------------

def test_claim_pending_task_succeeds(task_id):
    resp = client.post(f"/tasks/{task_id}/claim")
    assert resp.status_code == 200
    body = resp.json()
    assert body["claimed"] is True
    assert body["status"] == "claimed"

    data = _get_task(task_id)
    assert data["status"] == "claimed"
    assert data["claimed_by"] == MANUAL_WORKER


def test_claim_already_claimed_task_returns_409(task_id):
    assert client.post(f"/tasks/{task_id}/claim").status_code == 200
    resp = client.post(f"/tasks/{task_id}/claim")
    assert resp.status_code == 409


def test_claim_missing_task_returns_404():
    resp = client.post("/tasks/999999999/claim")
    assert resp.status_code == 404


# ---------------------------------------------------------------
# 上报 API：状态与归属校验
# ---------------------------------------------------------------

def _claim_and_run(tid: int, worker: str = MANUAL_WORKER):
    assert client.post(f"/tasks/{tid}/claim").status_code == 200
    assert repository.mark_task_running(tid, worker) is True


def test_report_on_pending_task_returns_409(task_id):
    resp = client.post(
        f"/tasks/{task_id}/steps/1/report",
        json={"status": "success", "worker_id": REPORT_WORKER},
    )
    assert resp.status_code == 409


def test_report_by_non_owner_returns_403(task_id):
    _claim_and_run(task_id, MANUAL_WORKER)

    resp = client.post(
        f"/tasks/{task_id}/steps/1/report",
        json={"status": "success", "worker_id": "other-worker"},
    )
    assert resp.status_code == 403


def test_report_missing_step_returns_409(task_id):
    _claim_and_run(task_id, MANUAL_WORKER)

    resp = client.post(
        f"/tasks/{task_id}/steps/99/report",
        json={"status": "success", "worker_id": MANUAL_WORKER},
    )
    assert resp.status_code == 409


def test_report_success_single_step_keeps_task_running(task_id):
    # 用 manual-claim 持有任务，再用同一 worker 上报，验证原子路径
    _claim_and_run(task_id, MANUAL_WORKER)

    resp = client.post(
        f"/tasks/{task_id}/steps/1/report",
        json={"status": "success", "message": "step1 ok", "worker_id": MANUAL_WORKER},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["inserted"] is True
    assert body["step_status"] == "done"
    assert body["task_status"] == "running"
    assert body["task_finished"] is False

    task = repository.get_task_with_steps(task_id)
    assert task["steps"][0]["status"] == "done"


def test_report_last_success_marks_task_done(task_id):
    _claim_and_run(task_id, MANUAL_WORKER)
    client.post(
        f"/tasks/{task_id}/steps/1/report",
        json={"status": "success", "worker_id": MANUAL_WORKER},
    )

    resp = client.post(
        f"/tasks/{task_id}/steps/2/report",
        json={"status": "success", "worker_id": MANUAL_WORKER},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["task_status"] == "done"
    assert body["task_finished"] is True

    data = _get_task(task_id)
    assert data["status"] == "done"
    assert [s["status"] for s in data["steps"]] == ["done", "done"]


def test_report_failure_marks_task_failed(task_id):
    _claim_and_run(task_id, MANUAL_WORKER)

    resp = client.post(
        f"/tasks/{task_id}/steps/1/report",
        json={"status": "failure", "message": "boom", "worker_id": MANUAL_WORKER},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["task_status"] == "failed"
    assert body["task_finished"] is True

    data = _get_task(task_id)
    assert data["status"] == "failed"


def test_duplicate_success_is_idempotent(task_id):
    _claim_and_run(task_id, MANUAL_WORKER)

    first = client.post(
        f"/tasks/{task_id}/steps/1/report",
        json={"status": "success", "message": "first", "worker_id": MANUAL_WORKER},
    )
    second = client.post(
        f"/tasks/{task_id}/steps/1/report",
        json={"status": "success", "message": "second", "worker_id": MANUAL_WORKER},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["inserted"] is True
    assert second.json()["inserted"] is False
    assert second.json()["message"] == "duplicate ignored (idempotent)"

    logs = repository.list_step_logs(task_id)
    assert len(logs) == 1
    assert logs[0]["message"] == "first"


def test_late_failure_does_not_overwrite_success(task_id):
    _claim_and_run(task_id, MANUAL_WORKER)

    first = client.post(
        f"/tasks/{task_id}/steps/1/report",
        json={"status": "success", "message": "first", "worker_id": MANUAL_WORKER},
    )
    late = client.post(
        f"/tasks/{task_id}/steps/1/report",
        json={"status": "failure", "message": "late", "worker_id": MANUAL_WORKER},
    )

    assert first.status_code == 200
    assert late.status_code == 200
    body = late.json()
    assert body["ignored_duplicate"] is True
    assert body["step_status"] == "done"
    assert body["task_status"] == "running"
    assert "existing success preserved" in body["message"]

    data = _get_task(task_id)
    assert data["status"] == "running"
    assert data["steps"][0]["status"] == "done"

    logs = repository.list_step_logs(task_id)
    assert len(logs) == 1
    assert logs[0]["status"] == "success"
    assert logs[0]["message"] == "first"


def test_report_missing_task_returns_404():
    resp = client.post(
        "/tasks/999999999/steps/1/report",
        json={"status": "success", "worker_id": REPORT_WORKER},
    )
    assert resp.status_code == 404
