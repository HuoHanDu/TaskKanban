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

    logger.info("worker=%s claimed task=%s", worker_id, task["id"])
    repository.mark_task_running(task["id"])

    # 按顺序逐步执行，边执行边合并参数（粘性）
    current = apply_group(task["base_params"], task["group_override"])
    for step in task["steps"]:
        repository.mark_step_status(
            task["id"], step["step_index"], "running", started=True
        )

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
                task["id"], step["step_index"], "done", finished=True
            )
        else:
            repository.mark_step_status(
                task["id"], step["step_index"], "failed", finished=True
            )
            repository.mark_task_failed(task["id"])
            logger.info("worker=%s task=%s failed at step=%s",
                        worker_id, task["id"], step["step_index"])
            return True

    repository.mark_task_done(task["id"])
    logger.info("worker=%s task=%s done", worker_id, task["id"])
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="TaskKanban worker")
    parser.add_argument("--worker-id", default="worker-local", help="worker 标识")
    parser.add_argument("--interval", type=float, default=1.0, help="空轮询间隔秒")
    args = parser.parse_args()

    logger.info("worker=%s started", args.worker_id)
    while True:
        try:
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
