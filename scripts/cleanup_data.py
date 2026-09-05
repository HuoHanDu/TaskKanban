"""清理 TaskKanban 数据库中的任务 / 步骤 / 日志数据。

功能：
- 默认只统计，不删除（安全模式）；
- 加 --delete 才真正删除；
- 加 --all 删除所有任务；否则只删除 done/failed/claimed 等历史任务；
- 可选只删除指定任务 id。

用法：
    # 统计
    python scripts/cleanup_data.py

    # 删除已完成/失败/认领等历史任务
    python scripts/cleanup_data.py --delete

    # 删除全部任务（含 pending）
    python scripts/cleanup_data.py --delete --all

    # 删除指定任务
    python scripts/cleanup_data.py --task-id 123 --delete

    # 服务器虚拟环境
    .venv/bin/python scripts/cleanup_data.py --delete
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import repository  # noqa: E402


def _delete_task_conn(task_id: int) -> None:
    """通过 repository 删除任务（级联删除 steps/step_logs）。"""
    repository.delete_task_by_id(task_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="清理任务数据")
    parser.add_argument("--delete", action="store_true", help="真正删除；不加则只统计")
    parser.add_argument("--all", action="store_true", help="删除全部任务（含 pending）")
    parser.add_argument("--task-id", type=int, default=None, help="只清理指定任务 id")
    args = parser.parse_args()

    tasks = repository.list_tasks()

    if args.task_id is not None:
        target = [t for t in tasks if t["id"] == args.task_id]
        if not target:
            print(f"任务 {args.task_id} 不存在")
            return
        tasks = target

    if not args.all and args.task_id is None:
        # 默认清理非 pending 的历史任务；保留 pending 防止误删正在排队的数据
        tasks = [t for t in tasks if t["status"] != "pending"]

    if not tasks:
        print("没有需要清理的任务")
        return

    print(f"待清理任务数: {len(tasks)}")
    for t in tasks[:20]:
        print(f"  id={t['id']} status={t['status']} claimed_by={t['claimed_by']}")
    if len(tasks) > 20:
        print(f"  ... 其余 {len(tasks) - 20} 个省略")

    if not args.delete:
        print("\n这是统计模式，未删除任何数据。加 --delete 执行真正删除。")
        return

    print("\n开始删除...")
    for i, t in enumerate(tasks, start=1):
        _delete_task_conn(t["id"])
        if i % 50 == 0 or i == len(tasks):
            print(f"  已删除 {i}/{len(tasks)}")
    print(f"完成，共删除 {len(tasks)} 个任务及其步骤/日志。")


if __name__ == "__main__":
    main()
