"""并发安全认领演示：打印 MySQL 防重复设计，并做真实多进程认领/消费测试。

本脚本整合了仓库中已有的 scripts/concurrency_demo.py（多进程抢认领）与
scripts/stress_demo.py（Worker + Producer 消费）的核心能力，方便一次讲清：
1. MySQL 为什么不会重复认领；
2. 真实多进程并发认领结果（0 duplicate）；
3. 可选：启动 N 个 Worker 进程 + 主进程生成任务，验证任务能被唯一消费。

用法：
    # 只做多进程抢认领（默认，不启动常驻 worker）
    python scripts/demo_concurrency.py --rounds 5 --tasks 5 --workers 10

    # 抢认领 + 真实 worker 消费压力演示
    python scripts/demo_concurrency.py --rounds 2 --with-workers 5 \
        --duration 5 --tasks-per-second 5 --settle-seconds 5

    # 服务器虚拟环境
    .venv/bin/python scripts/demo_concurrency.py --rounds 5 --tasks 5 --workers 10
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import random
import sys
import time
from pathlib import Path

# Windows GBK 控制台直接打印中文会乱码，统一走 UTF-8 输出。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import repository  # noqa: E402
from app.worker import run_worker_once  # noqa: E402

ACTIONS = ["check", "send", "record", "notify", "audit"]


# ---------------------------------------------------------------
# 第一部分：打印 MySQL 防重复设计
# ---------------------------------------------------------------

def print_mysql_design() -> None:
    print("=" * 78)
    print("MySQL 如何防止并发重复领取任务")
    print("=" * 78)
    print("""
1) InnoDB 行锁 + 当前读
   SELECT id FROM tasks
   WHERE status='pending'
   ORDER BY created_at, id
   LIMIT 1
   FOR UPDATE SKIP LOCKED
   - FOR UPDATE：对候选行加排他锁；
   - SKIP LOCKED：其他事务跳过已被锁的行，不阻塞等待。

2) 条件 UPDATE（第二道防线）
   UPDATE tasks
   SET status='claimed', claimed_by=%s, claimed_at=NOW()
   WHERE id=%s AND status='pending'
   - 只有 rowcount=1 才算认领成功；
   - 即使行锁内读到旧状态，也会被外层 status 条件挡住。

3) 任务唯一性体现在“行状态 + 行锁”，不是应用层判断
   - 两个 Worker 同时来，只有一个事务能锁定同一行；
   - 另一个 SKIP 到下一行或返回空，因此不会两个都拿到同一个任务。

4) 持有者约束
   - 后续 running/done/failed/release 都要求 claimed_by=当前 worker；
   - 防止 A 认领后 B 越权推进。
""")


# ---------------------------------------------------------------
# 第二部分：多进程抢认领（0 duplicate 证据）
# ---------------------------------------------------------------

def _claim_worker(worker_id: str, queue):
    task = repository.claim_next_task(worker_id)
    queue.put(task["id"] if task is not None else None)


def run_claim_round(round_no: int, task_count: int, worker_count: int) -> dict:
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
                args=(f"claim-worker-r{round_no}-{i}", queue),
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


def run_claim_test(rounds: int, tasks: int, workers: int) -> None:
    print("\n" + "=" * 78)
    print(f"真实多进程并发认领：每轮 {tasks} 个任务，{workers} 个进程抢，跑 {rounds} 轮")
    print("=" * 78)
    total_duplicates = 0
    for round_no in range(1, rounds + 1):
        result = run_claim_round(round_no, tasks, workers)
        total_duplicates += result["duplicates"]
        print(
            f"round={result['round']:<3} tasks={result['tasks']} "
            f"workers={result['workers']} claimed={result['claimed']} "
            f"unique={result['unique']} duplicates={result['duplicates']}"
        )
    print("-" * 78)
    print(f"总重复认领次数 = {total_duplicates}")
    if total_duplicates == 0:
        print("结果：0 次重复认领，并发安全验证通过 [OK]")
    else:
        print("结果：存在重复认领 [FAIL]")


# ---------------------------------------------------------------
# 第三部分：真实 Worker + Producer 消费（可选）
# ---------------------------------------------------------------

def _random_task(batch: int, seq: int) -> dict:
    step_count = random.randint(2, 4)
    steps = []
    for idx in range(1, step_count + 1):
        action = random.choice(ACTIONS)
        override = {"action": action}
        if idx == 1:
            override["template_id"] = f"T{random.randint(1, 4)}"
            override["send_at"] = ""
        if idx == step_count:
            override["customer_name"] = f"cust-{batch}-{seq}"
        steps.append({"step_index": idx, "override": override, "action": action})
    return {
        "base_params": {
            "batch": batch,
            "seq": seq,
            "template_id": "T000",
            "channel": random.choice(["email", "sms", "push"]),
        },
        "group_override": {"priority": random.choice(["low", "normal", "high"])},
        "steps": steps,
    }


def _worker_loop(worker_id: str, stop_after: float) -> None:
    deadline = time.time() + stop_after
    while time.time() < deadline:
        try:
            handled = run_worker_once(worker_id, show_params=False)
            if not handled:
                time.sleep(0.2)
        except Exception:
            import traceback
            traceback.print_exc()
            time.sleep(0.5)


def run_worker_producer_demo(
    workers: int,
    duration: float,
    tasks_per_second: float,
    settle_seconds: float,
) -> None:
    print("\n" + "=" * 78)
    print(f"真实 Worker 消费演示：{workers} 个进程，每秒 {tasks_per_second} 个任务，"
          f"持续 {duration}s")
    print("=" * 78)
    ctx = mp.get_context("spawn")
    stop_after = duration + settle_seconds
    processes = []
    for i in range(1, workers + 1):
        p = ctx.Process(target=_worker_loop, args=(f"demo-w-{i}", stop_after))
        p.start()
        processes.append(p)
        print(f"started demo-w-{i} pid={p.pid}", flush=True)

    created_ids: list[int] = []
    try:
        second_deadline = time.time() + 1.0
        generation_deadline = time.time() + duration
        batch = 0
        while time.time() < generation_deadline:
            batch += 1
            for seq in range(1, int(tasks_per_second) + 1):
                spec = _random_task(batch, seq)
                tid = repository.create_task(
                    spec["base_params"], spec["group_override"], spec["steps"]
                )
                created_ids.append(tid)
            wait = second_deadline - time.time()
            if wait > 0:
                time.sleep(wait)
            second_deadline += 1.0
    except KeyboardInterrupt:
        print("producer interrupted", flush=True)

    print(f"created {len(created_ids)} tasks; waiting workers...", flush=True)
    for p in processes:
        p.join(timeout=max(5.0, stop_after - time.time()))
    for p in processes:
        if p.is_alive():
            p.terminate()
            p.join(timeout=5)

    status_counter: dict[str, int] = {}
    for tid in created_ids:
        detail = repository.get_task_with_steps(tid)
        if detail is not None:
            status_counter[detail["status"]] = status_counter.get(detail["status"], 0) + 1
    print(f"任务状态分布: {status_counter}")

    unfinished = {k: v for k, v in status_counter.items()
                  if k in {"pending", "claimed", "running"}}
    if unfinished:
        print(f"仍有未完成任务: {unfinished}（可调大 --settle-seconds）")
    else:
        print("所有任务均被唯一消费并完成 [OK]")

    # 清理本次生成数据，避免污染业务库
    for tid in created_ids:
        repository.delete_task_by_id(tid)
    print("已清理本次演示任务。")


# ---------------------------------------------------------------
# main
# ---------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="并发安全认领演示")
    parser.add_argument("--rounds", type=int, default=5, help="抢认领轮数")
    parser.add_argument("--tasks", type=int, default=5, help="每轮 pending 任务数")
    parser.add_argument("--workers", type=int, default=10, help="每轮抢任务进程数")
    parser.add_argument("--with-workers", type=int, default=0,
                        help=">0 时额外启动 N 个真实 Worker 消费任务")
    parser.add_argument("--duration", type=float, default=5.0, help="生成任务持续秒数")
    parser.add_argument("--tasks-per-second", type=float, default=5.0, help="每秒任务数")
    parser.add_argument("--settle-seconds", type=float, default=5.0, help="生成后等待秒数")
    parser.add_argument("--seed", type=int, default=None, help="随机种子")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    print_mysql_design()
    run_claim_test(args.rounds, args.tasks, args.workers)
    if args.with_workers > 0:
        run_worker_producer_demo(
            args.with_workers,
            args.duration,
            args.tasks_per_second,
            args.settle_seconds,
        )

    print("\n演示结束。")


if __name__ == "__main__":
    main()
