"""端到端联调测试：模拟“创建任务 -> worker 认领执行 -> API 上报幂等 -> 看板查询”完整链路。

这个测试不启动真实 uvicorn 进程，而是通过 FastAPI TestClient 作为 HTTP 入口，
并调用 worker.run_worker_once() 作为 worker 执行入口，覆盖真实系统的主要串联路径。

需要本地 MySQL taskkanban 库可用；测试会创建真实任务并清理。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app import repository
from app.api import app
from app.worker import run_worker_once

client = TestClient(app)


@pytest.fixture()
def e2e_task():
    """通过 repository 创建端到端任务，返回 task_id，测试结束清理。"""
    tid = repository.create_task(
        base_params={
            "template_id": "T001",
            "channel": "email",
            "send_at": "2026-09-05 10:00:00",
        },
        group_override={
            "channel": "sms",
            "priority": "high",
        },
        steps=[
            {
                "step_index": 1,
                "override": {"template_id": "T002", "send_at": ""},
                "action": "check_subscription",
            },
            {
                "step_index": 2,
                "override": {"action": "send_message"},
                "action": "send_message",
            },
            {
                "step_index": 3,
                "override": {"action": "record_receipt", "customer_name": "Alice"},
                "action": "record_receipt",
            },
        ],
    )
    yield tid
    repository.delete_task_by_id(tid)


def _api_task_detail(tid: int) -> dict:
    resp = client.get(f"/tasks/{tid}")
    assert resp.status_code == 200
    return resp.json()


def test_e2e_worker_runs_pending_task_to_done(e2e_task):
    """一个真实 worker 执行入口跑完一个 pending 任务。"""
    tid = e2e_task

    # 初始状态 pending
    assert _api_task_detail(tid)["status"] == "pending"

    # worker 认领并执行一个任务；run_worker_once 返回 True 表示处理了一个任务
    assert run_worker_once("e2e-worker") is True

    # 任务应进入终态 done
    task = _api_task_detail(tid)
    assert task["status"] == "done"

    # 每个 Step 都应为 done
    assert [s["status"] for s in task["steps"]] == ["done", "done", "done"]

    # 每个 Step 都只有一条日志
    logs = repository.list_step_logs(tid)
    assert len(logs) == 3
    assert {log["step_index"] for log in logs} == {1, 2, 3}
    assert all(log["status"] == "success" for log in logs)


def test_e2e_params_are_sticky_across_worker_execution(e2e_task):
    """worker 执行后，日志里应能看到参数粘性演变的证据。"""
    tid = e2e_task

    assert run_worker_once("e2e-worker") is True

    logs = repository.list_step_logs(tid)
    messages = {log["step_index"]: log["message"] for log in logs}

    # Step1 override: template_id=T002, send_at="" (空串跳过)
    step1_params = _extract_params_from_log(messages[1])
    assert step1_params["template_id"] == "T002"          # L3 覆盖 L1
    assert step1_params["channel"] == "sms"                # L2 覆盖 L1
    assert step1_params["send_at"] == "2026-09-05 10:00:00"  # 空串保持当前值
    assert step1_params["priority"] == "high"              # L2 引入新 key

    # Step3 override: 引入 customer_name；模板应保持 Step1 的 T002（粘性）
    step3_params = _extract_params_from_log(messages[3])
    assert step3_params["template_id"] == "T002"
    assert step3_params["customer_name"] == "Alice"
    assert step3_params["channel"] == "sms"


def test_e2e_api_claim_and_report_flow(e2e_task):
    """通过 API 手动认领 + 上报完成一个 2 Step 任务（task 本身 3 步，这里只测前两步足够覆盖）。"""
    tid = e2e_task

    # 手动认领
    claim_resp = client.post(f"/tasks/{tid}/claim")
    assert claim_resp.status_code == 200
    assert claim_resp.json()["claimed"] is True

    # 置为 running（真实 worker 会做；测试模拟执行权交接）
    assert repository.mark_task_running(tid, "manual-claim") is True

    # 上报 Step1 成功
    r1 = client.post(
        f"/tasks/{tid}/steps/1/report",
        json={"status": "success", "message": "e2e step1", "worker_id": "manual-claim"},
    )
    assert r1.status_code == 200
    assert r1.json()["task_status"] == "running"

    # 并发 5 次上报 Step2（演示幂等）
    responses = []
    for i in range(5):
        resp = client.post(
            f"/tasks/{tid}/steps/2/report",
            json={
                "status": "success",
                "message": f"e2e concurrent {i}",
                "worker_id": "manual-claim",
            },
        )
        assert resp.status_code == 200
        responses.append(resp.json())

    # 只有第一次 inserted=True，其余为 False
    inserted_count = sum(1 for r in responses if r["inserted"] is True)
    assert inserted_count == 1
    # 因为任务有 3 个 Step，第 2 个成功后任务仍未 done
    assert responses[0]["task_status"] == "running"

    logs = repository.list_step_logs(tid)
    step2_logs = [log for log in logs if log["step_index"] == 2]
    assert len(step2_logs) == 1

    # 补报 Step3 后任务 done
    r3 = client.post(
        f"/tasks/{tid}/steps/3/report",
        json={"status": "success", "message": "e2e step3", "worker_id": "manual-claim"},
    )
    assert r3.status_code == 200
    assert r3.json()["task_status"] == "done"

    assert _api_task_detail(tid)["status"] == "done"


def test_e2e_dashboard_task_list_reflects_status(e2e_task):
    """看板数据源 GET /tasks 能看到任务状态流转。"""
    tid = e2e_task

    # pending 时可见
    resp = client.get("/tasks")
    assert resp.status_code == 200
    task_row = next(t for t in resp.json()["tasks"] if t["id"] == tid)
    assert task_row["status"] == "pending"

    # worker 执行后应变为 done
    assert run_worker_once("e2e-worker") is True

    resp = client.get("/tasks")
    assert resp.status_code == 200
    task_row = next(t for t in resp.json()["tasks"] if t["id"] == tid)
    assert task_row["status"] == "done"


def test_e2e_report_after_done_is_rejected(e2e_task):
    """任务已完成后再上报应被 API 拒绝，避免破坏终态。"""
    tid = e2e_task
    assert run_worker_once("e2e-worker") is True
    assert _api_task_detail(tid)["status"] == "done"

    resp = client.post(
        f"/tasks/{tid}/steps/1/report",
        json={"status": "success", "message": "late", "worker_id": "e2e-worker"},
    )
    assert resp.status_code == 409


def _extract_params_from_log(message: str) -> dict:
    """从 executor 日志消息 [action] params={...} 中提取参数字典。

    用于在端到端测试中断言 worker 实际执行时看到的参数。
    """
    marker = "params="
    start = message.index(marker) + len(marker)
    return eval(message[start:])
