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


def run_worker_once(worker_id: str, *, show_params: bool = True) -> bool:
    """认领并执行一个任务；没有可执行任务时返回 False。

    show_params=True 时每个 Step 执行前打印该 Step 看到的参数快照，
    便于现场展示 L1/L2/L3 粘性合并过程。
    """
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
            if show_params:
                logger.info(
                    "worker=%s task=%s step=%s override=%s params=%s",
                    worker_id,
                    task_id,
                    step["step_index"],
                    step["override"],
                    current,
                )
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


def main_loop_forever(
    *,
    worker_id: str,
    interval: float = 1.0,
    show_params: bool = True,
    claim_lease_seconds: float = 300.0,
    recover_claimed_interval: float = 30.0,
) -> None:
    """Worker 主循环：可被命令行 main 或 scripts/run_workers.py 复用。

    每个调用方应处于独立进程；不要在多个线程/协程中共享同一个 worker 身份。
    """
    logger.info("worker=%s started", worker_id)
    while True:
        try:
            # 定期回收超时未进入 running 的 claimed 任务，避免 worker 崩溃后任务卡死。
            if recover_claimed_interval > 0:
                recovered = recover_expired_claims(claim_lease_seconds)
                if recovered:
                    logger.info("worker=%s recovered %d expired claimed task(s)",
                                worker_id, recovered)

            handled = run_worker_once(worker_id, show_params=show_params)
            if not handled:
                time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("worker=%s stopped", worker_id)
            break
        except Exception:
            logger.exception("worker=%s unexpected error", worker_id)
            time.sleep(interval)


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
    parser.add_argument(
        "--show-params",
        action="store_true",
        default=True,
        help="每个 Step 执行时打印该 Step 看到的参数快照（默认开启）",
    )
    parser.add_argument(
        "--hide-params",
        action="store_true",
        help="关闭 Step 参数打印，只保留任务级日志",
    )
    args = parser.parse_args()
    show_params = args.show_params and not args.hide_params

    main_loop_forever(
        worker_id=args.worker_id,
        interval=args.interval,
        show_params=show_params,
        claim_lease_seconds=args.claim_lease_seconds,
        recover_claimed_interval=args.recover_claimed_interval,
    )


if __name__ == "__main__":
    main()
