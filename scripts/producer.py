"""按速率生成随机任务的独立生产者脚本。

用途：
- 配合已启动的多个 worker，持续向数据库灌入任务；
- 默认每秒生成 5 个随机任务，持续指定秒数；
- 参数中携带 batch/seq/scheduled_at 等演示字段；
- 可选 --cleanup 结束时清理本脚本创建的任务。

用法：
    python scripts/producer.py --duration 10 --tasks-per-second 5
    python scripts/producer.py --duration 30 --tasks-per-second 5 --cleanup
    .venv/bin/python scripts/producer.py --duration 5 --log-every 10
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import repository  # noqa: E402

TEMPLATES = ["T001", "T002", "T003", "T004"]
CHANNELS = ["email", "sms", "push", "wechat"]
PRIORITIES = ["low", "normal", "high"]
NAMES = ["Alice", "Bob", "Carol", "David", "Eve"]
ACTIONS = ["check", "send", "record", "notify", "audit"]


def _now_text() -> str:
    return dt.datetime.now().strftime("%H:%M:%S")


def build_random_task(batch: int, seq: int) -> dict:
    """生成一个随机任务，参数携带 batch/seq/scheduled_at。

    scheduled_at 只是演示“参数可以携带时间”，不触发真实定时。
    """
    step_count = random.randint(2, 4)
    steps = []
    for idx in range(1, step_count + 1):
        action = random.choice(ACTIONS)
        override = {"action": action}
        if idx == 1:
            override["template_id"] = random.choice(TEMPLATES)
            override["send_at"] = ""  # L3 空串：不覆盖，沿用当前值
        if idx == step_count:
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
            "notice": "",
        },
        "steps": steps,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="按速率生成随机任务的生产者")
    parser.add_argument("--duration", type=float, default=10.0, help="生成持续秒数（默认 10）")
    parser.add_argument("--tasks-per-second", type=float, default=5.0, help="每秒任务数（默认 5）")
    parser.add_argument("--seed", type=int, default=None, help="随机种子")
    parser.add_argument("--cleanup", action="store_true", help="结束后删除本脚本创建的任务")
    parser.add_argument("--log-every", type=int, default=5, help="每创建多少个任务打印一条进度")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    created_ids: list[int] = []
    batch = 0
    per_second_int = max(1, int(args.tasks_per_second))
    generation_deadline = time.time() + args.duration
    second_deadline = time.time() + 1.0

    print(f"生产者启动：每秒约 {args.tasks_per_second} 个任务，持续 {args.duration}s",
          flush=True)

    try:
        while time.time() < generation_deadline:
            batch += 1
            for seq in range(1, per_second_int + 1):
                spec = build_random_task(batch, seq)
                tid = repository.create_task(
                    spec["base_params"],
                    spec["group_override"],
                    spec["steps"],
                )
                created_ids.append(tid)
                if len(created_ids) % args.log_every == 0:
                    print(
                        f"[{_now_text()}] created task={tid} "
                        f"(total={len(created_ids)})",
                        flush=True,
                    )

            wait = second_deadline - time.time()
            if wait > 0:
                time.sleep(wait)
            second_deadline += 1.0
    except KeyboardInterrupt:
        print("\n收到中断，停止生成", flush=True)

    print(f"\n生成结束，共创建 {len(created_ids)} 个任务", flush=True)

    if args.cleanup:
        for tid in created_ids:
            repository.delete_task_by_id(tid)
        print(f"已清理 {len(created_ids)} 个任务", flush=True)


if __name__ == "__main__":
    main()
