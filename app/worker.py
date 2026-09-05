"""Worker 进程：循环认领任务并执行全部步骤。

启动方式：
    python -m app.worker --worker-id worker-1
"""

from __future__ import annotations

import argparse
import logging
import time

from app.executor import execute_step
from app.params import apply_step, apply_group
from app import repository
from app.repository import complete_task_atomically, recover_expired_claims, release_task

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(process)d] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def run_worker_once(worker_id: str) -> bool:
    """认领并执行一个任务；没有可执行任务时返回 False。"""
    task = repository.claim_next_task(worker_id)
    if task is None:
        return False

    task_id = task["id"]
    logger.info("worker=%s claimed task=%s", worker_id, task_id)

    # 只有真正从 claimed 推进到 running 成功，才继续执行。
    # 防止 worker 认领后任务已被其他调用方释放/回收/修改。
    if not repository.mark_task_running(task_id, worker_id):
        logger.warning("worker=%s lost ownership of task=%s before running",
                       worker_id, task_id)
        return True

    try:
        # 按顺序逐步执行，边执行边合并参数（粘性）
        current = apply_group(task["base_params"], task["group_override"])
        results: list[dict] = []
        for step in task["steps"]:
            current = apply_step(current, step["override"])
            result = execute_step(step, current)
            results.append(
                {
                    "step_index": step["step_index"],
                    "success": result.success,
                    "message": result.message,
                }
            )

        # 所有 Step 都模拟执行完成后，在单个事务内原子提交：
        # 日志 + Step 状态 + 任务终态要么全部成功，要么全部回滚。
        outcome = repository.complete_task_atomically(
            task["id"],
            worker_id,
            results=results,
        )
        if outcome["task_status"] == "failed":
            failed_step = next(
                (r["step_index"] for r in results if not r["success"]), None
            )
            logger.info("worker=%s task=%s failed at step=%s",
                        worker_id, task["id"], failed_step)
        else:
            logger.info("worker=%s task=%s done", worker_id, task["id"])
        return True
    except Exception:
        # 执行中出现未预期异常：主动释放任务，便于其他 worker 重新认领。
        # 若 complete_task_atomically 已部分提交，由于它整体回滚，不会留下中间态。
        logger.exception("worker=%s task=%s unexpected error; releasing task",
                         worker_id, task_id)
        repository.release_task(task_id, worker_id)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="TaskKanban worker")
    parser.add_argument("--worker-id", default="worker-local", help="worker 标识")
    parser.add_argument("--interval", type=float, default=1.0, help="空轮询间隔秒")
    parser.add_argument(
        "--claim-lease-seconds",
        type=float,
        default=300.0,
        help="claimed 任务超过该秒数未进入 running 则允许回收（默认 300）",
    )
    parser.add_argument(
        "--recover-claimed-interval",
        type=float,
        default=30.0,
        help="worker 每次循环前执行超时 claimed 回收的间隔控制；<=0 表示关闭",
    )
    args = parser.parse_args()

    logger.info("worker=%s started", args.worker_id)
    while True:
        try:
            # 定期回收超时未进入 running 的 claimed 任务，避免 worker 崩溃后任务卡死。
            if args.recover_claimed_interval > 0:
                recovered = recover_expired_claims(args.claim_lease_seconds)
                if recovered:
                    logger.info("worker=%s recovered %d expired claimed task(s)",
                                args.worker_id, recovered)

            handled = run_worker_once(args.worker_id)
            if not handled:
                time.sleep(args.interval)
        except KeyboardInterrupt:
            logger.info("worker=%s stopped", args.worker_id)
            break
        except Exception:
            logger.exception("worker=%s unexpected error", args.worker_id)
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
