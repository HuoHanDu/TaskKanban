"""Repository 层新增边界校验测试。

覆盖：
1. complete_task_atomically 必须提交任务全部 Step，不允许部分结果提前 done；
2. report_step_execution 必须按 step_index 顺序上报，不允许跳过前置 Step；
3. create_task 入口校验：空任务、重复/非法 step_index、override/action 非法。
"""

from __future__ import annotations

import pytest

from app import repository


@pytest.fixture()
def task_id():
    tid = repository.create_task(
        base_params={},
        group_override={},
        steps=[
            {"step_index": 1, "override": {}, "action": "mock"},
            {"step_index": 2, "override": {}, "action": "mock"},
        ],
    )
    yield tid
    repository.delete_task_by_id(tid)


def _claim_and_run(tid: int, worker: str = "worker-a") -> None:
    assert repository.claim_by_id(tid, worker) is True
    assert repository.mark_task_running(tid, worker) is True


# ---------------------------------------------------------------
# complete_task_atomically 全量 Step 覆盖
# ---------------------------------------------------------------

def test_complete_task_atomically_rejects_partial_results(task_id):
    _claim_and_run(task_id)

    with pytest.raises(RuntimeError, match="must cover every step"):
        repository.complete_task_atomically(
            task_id,
            "worker-a",
            results=[{"step_index": 1, "success": True, "message": "ok"}],
        )

    task = repository.get_task_with_steps(task_id)
    assert task["status"] == "running"
    assert [s["status"] for s in task["steps"]] == ["pending", "pending"]


def test_complete_task_atomically_allows_full_results(task_id):
    _claim_and_run(task_id)

    out = repository.complete_task_atomically(
        task_id,
        "worker-a",
        results=[
            {"step_index": 1, "success": True, "message": "ok1"},
            {"step_index": 2, "success": True, "message": "ok2"},
        ],
    )
    assert out["task_status"] == "done"

    task = repository.get_task_with_steps(task_id)
    assert task["status"] == "done"
    assert [s["status"] for s in task["steps"]] == ["done", "done"]


def test_complete_task_atomically_rejects_empty_results(task_id):
    _claim_and_run(task_id)

    with pytest.raises(ValueError, match="must not be empty"):
        repository.complete_task_atomically(
            task_id, "worker-a", results=[]
        )


# ---------------------------------------------------------------
# report_step_execution 顺序上报
# ---------------------------------------------------------------

def test_report_out_of_order_step_is_rejected(task_id):
    _claim_and_run(task_id)

    with pytest.raises(RuntimeError, match="cannot be reported before"):
        repository.report_step_execution(
            task_id, 2, "success", "worker-a", message="skip step1"
        )

    task = repository.get_task_with_steps(task_id)
    assert task["status"] == "running"
    assert [s["status"] for s in task["steps"]] == ["pending", "pending"]
    assert repository.list_step_logs(task_id) == []


def test_report_in_order_steps_succeeds(task_id):
    _claim_and_run(task_id)

    first = repository.report_step_execution(
        task_id, 1, "success", "worker-a", message="step1"
    )
    assert first["log_inserted"] is True
    assert first["task_status"] == "running"

    second = repository.report_step_execution(
        task_id, 2, "success", "worker-a", message="step2"
    )
    assert second["task_status"] == "done"


def test_duplicate_report_last_step_does_not_change_done_state(task_id):
    _claim_and_run(task_id)

    repository.report_step_execution(task_id, 1, "success", "worker-a")
    first_last = repository.report_step_execution(task_id, 2, "success", "worker-a")
    assert first_last["task_status"] == "done"

    # 任务已 done，API 会拒；直接调 repository 也应被状态机拒绝。
    with pytest.raises(RuntimeError, match="status"):
        repository.report_step_execution(task_id, 2, "success", "worker-a")


# ---------------------------------------------------------------
# create_task 入口校验
# ---------------------------------------------------------------

def test_create_task_rejects_empty_steps():
    with pytest.raises(ValueError, match="at least one step"):
        repository.create_task({}, {}, [])


def test_create_task_rejects_duplicate_step_index():
    with pytest.raises(ValueError, match="duplicate step_index"):
        repository.create_task(
            {},
            {},
            [
                {"step_index": 1, "override": {}, "action": "mock"},
                {"step_index": 1, "override": {}, "action": "mock"},
            ],
        )


def test_create_task_rejects_non_mapping_steps():
    with pytest.raises(TypeError, match="steps"):
        repository.create_task({}, {}, [1, 2])


def test_create_task_rejects_bad_override():
    with pytest.raises(TypeError, match="override"):
        repository.create_task(
            {},
            {},
            [{"step_index": 1, "override": "bad", "action": "mock"}],
        )


def test_create_task_rejects_empty_action():
    with pytest.raises(ValueError, match="action"):
        repository.create_task(
            {},
            {},
            [{"step_index": 1, "override": {}, "action": "   "}],
        )
