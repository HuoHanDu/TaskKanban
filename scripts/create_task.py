"""命令行创建演示任务。

用法：
    python scripts/create_task.py --count 5
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 允许直接以 scripts 下的方式运行时导入 app 包
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.repository import create_task, list_tasks  # noqa: E402


DEMO_STEPS = [
    {
        "step_index": 1,
        "override": {"action": "check_subscription", "template_id": "T002", "send_at": ""},
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
        "steps": json.loads(json.dumps(DEMO_STEPS)),  # 深拷贝避免共享
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="创建演示任务")
    parser.add_argument("--count", type=int, default=3, help="创建任务数量")
    args = parser.parse_args()

    for i in range(1, args.count + 1):
        task = build_demo_task(i)
        task_id = create_task(task["base_params"], task["group_override"], task["steps"])
        print(f"created task id={task_id}")

    print("\n当前任务:")
    for t in list_tasks():
        print(f"  id={t['id']} status={t['status']}")


if __name__ == "__main__":
    main()
