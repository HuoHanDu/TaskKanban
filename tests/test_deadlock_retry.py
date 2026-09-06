"""死锁自动重试测试：report_step_execution / complete_task_atomically。

通过第一次连接抛 errno=1213，第二次使用真实连接成功，验证装饰器会重跑
整个事务体而不是把错误暴露给调用方。
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


class _DeadlockConn:
    """第一次连接：在 start_transaction 时模拟 MySQL 死锁 1213。"""

    def start_transaction(self):
        exc = RuntimeError("Deadlock found when trying to get lock")
        exc.errno = 1213
        raise exc

    def rollback(self):
        pass

    def close(self):
        pass


def _make_factory(real_conn):
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        if calls["n"] == 1:
            return _DeadlockConn()
        return real_conn()

    return factory, calls


def test_report_step_execution_retries_on_deadlock(task_id, monkeypatch):
    assert repository.claim_by_id(task_id, "worker-a") is True
    assert repository.mark_task_running(task_id, "worker-a") is True

    real_conn = repository.create_connection
    fake_factory, calls = _make_factory(real_conn)
    monkeypatch.setattr(repository, "create_connection", fake_factory)

    out = repository.report_step_execution(
        task_id, 1, "success", "worker-a", message="retried"
    )

    assert calls["n"] == 2
    assert out["log_inserted"] is True
    assert repository.get_task_with_steps(task_id)["steps"][0]["status"] == "done"


def test_complete_task_atomically_retries_on_deadlock(task_id, monkeypatch):
    assert repository.claim_by_id(task_id, "worker-a") is True
    assert repository.mark_task_running(task_id, "worker-a") is True

    real_conn = repository.create_connection
    fake_factory, calls = _make_factory(real_conn)
    monkeypatch.setattr(repository, "create_connection", fake_factory)

    out = repository.complete_task_atomically(
        task_id,
        "worker-a",
        results=[
            {"step_index": 1, "success": True, "message": "ok1"},
            {"step_index": 2, "success": True, "message": "ok2"},
        ],
    )

    assert calls["n"] == 2
    assert out["task_status"] == "done"
    assert repository.get_task_with_steps(task_id)["status"] == "done"
