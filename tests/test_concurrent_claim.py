"""并发认领测试：用真实多进程攻击认领逻辑。

注意：此测试依赖本地 MySQL taskkanban 库，并且会创建/删除真实任务。
Windows 下使用 multiprocessing spawn 方式，需要独立脚本入口。
"""

from __future__ import annotations

import multiprocessing as mp
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import repository  # noqa: E402


def _claim_worker(worker_id: str, queue):
    """子进程入口：独立数据库连接认领一个任务，把 id 放入队列。"""
    task = repository.claim_next_task(worker_id)
    if task is not None:
        queue.put(task["id"])
    else:
        queue.put(None)


@pytest.fixture()
def pending_tasks():
    ids = []
    for i in range(5):
        tid = repository.create_task(
            base_params={"customer": f"c{i}"},
            group_override={},
            steps=[{"step_index": 1, "override": {}, "action": "mock"}],
        )
        ids.append(tid)
    yield ids
    for tid in ids:
        repository.delete_task_by_id(tid)


def test_concurrent_claim_no_duplicate(pending_tasks):
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    workers = [ctx.Process(target=_claim_worker, args=(f"worker-{i}", queue)) for i in range(10)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=30)

    claimed = []
    while not queue.empty():
        claimed.append(queue.get())

    # 去掉 None（没抢到的）
    claimed_ids = [x for x in claimed if x is not None]
    # 不应有重复
    assert len(claimed_ids) == len(set(claimed_ids)), f"duplicate claims: {claimed_ids}"
    # 抢到数量不超过总任务数
    assert len(claimed_ids) <= len(pending_tasks)
    # 所有返回的 id 都属于 pending_tasks
    assert set(claimed_ids) <= set(pending_tasks)
