"""多进程 Worker + 按速率生成随机任务的压力/演示脚本。

功能：
1. 默认启动 5 个真实 worker 子进程（multiprocessing），每个进程独立 MySQL 连接；
2. 主进程按“每秒 5 个随机任务”的速率创建任务，参数中携带计划运行时间等演示字段；
3. worker 实时打印认领任务、每 Step 的 override 和合并后参数；
4. 全部结束后汇总数据库：
   - 任务状态分布；
   - 每个 worker 认领数量；
   - Step 日志总数 / 唯一日志数；
   - 抽样展示若干任务的 step_logs（含每个 Step 实际参数）。

用法：
    python scripts/stress_demo.py --workers 5 --duration 10 --tasks-per-second 5
    python scripts/stress_demo.py --workers 5 --duration 5 --log-limit 3 --cleanup

注意：Windows 下需要能正常创建 multiprocessing 管道（普通本机终端可以，
受限沙箱可能被禁止）。运行前请确保 MySQL 已初始化并配置 .env。
"""

from __future__ import annotations

import argparse
import datetime as dt
import multiprocessing as mp
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import repository  # noqa: E402
from app.worker import run_worker_once  # noqa: E402

TEMPLATES = ["T001", "T002", "T003", "T004"]
CHANNELS = ["email", "sms", "push", "wechat"]
PRIORITIES = ["low", "normal", "high"]
NAMES = ["Alice", "Bob", "Carol", "David", "Eve"]
ACTIONS = ["check", "send", "record", "notify", "audit"]


def _now_text() -> str:
    return dt.datetime.now().strftime("%H:%M:%S")


def _build_random_task(batch: int, seq: int) -> dict:
    """生成一个带随机参数和运行时间的任务。

    参数中的 scheduled_at 只是演示“任务参数可以携带时间信息”，
    并不真正定时触发；真正执行时间取决于 worker 认领时刻。
    """
    step_count = random.randint(2, 4)
    steps = []
    for idx in range(1, step_count + 1):
        override = {}
        action = random.choice(ACTIONS)
        override["action"] = action
        if idx == 1:
            # Step1 引入 template_id，演示 L3 粘性起始
            override["template_id"] = random.choice(TEMPLATES)
            # 空字符串：演示 L3 不覆盖、沿用当前生效值
            override["send_at"] = ""
        if idx == step_count:
            # 最后一步引入新 key，演示粘性贯穿到末尾
            override["customer_name"] = random.choice(NAMES)
        steps.append(
            {
                "step_index": idx,
                "override": override,
                "action": action,
            }
        )

    scheduled_at = (
        dt.datetime.now() + dt.timedelta(seconds=random.randint(0, 30))
    ).isoformat(sep=" ", timespec="seconds")

    return {
        "base_params": {
            "customer_id": f"cust-{batch}-{seq}",
            "email": f"cust{batch}-{seq}@example.com",
            "template_id": "T000",
            "channel": random.choice(CHANNELS),
            "scheduled_at": scheduled_at,
            "batch": batch,
        },
        "group_override": {
            "priority": random.choice(PRIORITIES),
            # L2 空字符串按字面值处理，特意保留一个演示字段
            "notice": "",
        },
        "steps": steps,
    }


def worker_loop(worker_id: str, stop_after: float) -> None:
    """worker 子进程入口：循环认领并执行任务，直到运行时间截止。"""
    deadline = time.time() + stop_after
    empty_waits = 0
    while time.time() < deadline:
        try:
            handled = run_worker_once(worker_id, show_params=True)
            if not handled:
                empty_waits += 1
                # 没有任务时短暂等待，避免空转
                time.sleep(0.2)
            else:
                empty_waits = 0
        except Exception:
            import traceback
            traceback.print_exc()
            time.sleep(0.5)


def main() -> None:
    parser = argparse.ArgumentParser(description="多进程 Worker + 随机任务演示")
    parser.add_argument("--workers", type=int, default=5, help="worker 进程数（默认 5）")
    parser.add_argument("--duration", type=int, default=10, help="任务生成持续秒数（默认 10）")
    parser.add_argument("--tasks-per-second", type=float, default=5.0, help="每秒生成任务数（默认 5）")
    parser.add_argument("--settle-seconds", type=int, default=8, help="生成结束后等待 worker 消化任务的秒数（默认 8）")
    parser.add_argument("--log-limit", type=int, default=5, help="最后抽样展示多少个任务的日志（默认 5）")
    parser.add_argument("--seed", type=int, default=None, help="随机种子，便于复现")
    parser.add_argument("--cleanup", action="store_true", help="结束后删除本次创建的任务")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    total_slots = int(args.duration * args.tasks_per_second)
    print("=" * 80)
    print(f"压力/演示：{args.workers} worker 进程，每秒 {args.tasks_per_second} 个任务，持续 {args.duration}s，预计 {total_slots} 个任务")
    print("=" * 80, flush=True)

    # 1. 启动 worker 进程
    ctx = mp.get_context("spawn")
    stop_after = args.duration + args.settle_seconds
    processes = []
    for i in range(1, args.workers + 1):
        p = ctx.Process(target=worker_loop, args=(f"demo-worker-{i}", stop_after))
        p.start()
        processes.append(p)
        print(f"[{_now_text()}] 启动 worker demo-worker-{i} (pid={p.pid})", flush=True)

    # 2. 按速率生成随机任务
    created_ids: list[int] = []
    batch = 0
    second_deadline = time.time() + 1.0
    generation_deadline = time.time() + args.duration

    try:
        while time.time() < generation_deadline:
            batch += 1
            for seq in range(1, int(args.tasks_per_second) + 1):
                spec = _build_random_task(batch, seq)
                tid = repository.create_task(
                    spec["base_params"],
                    spec["group_override"],
                    spec["steps"],
                )
                created_ids.append(tid)
                print(
                    f"[{_now_text()}] [producer] created task={tid} "
                    f"batch={batch} priority={spec['group_override']['priority']} "
                    f"steps={len(spec['steps'])}",
                    flush=True,
                )

            # 保持每秒一批的节奏
            wait = second_deadline - time.time()
            if wait > 0:
                time.sleep(wait)
            second_deadline += 1.0
    except KeyboardInterrupt:
        print("\n收到中断，停止生成任务...", flush=True)

    print(f"\n[{_now_text()}] 任务生成结束，共创建 {len(created_ids)} 个任务，等待 worker 消化...", flush=True)

    # 3. 等待 worker 结束
    remaining = max(5.0, stop_after - time.time())
    for p in processes:
        p.join(timeout=remaining)
    for p in processes:
        if p.is_alive():
            print(f"worker {p.pid} 仍在运行，强制结束", flush=True)
            p.terminate()
            p.join(timeout=5)

    # 4. 数据库汇总
    print("\n" + "=" * 80)
    print("数据库汇总")
    print("=" * 80, flush=True)

    status_counter: dict[str, int] = {}
    claimed_counter: dict[str, int] = {}
    log_total = 0
    log_unique = 0
    sample_tasks = created_ids[: args.log_limit]

    for tid in created_ids:
        detail = repository.get_task_with_steps(tid)
        if detail is None:
            continue
        status_counter[detail["status"]] = status_counter.get(detail["status"], 0) + 1
        owner = detail["claimed_by"] or "-"
        claimed_counter[owner] = claimed_counter.get(owner, 0) + 1

        logs = repository.list_step_logs(tid)
        log_total += len(logs)
        log_unique += len({(log["task_id"], log["step_index"]) for log in logs})

    print(f"创建任务总数      : {len(created_ids)}")
    print(f"任务状态分布      : {status_counter if status_counter else '无'}")
    print(f"worker 认领分布   : {dict(sorted(claimed_counter.items()))}")
    print(f"Step 日志总行数   : {log_total}")
    print(f"唯一 (task,step)  : {log_unique}")

    unfinished = {k: v for k, v in status_counter.items() if k in {"pending", "claimed", "running"}}
    if unfinished:
        print(f"\n⚠️  仍有 {sum(unfinished.values())} 个任务未完成（{unfinished}）；"
              f"可调大 --settle-seconds 或 --duration 后重试。")
    else:
        print("\n所有任务均已完成 [OK]")

    # 5. 抽样展示数据库 step_logs
    if sample_tasks:
        print("\n" + "=" * 80)
        print(f"抽样展示数据库日志（前 {len(sample_tasks)} 个任务）")
        print("=" * 80, flush=True)

        for tid in sample_tasks:
            detail = repository.get_task_with_steps(tid)
            if detail is None:
                continue
            logs = repository.list_step_logs(tid)
            print(f"\n任务 {tid}  status={detail['status']}  claimed_by={detail['claimed_by']}")
            if not logs:
                print("  (无日志)")
            for log in logs:
                print(f"  Step{log['step_index']} [{log['status']}] {log['message']}")

    # 6. 可选清理
    if args.cleanup:
        for tid in created_ids:
            repository.delete_task_by_id(tid)
        print(f"\n已清理 {len(created_ids)} 个任务。")

    print("\n演示结束。")


if __name__ == "__main__":
    main()
