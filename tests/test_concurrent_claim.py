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
    worker_count = 10
    task_count = len(pending_tasks)
    workers = [
        ctx.Process(target=_claim_worker, args=(f"worker-{i}", queue))
        for i in range(worker_count)
    ]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=30)

    # a) 每个子进程必须正常退出（exitcode == 0），不能因异常静默失败
    exitcodes = [w.exitcode for w in workers]
    assert all(code == 0 for code in exitcodes), f"workers crashed: {exitcodes}"

    # b) 队列里必须收到 worker_count 个结果（含 None）
    claimed = []
    for _ in range(worker_count):
        claimed.append(queue.get(timeout=5))
    assert len(claimed) == worker_count, (
        f"expected {worker_count} queue results, got {len(claimed)}; "
        f"queue.empty() may be unreliable after abnormal child exit"
    )

    # 去掉 None（没抢到的）
    claimed_ids = [x for x in claimed if x is not None]
    # c) 抢到任务数 == min(worker_count, task_count)
    #    worker 数 >= 任务数时应恰好等于任务数
    expected_claimed = min(worker_count, task_count)
    assert len(claimed_ids) == expected_claimed, (
        f"expected {expected_claimed} claimed tasks, got {len(claimed_ids)}: "
        f"{claimed_ids}"
    )
    # d) 无重复且都是本轮创建的任务 id
    assert len(claimed_ids) == len(set(claimed_ids)), f"duplicate claims: {claimed_ids}"
    assert set(claimed_ids) <= set(pending_tasks), (
        f"claimed ids outside this round: {set(claimed_ids) - set(pending_tasks)}"
    )
