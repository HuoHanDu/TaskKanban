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
from app.repository import recover_expired_claims, release_task

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
        for step in task["steps"]:
            if not repository.mark_step_status(
                task["id"],
                step["step_index"],
                "running",
                worker_id=worker_id,
                started=True,
            ):
                logger.warning(
                    "worker=%s no longer owns task=%s at step=%s, abort execution",
                    worker_id,
                    task["id"],
                    step["step_index"],
                )
                return True

            current = apply_step(current, step["override"])
            result = execute_step(step, current)

            # 写日志（幂等，重复上报不会覆盖）
            repository.write_step_log(
                task["id"],
                step["step_index"],
                "success" if result.success else "failure",
                result.message,
            )

            if result.success:
                repository.mark_step_status(
                    task["id"],
                    step["step_index"],
                    "done",
                    worker_id=worker_id,
                    finished=True,
                )
            else:
                repository.mark_step_status(
                    task["id"],
                    step["step_index"],
                    "failed",
                    worker_id=worker_id,
                    finished=True,
                )
                repository.mark_task_failed(task["id"], worker_id)
                logger.info("worker=%s task=%s failed at step=%s",
                            worker_id, task["id"], step["step_index"])
                return True

        repository.mark_task_done(task["id"], worker_id)
        logger.info("worker=%s task=%s done", worker_id, task["id"])
        return True
    except Exception:
        # 执行中出现未预期异常：主动释放任务，便于其他 worker 重新认领。
        # 已经写入的成功日志不会被覆盖（幂等）。
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
