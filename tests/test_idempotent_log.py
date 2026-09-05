"""step_logs 幂等写入的数据库集成测试。

需要本地 MySQL taskkanban 库可用；测试会创建真实任务并清理。
"""

from __future__ import annotations

import concurrent.futures

import pytest

from app import repository


@pytest.fixture()
def task_id():
    tid = repository.create_task(
        base_params={"template_id": "T001", "channel": "email"},
        group_override={"channel": "sms"},
        steps=[
            {"step_index": 1, "override": {"action": "mock"}, "action": "mock"},
        ],
    )
    yield tid
    repository.delete_task_by_id(tid)


def test_first_log_inserted_and_duplicate_ignored():
    task_id = repository.create_task(
        base_params={},
        group_override={},
        steps=[
            {"step_index": 1, "override": {}, "action": "mock"},
        ],
    )
    try:
        first = repository.write_step_log(task_id, 1, "success", "first")
        second = repository.write_step_log(task_id, 1, "failure", "should not overwrite")

        assert first is True
        assert second is False

        logs = repository.list_step_logs(task_id)
        assert len(logs) == 1
        assert logs[0]["status"] == "success"
        assert logs[0]["message"] == "first"
    finally:
        repository.delete_task_by_id(task_id)


def test_concurrent_duplicate_reports_keep_one_log():
    tid = repository.create_task(
        base_params={},
        group_override={},
        steps=[{"step_index": 1, "override": {}, "action": "mock"}],
    )
    try:
        def report(i: int):
            return repository.write_step_log(tid, 1, "success", f"report-{i}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(report, range(5)))

        logs = repository.list_step_logs(tid)
        assert len(logs) == 1
        # 至少有一次插入成功，其余被忽略
        assert sum(1 for r in results if r is True) >= 1
        assert sum(1 for r in results if r is False) == 4
    finally:
        repository.delete_task_by_id(tid)
