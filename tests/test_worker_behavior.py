"""Worker 行为测试：排序、失败路径、回收间隔。

这些测试调用真实 repository / executor，需要 MySQL；任务会创建并清理。
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from app import repository
from app.executor import StepResult
from app.worker import (
    _run_claimed_task,
    main_loop_forever,
    run_worker_once,
    validate_fail_rate,
)


@pytest.fixture()
def task_with_owner():
    """创建任务并手动推进到 running，返回 (task_id, 乱序 task dict, worker)。"""
    worker = "worker-test"
    tid = repository.create_task(
        base_params={"v": "base"},
        group_override={},
        steps=[
            {"step_index": 1, "override": {}, "action": "mock"},
            {"step_index": 2, "override": {}, "action": "mock"},
        ],
    )
    try:
        assert repository.claim_by_id(tid, worker) is True
        assert repository.mark_task_running(tid, worker) is True
        detail = repository.get_task_with_steps(tid)
        # 人为构造乱序 steps：即使 DB/上游返回乱序，worker 也应先排序。
        unsorted_task = {
            **detail,
            "steps": list(reversed(detail["steps"])),
        }
        yield tid, unsorted_task, worker
    finally:
        repository.delete_task_by_id(tid)


def _extract_params(message: str) -> dict:
    marker = "params="
    start = message.index(marker) + len(marker)
    return eval(message[start:])


def test_worker_sorts_steps_by_step_index_before_execution(task_with_owner):
    """即使传入乱序 steps，worker 仍按 step_index 升序执行并完成粘性合并。"""
    tid, unsorted_task, worker = task_with_owner

    # 两个 Step 的 override 都是 v 的新值；必须升序执行，最终 done 且日志顺序正确。
    unsorted_task["steps"][0]["override"] = {"v": "step2-value"}  # 原 step2
    unsorted_task["steps"][1]["override"] = {"v": "step1-value"}  # 原 step1
    # repository 已把 override JSON 存库并返回 dict，上面直接修改内存对象即可。

    assert _run_claimed_task(
        unsorted_task, worker, show_params=False, sleep_seconds=0
    ) is True

    logs = repository.list_step_logs(tid)
    assert [log["step_index"] for log in logs] == [1, 2]
    step1_params = _extract_params(logs[0]["message"])
    step2_params = _extract_params(logs[1]["message"])
    assert step1_params["v"] == "step1-value"
    assert step2_params["v"] == "step2-value"


def test_worker_failure_stops_and_backfills_remaining_steps(task_with_owner):
    """失败即停止：Step1 成功、Step2 失败，后续 Step 不再执行并补齐失败。"""
    tid, task, worker = task_with_owner

    # 给任务多加一个 Step3，验证补齐
    conn = repository.create_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO steps (task_id, step_index, override, action) "
            "VALUES (%s, 3, %s, 'mock')",
            (tid, repository._json_dumps({})),
        )
        conn.commit()
    finally:
        conn.close()
    # 重新构造带 3 步的乱序 task dict
    detail = repository.get_task_with_steps(tid)
    task = {**detail, "steps": list(reversed(detail["steps"]))}

    def fake_execute_step(step, params, **kwargs):
        idx = int(step["step_index"])
        if idx == 1:
            return StepResult(success=True, message="step1-ok")
        if idx == 2:
            return StepResult(success=False, message="step2-boom")
        raise AssertionError("step 3 should not be executed after step 2 failure")

    with patch("app.worker.execute_step", side_effect=fake_execute_step):
        assert _run_claimed_task(task, worker, show_params=False) is True

    task_row = repository.get_task_with_steps(tid)
    assert task_row["status"] == "failed"
    assert [s["status"] for s in task_row["steps"]] == ["done", "failed", "failed"]

    logs = repository.list_step_logs(tid)
    by_step = {log["step_index"]: log for log in logs}
    assert len(logs) == 3
    assert by_step[1]["status"] == "success"
    assert by_step[1]["message"] == "step1-ok"
    assert by_step[2]["status"] == "failure"
    assert by_step[2]["message"] == "step2-boom"
    assert by_step[3]["status"] == "failure"
    assert "not executed because step 2 failed" in by_step[3]["message"]


def test_run_worker_once_with_fail_rate_marks_task_failed():
    """通过公开入口 run_worker_once + fail_rate 注入中间 Step 失败。"""
    worker = "fail-worker"
    tid = repository.create_task(
        base_params={},
        group_override={},
        steps=[
            {"step_index": 1, "override": {}, "action": "mock"},
            {"step_index": 2, "override": {}, "action": "mock"},
            {"step_index": 3, "override": {}, "action": "mock"},
        ],
    )
    try:
        # 真实 execute_step + fail_rate=0.5：
        # 随机序列让 Step1 不失败(0.99)、Step2 失败(0.01)，Step3 不应再取随机数。
        with patch("app.executor.random.random", side_effect=[0.99, 0.01]):
            assert run_worker_once(worker, show_params=False, fail_rate=0.5) is True

        task = repository.get_task_with_steps(tid)
        assert task["status"] == "failed"
        assert [s["status"] for s in task["steps"]] == ["done", "failed", "failed"]
        logs = repository.list_step_logs(tid)
        assert len(logs) == 3
        assert {log["step_index"] for log in logs} == {1, 2, 3}
        by_step = {log["step_index"]: log for log in logs}
        assert by_step[1]["status"] == "success"
        assert by_step[2]["status"] == "failure"
        assert by_step[3]["status"] == "failure"
    finally:
        repository.delete_task_by_id(tid)


def test_recover_claimed_interval_is_actual_interval_not_every_loop():
    """recover_claimed_interval=5 时，短时空轮不应每轮触发回收。"""
    recover_calls: list[float] = []
    worker_calls = [0]

    def spy_recover(max_claimed_seconds: float) -> int:
        recover_calls.append(time.monotonic())
        return 0

    def empty_worker(*args, **kwargs):
        worker_calls[0] += 1
        if worker_calls[0] >= 3:
            raise KeyboardInterrupt
        return False

    with patch("app.worker.recover_expired_claims", side_effect=spy_recover), \
         patch("app.worker.run_worker_once", side_effect=empty_worker):
        start = time.monotonic()
        main_loop_forever(
            worker_id="interval-worker",
            interval=0.001,
            recover_claimed_interval=5.0,
            show_params=False,
        )
        elapsed = time.monotonic() - start

    # 跑 3 轮、总耗时远小于 5s；回收应只发生 1 次（启动时），而不是 3 次。
    assert worker_calls[0] == 3
    assert len(recover_calls) == 1, (
        f"recover should only run once in a short loop, got {len(recover_calls)}"
    )
    assert elapsed < 5.0


def test_recover_claimed_interval_zero_disables_recovery():
    """recover_claimed_interval <= 0 表示关闭回收。"""
    recover_calls: list[float] = []
    worker_calls = [0]

    def spy_recover(max_claimed_seconds: float) -> int:
        recover_calls.append(time.monotonic())
        return 0

    def empty_worker(*args, **kwargs):
        worker_calls[0] += 1
        if worker_calls[0] >= 2:
            raise KeyboardInterrupt
        return False

    with patch("app.worker.recover_expired_claims", side_effect=spy_recover), \
         patch("app.worker.run_worker_once", side_effect=empty_worker):
        main_loop_forever(
            worker_id="interval-worker",
            interval=0.001,
            recover_claimed_interval=0,
            show_params=False,
        )

    assert worker_calls[0] == 2
    assert recover_calls == []


@pytest.mark.parametrize("bad", [-0.1, 1.1, 2.0])
def test_validate_fail_rate_rejects_out_of_range(bad):
    with pytest.raises(ValueError, match="between 0.0 and 1.0"):
        validate_fail_rate(bad)


def test_validate_fail_rate_accepts_bounds():
    assert validate_fail_rate(0.0) == 0.0
    assert validate_fail_rate(1.0) == 1.0
