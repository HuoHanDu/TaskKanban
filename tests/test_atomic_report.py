"""原子 Step 上报 / 整任务原子提交的集成测试。

验证目标：
1. report_step_execution：单个 Step 的日志+状态+任务终态在一个事务内完成；
2. 只有任务持有者且任务处于 claimed/running 时才能上报；
3. 重复上报不会覆盖已有日志，也不会重复推进状态；
4. 失败上报立即将任务置为 failed；
5. 最后一个成功 Step 上报后任务自动 done；
6. complete_task_atomically：一次提交全部 Step 结果，整体成功/回滚。
"""

from __future__ import annotations

import pytest

from app import repository


@pytest.fixture()
def task_id():
    tid = repository.create_task(
        base_params={"a": 1},
        group_override={},
        steps=[
            {"step_index": 1, "override": {}, "action": "mock"},
            {"step_index": 2, "override": {}, "action": "mock"},
        ],
    )
    yield tid
    repository.delete_task_by_id(tid)


def _claim(tid: int, worker: str = "worker-a"):
    assert repository.claim_by_id(tid, worker) is True


def _run(tid: int, worker: str = "worker-a"):
    assert repository.mark_task_running(tid, worker) is True


# ---------------------------------------------------------------
# report_step_execution：单 Step 原子上报
# ---------------------------------------------------------------

def test_report_success_step_keeps_task_running(task_id):
    _claim(task_id)
    _run(task_id)

    out = repository.report_step_execution(
        task_id, 1, "success", "worker-a", message="step1 ok"
    )
    assert out["log_inserted"] is True
    assert out["step_status"] == "done"
    assert out["task_status"] == "running"
    assert out["task_finished"] is False

    task = repository.get_task_with_steps(task_id)
    assert task["status"] == "running"
    assert task["steps"][0]["status"] == "done"
    assert task["steps"][1]["status"] == "pending"

    logs = repository.list_step_logs(task_id)
    assert len(logs) == 1
    assert logs[0]["status"] == "success"


def test_report_last_success_step_marks_task_done(task_id):
    _claim(task_id)
    _run(task_id)
    repository.report_step_execution(task_id, 1, "success", "worker-a")

    out = repository.report_step_execution(
        task_id, 2, "success", "worker-a", message="last"
    )
    assert out["task_finished"] is True
    assert out["task_status"] == "done"
    assert repository.get_task_with_steps(task_id)["status"] == "done"


def test_report_failure_marks_task_failed_and_stops(task_id):
    _claim(task_id)
    _run(task_id)
    repository.report_step_execution(task_id, 1, "success", "worker-a")

    out = repository.report_step_execution(
        task_id, 2, "failure", "worker-a", message="boom"
    )
    assert out["task_status"] == "failed"
    assert out["task_finished"] is True

    task = repository.get_task_with_steps(task_id)
    assert task["status"] == "failed"
    assert task["steps"][1]["status"] == "failed"


def test_duplicate_report_does_not_overwrite_existing_success(task_id):
    _claim(task_id)
    _run(task_id)

    first = repository.report_step_execution(
        task_id, 1, "success", "worker-a", message="first"
    )
    second = repository.report_step_execution(
        task_id, 1, "failure", "worker-a", message="should not overwrite"
    )

    assert first["log_inserted"] is True
    assert second["log_inserted"] is False
    assert second["ignored_duplicate"] is True
    assert second["step_status"] == "done"
    assert second["task_status"] == "running"

    logs = repository.list_step_logs(task_id)
    assert len(logs) == 1
    assert logs[0]["status"] == "success"
    assert logs[0]["message"] == "first"

    task = repository.get_task_with_steps(task_id)
    assert task["steps"][0]["status"] == "done"


def test_report_rejected_when_not_owner(task_id):
    _claim(task_id, "worker-a")
    _run(task_id)

    with pytest.raises(RuntimeError, match="claimed by"):
        repository.report_step_execution(task_id, 1, "success", "worker-b")


def test_report_rejected_when_task_not_active(task_id):
    # pending 任务不能直接上报
    with pytest.raises(RuntimeError, match="status"):
        repository.report_step_execution(task_id, 1, "success", "worker-a")


def test_report_rejected_when_step_not_found(task_id):
    _claim(task_id)
    _run(task_id)

    with pytest.raises(RuntimeError, match="not found"):
        repository.report_step_execution(task_id, 99, "success", "worker-a")


# ---------------------------------------------------------------
# complete_task_atomically：整任务原子提交
# ---------------------------------------------------------------

def test_complete_task_atomically_all_success(task_id):
    _claim(task_id)
    _run(task_id)

    out = repository.complete_task_atomically(
        task_id,
        "worker-a",
        results=[
            {"step_index": 1, "success": True, "message": "ok1"},
            {"step_index": 2, "success": True, "message": "ok2"},
        ],
    )
    assert out["task_status"] == "done"
    assert out["log_inserted"] == 2
    assert out["steps_updated"] == 2

    task = repository.get_task_with_steps(task_id)
    assert task["status"] == "done"
    assert [s["status"] for s in task["steps"]] == ["done", "done"]
    assert len(repository.list_step_logs(task_id)) == 2


def test_complete_task_atomically_any_failure_marks_failed(task_id):
    _claim(task_id)
    _run(task_id)

    out = repository.complete_task_atomically(
        task_id,
        "worker-a",
        results=[
            {"step_index": 1, "success": True, "message": "ok"},
            {"step_index": 2, "success": False, "message": "boom"},
        ],
    )
    assert out["task_status"] == "failed"

    task = repository.get_task_with_steps(task_id)
    assert task["status"] == "failed"
    assert task["steps"][0]["status"] == "done"
    assert task["steps"][1]["status"] == "failed"


def test_complete_task_atomically_rejects_missing_steps(task_id):
    _claim(task_id)
    _run(task_id)

    with pytest.raises(RuntimeError, match="not found"):
        repository.complete_task_atomically(
            task_id,
            "worker-a",
            results=[{"step_index": 99, "success": True}],
        )


def test_complete_task_atomically_rejects_duplicate_steps(task_id):
    _claim(task_id)
    _run(task_id)

    with pytest.raises(ValueError, match="duplicate"):
        repository.complete_task_atomically(
            task_id,
            "worker-a",
            results=[
                {"step_index": 1, "success": True},
                {"step_index": 1, "success": True},
            ],
        )


def test_complete_task_atomically_rejects_non_owner(task_id):
    _claim(task_id, "worker-a")
    _run(task_id)

    with pytest.raises(RuntimeError, match="claimed by"):
        repository.complete_task_atomically(
            task_id,
            "worker-b",
            results=[{"step_index": 1, "success": True}],
        )
