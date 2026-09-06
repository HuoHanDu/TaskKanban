"""演示一：一个 Task 多个 Step 的 L1/L2/L3 参数覆盖与 Worker 实跑。

功能：
1. 打印任务完整的 L1 base、L2 group override、每个 Step 的 L3 override；
2. 打印合并后“每个 Step 实际看到的完整参数快照”；
3. 启动一个真实 worker（run_worker_once）领取该任务并执行；
4. 输出 worker 日志与最终 step_logs，展示参数粘性演变。

用法：
    python scripts/demo_params_task.py
    python scripts/demo_params_task.py --worker-id demo-worker
    python scripts/demo_params_task.py --no-run-worker   # 只看参数推导，不跑 DB/Worker
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Windows GBK 控制台直接打印中文会乱码，统一走 UTF-8 输出。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.params import apply_group, apply_step  # noqa: E402


# 一个“完整链”演示任务：
# - L2 覆盖 L1（channel email -> sms）
# - L2 引入新 key（priority）
# - L3 Step1 覆盖 template_id，send_at="" 表示不覆盖（保留 L1 的值）
# - L3 Step2 覆盖 priority
# - L3 Step3 引入新 key customer_name 和 retry
DEMO_TASK = {
    "base_params": {
        "customer_name": "Bob",
        "email": "bob@example.com",
        "template_id": "T001",
        "channel": "email",
        "send_at": "2026-09-05 10:00:00",
        "retry": 0,
    },
    "group_override": {
        "channel": "sms",
        "priority": "high",
    },
    "steps": [
        {
            "step_index": 1,
            "override": {"template_id": "T002", "send_at": "", "action": "check"},
            "action": "check",
        },
        {
            "step_index": 2,
            "override": {"priority": "urgent", "action": "send"},
            "action": "send",
        },
        {
            "step_index": 3,
            "override": {"customer_name": "Alice", "retry": 3, "action": "record"},
            "action": "record",
        },
    ],
}


def _fmt(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def preview_task(task: dict) -> None:
    """不依赖数据库，打印参数推导全过程。"""
    base = task["base_params"]
    group = task["group_override"]
    steps = task["steps"]

    print("=" * 78)
    print("参数覆盖演示：一个 Task 多个 Step（L1 base + L2 group + L3 step）")
    print("=" * 78)

    print("\n[L1] base_params")
    print("    " + _fmt(base))
    print("\n[L2] group_override（任务开始时一次性生效，空串按字面值）")
    print("    " + _fmt(group))

    initial = apply_group(base, group)
    print("\n[任务开始] current = L1 + L2")
    print("    " + _fmt(initial))

    current = initial
    snapshots = []
    print("\n[逐步执行] 每步应用 L3 override")
    for step in sorted(steps, key=lambda s: s["step_index"]):
        print(f"\n  Step{step['step_index']} override = {_fmt(step['override'])}")
        current = apply_step(current, step.get("override", {}))
        snapshots.append({"step_index": step["step_index"], "params": current})
        print(f"  Step{step['step_index']} 实际参数 = {_fmt(current)}")

    print("\n" + "-" * 78)
    print("每个 Step 看到的完整参数快照")
    for snap in snapshots:
        print(f"  Step{snap['step_index']}: {_fmt(snap['params'])}")
    print("-" * 78)


def main() -> None:
    parser = argparse.ArgumentParser(description="Task 多 Step 参数覆盖 + Worker 实跑演示")
    parser.add_argument("--worker-id", default="demo-worker", help="worker 标识")
    parser.add_argument(
        "--no-run-worker",
        action="store_true",
        help="只打印参数推导，不创建任务也不启动 worker",
    )
    args = parser.parse_args()

    preview_task(DEMO_TASK)

    if args.no_run_worker:
        return

    from app import repository  # noqa: PLC0415
    from app.worker import run_worker_once  # noqa: PLC0415

    task_id = repository.create_task(
        DEMO_TASK["base_params"],
        DEMO_TASK["group_override"],
        DEMO_TASK["steps"],
    )
    print(f"\n已创建演示任务 id={task_id}，开始由 worker={args.worker_id} 认领执行...\n")

    handled = run_worker_once(args.worker_id, show_params=True)
    print(f"\nworker 处理结果：handled={handled}")

    detail = repository.get_task_with_steps(task_id)
    print(f"\n任务最终状态：{detail['status']}")

    logs = repository.list_step_logs(task_id)
    print("\nstep_logs 最终内容：")
    for log in logs:
        print(f"  Step{log['step_index']} [{log['status']}] {log['message']}")

    repository.delete_task_by_id(task_id)
    print("\n演示任务已清理。")


if __name__ == "__main__":
    main()
