"""真实多进程并发认领演示脚本。

用法：
    python scripts/concurrency_demo.py --tasks 5 --workers 10 --rounds 10

说明：
- 每轮先创建 N 个 pending 任务；
- 启动 M 个 multiprocessing 子进程，每个子进程独立数据库连接同时认领；
- 统计重复认领次数，打印 “0 duplicate” 证据。
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import repository  # noqa: E402


def _claim_worker(worker_id: str, queue):
    """子进程入口：独立数据库连接认领一个任务。"""
    task = repository.claim_next_task(worker_id)
    queue.put(task["id"] if task is not None else None)


def run_one_round(round_no: int, task_count: int, worker_count: int) -> dict:
    """创建 task_count 个任务并让 worker_count 个进程同时认领。"""
    task_ids = [
        repository.create_task(
            base_params={"round": round_no, "i": i},
            group_override={},
            steps=[{"step_index": 1, "override": {}, "action": "mock"}],
        )
        for i in range(task_count)
    ]

    try:
        ctx = mp.get_context("spawn")
        queue = ctx.Queue()
        processes = [
            ctx.Process(
                target=_claim_worker,
                args=(f"worker-{round_no}-{i}", queue),
            )
            for i in range(worker_count)
        ]
        for p in processes:
            p.start()
        for p in processes:
            p.join(timeout=60)

        claimed = []
        while not queue.empty():
            value = queue.get()
            if value is not None:
                claimed.append(value)

        unique = set(claimed)
        return {
            "round": round_no,
            "tasks": task_count,
            "workers": worker_count,
            "claimed": len(claimed),
            "unique": len(unique),
            "duplicates": len(claimed) - len(unique),
        }
    finally:
        for tid in task_ids:
            repository.delete_task_by_id(tid)


def main() -> None:
    parser = argparse.ArgumentParser(description="真实多进程并发认领演示")
    parser.add_argument("--tasks", type=int, default=5, help="每轮 pending 任务数")
    parser.add_argument("--workers", type=int, default=10, help="并发认领进程数")
    parser.add_argument("--rounds", type=int, default=5, help="重复轮数")
    args = parser.parse_args()

    print(f"并发认领演示：每轮 {args.tasks} 个任务，{args.workers} 个 worker 进程，跑 {args.rounds} 轮")
    print("=" * 70)

    total_duplicates = 0
    for round_no in range(1, args.rounds + 1):
        result = run_one_round(round_no, args.tasks, args.workers)
        total_duplicates += result["duplicates"]
        print(
            f"round={result['round']:<3} "
            f"tasks={result['tasks']} workers={result['workers']} "
            f"claimed={result['claimed']} unique={result['unique']} "
            f"duplicates={result['duplicates']}"
        )

    print("=" * 70)
    print(f"总轮数 {args.rounds}，总重复认领次数 = {total_duplicates}")
    if total_duplicates == 0:
        print("结果：0 次重复认领，并发安全验证通过 [OK]")
    else:
        print("结果：存在重复认领 [FAIL]")


if __name__ == "__main__":
    main()
