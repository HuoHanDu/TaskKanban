"""参数合并演示脚本：创建一个演示任务并让 worker 跑完，打印参数演变。

用法：
    python scripts/demo_params.py [--count 1] [--worker-id demo-worker]

输出会打印每个 Step 实际看到的参数，直观展示 L1/L2/L3 粘性合并。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import repository  # noqa: E402
from app.worker import run_worker_once  # noqa: E402


DEMO_STEPS = [
    {
        "step_index": 1,
        "override": {"template_id": "T002", "send_at": ""},
        "action": "check_subscription",
    },
    {
        "step_index": 2,
        "override": {"action": "send_message"},
        "action": "send_message",
    },
    {
        "step_index": 3,
        "override": {"action": "record_receipt", "customer_name": "Alice"},
        "action": "record_receipt",
    },
]


def build_demo_task(index: int) -> dict:
    return {
        "base_params": {
            "customer_name": f"Customer-{index}",
            "email": f"customer{index}@example.com",
            "template_id": "T001",
            "channel": "email",
            "send_at": "2026-09-05 10:00:00",
        },
        "group_override": {
            "channel": "sms",
            "priority": "high",
        },
        "steps": json.loads(json.dumps(DEMO_STEPS)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="参数合并演示")
    parser.add_argument("--count", type=int, default=1, help="创建演示任务数量")
    parser.add_argument("--worker-id", default="demo-worker", help="worker 标识")
    args = parser.parse_args()

    print("=" * 70)
    print("参数合并演示：L1 base + L2 group + L3 step override")
    print("=" * 70)

    for i in range(1, args.count + 1):
        task = build_demo_task(i)
        tid = repository.create_task(
            task["base_params"],
            task["group_override"],
            task["steps"],
        )
        print(f"\n创建演示任务 id={tid}")
        print(f"base_params      = {json.dumps(task['base_params'], ensure_ascii=False)}")
        print(f"group_override   = {json.dumps(task['group_override'], ensure_ascii=False)}")
        for step in task["steps"]:
            print(f"  step{step['step_index']} override = "
                  f"{json.dumps(step['override'], ensure_ascii=False)}")

        print("\n--- worker 执行日志 ---")
        run_worker_once(args.worker_id, show_params=True)

        print("\n--- 最终状态 ---")
        detail = repository.get_task_with_steps(tid)
        print(f"task status      = {detail['status']}")
        logs = repository.list_step_logs(tid)
        for log in logs:
            print(f"  step{log['step_index']} status={log['status']} message={log['message']}")
        print("=" * 70)


if __name__ == "__main__":
    main()
