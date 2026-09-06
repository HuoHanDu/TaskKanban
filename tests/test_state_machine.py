"""任务/步骤状态机与并发持有权的集成测试。

需要本地 MySQL taskkanban 库可用；测试会创建真实任务并清理。

重点覆盖：
1. 合法状态迁移（pending -> claimed -> running -> done/failed）
2. 非法迁移必须被拒绝（done 后不能 failed、failed 后不能 running 等）
3. 非持有者不能推进任务状态
4. 任务释放 / 超时 claimed 回收
5. 步骤状态更新受任务持有者约束
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


def _task_status(tid: int) -> str:
    task = repository.get_task_with_steps(tid)
    assert task is not None
    return task["status"]


def _claim(tid: int, worker: str = "worker-a") -> bool:
    return repository.claim_by_id(tid, worker)


# ---------------------------------------------------------------
# 合法状态流转
# ---------------------------------------------------------------

def test_success_path_pending_claimed_running_done(task_id):
    assert _task_status(task_id) == "pending"

    assert _claim(task_id, "worker-a") is True
    assert _task_status(task_id) == "claimed"

    assert repository.mark_task_running(task_id, "worker-a") is True
    assert _task_status(task_id) == "running"

    assert repository.mark_task_done(task_id, "worker-a") is True
    assert _task_status(task_id) == "done"


def test_failure_path_pending_claimed_running_failed(task_id):
    assert _claim(task_id, "worker-a") is True
    assert repository.mark_task_running(task_id, "worker-a") is True

    assert repository.mark_task_failed(task_id, "worker-a") is True
    assert _task_status(task_id) == "failed"


# ---------------------------------------------------------------
# 非法状态迁移：必须返回 False 且状态不变
# ---------------------------------------------------------------

def test_pending_cannot_go_directly_done(task_id):
    assert repository.mark_task_done(task_id, "worker-a") is False
    assert _task_status(task_id) == "pending"


def test_pending_cannot_go_directly_failed(task_id):
    assert repository.mark_task_failed(task_id, "worker-a") is False
    assert _task_status(task_id) == "pending"


def test_pending_cannot_run_without_claim(task_id):
    assert repository.mark_task_running(task_id, "worker-a") is False
    assert _task_status(task_id) == "pending"


def test_claimed_cannot_be_reclaimed_by_second_worker(task_id):
    assert _claim(task_id, "worker-a") is True
    assert _claim(task_id, "worker-b") is False
    assert _task_status(task_id) == "claimed"
    task = repository.get_task_with_steps(task_id)
    assert task is not None
    assert task["claimed_by"] == "worker-a"


def test_done_cannot_be_failed_after_success(task_id):
    _claim(task_id, "worker-a")
    repository.mark_task_running(task_id, "worker-a")
    assert repository.mark_task_done(task_id, "worker-a") is True

    # 已完成任务不能再失败，也不能被其他 worker 改变
    assert repository.mark_task_failed(task_id, "worker-a") is False
    assert repository.mark_task_failed(task_id, "worker-b") is False
    assert _task_status(task_id) == "done"


def test_failed_cannot_return_to_running(task_id):
    _claim(task_id, "worker-a")
    repository.mark_task_running(task_id, "worker-a")
    repository.mark_task_failed(task_id, "worker-a")

    assert repository.mark_task_running(task_id, "worker-a") is False
    assert repository.mark_task_done(task_id, "worker-a") is False
    assert _task_status(task_id) == "failed"


# ---------------------------------------------------------------
# 持有者归属校验
# ---------------------------------------------------------------

def test_non_owner_cannot_mark_running(task_id):
    _claim(task_id, "worker-a")

    assert repository.mark_task_running(task_id, "worker-b") is False
    assert _task_status(task_id) == "claimed"
    assert repository.get_task_with_steps(task_id)["claimed_by"] == "worker-a"


def test_non_owner_cannot_mark_done(task_id):
    _claim(task_id, "worker-a")
    repository.mark_task_running(task_id, "worker-a")

    assert repository.mark_task_done(task_id, "worker-b") is False
    assert _task_status(task_id) == "running"


def test_non_owner_cannot_mark_failed(task_id):
    _claim(task_id, "worker-a")
    repository.mark_task_running(task_id, "worker-a")

    assert repository.mark_task_failed(task_id, "worker-b") is False
    assert _task_status(task_id) == "running"


# ---------------------------------------------------------------
# 步骤状态受任务持有者约束
# ---------------------------------------------------------------

def test_step_status_cannot_be_updated_by_non_owner(task_id):
    _claim(task_id, "worker-a")
    repository.mark_task_running(task_id, "worker-a")

    ok = repository.mark_step_status(
        task_id, 1, "done", worker_id="worker-b", finished=True
    )
    assert ok is False

    task = repository.get_task_with_steps(task_id)
    assert task["steps"][0]["status"] == "pending"


def test_step_status_can_be_updated_by_owner(task_id):
    _claim(task_id, "worker-a")
    repository.mark_task_running(task_id, "worker-a")

    ok = repository.mark_step_status(
        task_id, 1, "done", worker_id="worker-a", finished=True
    )
    assert ok is True

    task = repository.get_task_with_steps(task_id)
    assert task["steps"][0]["status"] == "done"


def test_step_status_cannot_be_updated_after_task_done(task_id):
    _claim(task_id, "worker-a")
    repository.mark_task_running(task_id, "worker-a")
    repository.mark_task_done(task_id, "worker-a")

    ok = repository.mark_step_status(
        task_id, 1, "failed", worker_id="worker-a", finished=True
    )
    assert ok is False
    task = repository.get_task_with_steps(task_id)
    assert task["steps"][0]["status"] == "pending"


# ---------------------------------------------------------------
# 释放与回收
# ---------------------------------------------------------------

def test_owner_can_release_claimed_task(task_id):
    _claim(task_id, "worker-a")
    assert repository.release_task(task_id, "worker-a") is True

    task = repository.get_task_with_steps(task_id)
    assert task["status"] == "pending"
    assert task["claimed_by"] is None


def test_non_owner_cannot_release_task(task_id):
    _claim(task_id, "worker-a")
    assert repository.release_task(task_id, "worker-b") is False
    assert _task_status(task_id) == "claimed"


def test_recover_expired_claims_resets_stale_claimed(task_id):
    # 不经过公开 API 修改时间太侵入；通过直接 SQL 把 claimed_at 改成过去。
    _claim(task_id, "worker-a")
    conn = repository.create_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE tasks SET claimed_at = NOW() - INTERVAL 9999 SECOND "
            "WHERE id = %s",
            (task_id,),
        )
        conn.commit()
    finally:
        conn.close()

    recovered = repository.recover_expired_claims(max_claimed_seconds=60)
    assert recovered >= 1

    task = repository.get_task_with_steps(task_id)
    assert task["status"] == "pending"
    assert task["claimed_by"] is None


def test_recover_expired_claims_does_not_reset_recent_claim(task_id):
    _claim(task_id, "worker-a")
    recovered = repository.recover_expired_claims(max_claimed_seconds=60)
    # 刚 claim 的不应被回收；可能库里还有其他过期任务，因此用任务状态单独断言。
    assert _task_status(task_id) == "claimed"


def test_owner_can_release_running_task(task_id):
    _claim(task_id, "worker-a")
    repository.mark_task_running(task_id, "worker-a")
    assert repository.release_task(task_id, "worker-a") is True
    assert _task_status(task_id) == "pending"


def test_recover_expired_running_resets_stale_running(task_id):
    _claim(task_id, "worker-a")
    assert repository.mark_task_running(task_id, "worker-a") is True

    conn = repository.create_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE tasks SET lease_expires_at = NOW() - INTERVAL 9999 SECOND "
            "WHERE id = %s",
            (task_id,),
        )
        conn.commit()
    finally:
        conn.close()

    recovered = repository.recover_expired_running(lease_seconds=60)
    assert recovered >= 1
    task = repository.get_task_with_steps(task_id)
    assert task["status"] == "pending"
    assert task["claimed_by"] is None
    assert task["started_at"] is None
    assert task["lease_expires_at"] is None


def test_recover_expired_running_does_not_reset_active_lease(task_id):
    _claim(task_id, "worker-a")
    assert repository.mark_task_running(task_id, "worker-a") is True

    recovered = repository.recover_expired_running(lease_seconds=60)
    assert recovered == 0
    assert _task_status(task_id) == "running"


# ---------------------------------------------------------------
# API 手动认领的幂等性（仓库层语义）
# ---------------------------------------------------------------

def test_claim_by_id_returns_false_when_already_claimed(task_id):
    assert _claim(task_id, "worker-a") is True
    assert _claim(task_id, "worker-b") is False
    assert _task_status(task_id) == "claimed"
