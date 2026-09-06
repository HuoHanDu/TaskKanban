"""Worker 进程：循环认领任务并执行全部步骤。

启动方式：
    python -m app.worker --worker-id worker-1
"""

from __future__ import annotations

import argparse
import logging
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence

from app.executor import execute_step
from app.params import apply_step, apply_group
from app import repository
from app.repository import complete_task_atomically, recover_expired_claims, release_task

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(process)d] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def validate_fail_rate(fail_rate: float) -> float:
    """校验 fail_rate 必须在 [0, 1]，非法时抛出 ValueError。"""
    value = float(fail_rate)
    if not (0.0 <= value <= 1.0):
        raise ValueError(f"fail_rate must be between 0.0 and 1.0, got {fail_rate!r}")
    return value


def _sort_steps(steps: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """按 step_index 升序返回步骤列表（防御 SQL/内存顺序变化）。

    参数模块已保证 step_index 合法且任务内唯一；这里不再重复完整校验，
    只做防御性排序。若数据确实非法，后续 params/executor/repository 会报错。
    """
    return sorted((dict(step) for step in steps), key=lambda s: int(s["step_index"]))


def _run_claimed_task(
    task: Mapping[str, Any],
    worker_id: str,
    *,
    show_params: bool = True,
    fail_rate: float = 0.0,
    sleep_seconds: float = 0.1,
    lease_seconds: float = 30.0,
) -> bool:
    """执行一个已由 worker_id 推进到 running 的任务。

    步骤按 step_index 升序执行；参数在步骤间保持粘性。
    每个 Step 执行前续约 running 租约；任一步失败后立即停止后续 Step 执行，
    并把尚未执行的 Step 按失败结果补齐，最后通过 complete_task_atomically
    提交任务全部 Step，保证原子终态。
    """
    task_id = task["id"]
    try:
        steps = _sort_steps(task["steps"])
        current = apply_group(task["base_params"], task["group_override"])
        results: List[Dict[str, Any]] = []
        failed_step: Optional[int] = None

        for step in steps:
            # 执行 Step 前先续约；只有当前 running 持有者能续约成功。
            repository.renew_running_lease(
                task_id, worker_id, lease_seconds=lease_seconds
            )
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
            result = execute_step(
                step,
                current,
                fail_rate=fail_rate,
                sleep_seconds=sleep_seconds,
            )
            results.append(
                {
                    "step_index": step["step_index"],
                    "success": result.success,
                    "message": result.message,
                }
            )
            if not result.success:
                failed_step = int(step["step_index"])
                break

        # 失败即停止：为了让 complete_task_atomically 仍能原子提交“全部 Step”，
        # 把后续未执行的 Step 作为失败结果补齐（status=failed，message 标明未执行）。
        if failed_step is not None:
            for step in steps:
                if int(step["step_index"]) > failed_step:
                    results.append(
                        {
                            "step_index": step["step_index"],
                            "success": False,
                            "message": (
                                f"not executed because step {failed_step} failed"
                            ),
                        }
                    )

        # 所有 Step 结果（成功路径全部结果，失败路径含补齐结果）在单个事务内
        # 原子提交：日志 + Step 状态 + 任务终态要么全部成功，要么全部回滚。
        outcome = complete_task_atomically(
            task["id"],
            worker_id,
            results=results,
        )
        if outcome["task_status"] == "failed":
            logger.info(
                "worker=%s task=%s failed at step=%s",
                worker_id,
                task["id"],
                failed_step,
            )
        elif outcome["task_status"] == "done":
            logger.info("worker=%s task=%s done", worker_id, task["id"])
        else:
            logger.info(
                "worker=%s task=%s no state change; status=%s ignored_failures=%s",
                worker_id,
                task["id"],
                outcome["task_status"],
                outcome["ignored_failures"],
            )
        return True
    except Exception:
        # 执行中出现未预期异常：主动释放任务，便于其他 worker 重新认领。
        # 若 complete_task_atomically 已部分提交，由于它整体回滚，不会留下中间态。
        logger.exception("worker=%s task=%s unexpected error; releasing task",
                         worker_id, task_id)
        repository.release_task(task_id, worker_id)
        raise


def run_worker_once(
    worker_id: str,
    *,
    show_params: bool = True,
    fail_rate: float = 0.0,
    sleep_seconds: float = 0.1,
    lease_seconds: float = 30.0,
) -> bool:
    """认领并执行一个任务；没有可执行任务时返回 False。

    show_params=True 时每个 Step 执行前打印该 Step 看到的参数快照，
    便于现场展示 L1/L2/L3 粘性合并过程。
    fail_rate 用于测试/演示注入 Step 失败概率，默认 0 表示 Worker 从不失败。
    lease_seconds 是 running 租约秒数，执行期间每个 Step 前续约。
    """
    task = repository.claim_next_task(worker_id)
    if task is None:
        return False

    task_id = task["id"]
    logger.info("worker=%s claimed task=%s", worker_id, task_id)

    # 只有真正从 claimed 推进到 running 成功，才继续执行。
    # 防止 worker 认领后任务已被其他调用方释放/回收/修改。
    # 若推进失败（死锁/失去持有权），主动释放任务回 pending，避免孤儿 claimed。
    try:
        running_ok = repository.mark_task_running(
            task_id, worker_id, lease_seconds=lease_seconds
        )
    except Exception:
        logger.exception("worker=%s failed to mark task=%s running; releasing task",
                         worker_id, task_id)
        repository.release_task(task_id, worker_id)
        return True

    if not running_ok:
        logger.warning("worker=%s lost ownership of task=%s before running; releasing",
                       worker_id, task_id)
        repository.release_task(task_id, worker_id)
        return True

    return _run_claimed_task(
        task,
        worker_id,
        show_params=show_params,
        fail_rate=fail_rate,
        sleep_seconds=sleep_seconds,
        lease_seconds=lease_seconds,
    )


def main_loop_forever(
    *,
    worker_id: str,
    interval: float = 1.0,
    show_params: bool = True,
    claim_lease_seconds: float = 300.0,
    recover_claimed_interval: float = 30.0,
    fail_rate: float = 0.0,
    lease_seconds: float = 30.0,
) -> None:
    """Worker 主循环：可被命令行 main 或 scripts/run_workers.py 复用。

    recover_claimed_interval 是真正的回收间隔（秒）：
    - 启动时先执行一次回收（若开关开启）；
    - 之后距离上次回收 elapsed >= recover_claimed_interval 才再次调用
      recover_expired_claims；
    - <=0 表示完全关闭回收。
    lease_seconds 是 running 租约秒数，Worker 每个 Step 前续约。
    每个调用方应处于独立进程；不要在多个线程/协程中共享同一个 worker 身份。
    """
    logger.info("worker=%s started", worker_id)
    last_recover_time: Optional[float] = None

    while True:
        try:
            # 间隔控制回收：<=0 关闭；启动时允许先回收一次。
            if recover_claimed_interval > 0:
                now = time.monotonic()
                if last_recover_time is None or (
                    now - last_recover_time >= recover_claimed_interval
                ):
                    logger.debug("worker=%s recover claimed check (interval=%s)",
                                 worker_id, recover_claimed_interval)
                    recovered = recover_expired_claims(claim_lease_seconds)
                    if recovered:
                        logger.info("worker=%s recovered %d expired claimed task(s)",
                                    worker_id, recovered)
                    last_recover_time = time.monotonic()

            handled = run_worker_once(
                worker_id,
                show_params=show_params,
                fail_rate=fail_rate,
                sleep_seconds=0.1,
                lease_seconds=lease_seconds,
            )
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
        help="超时 claimed 回收的真实间隔秒；<=0 表示关闭；启动时若开启先回收一次",
    )
    parser.add_argument(
        "--fail-rate",
        type=float,
        default=0.0,
        help="模拟 Step 失败概率（测试/演示用），默认 0 表示 Worker 不失败",
    )
    parser.add_argument(
        "--lease-seconds",
        type=float,
        default=30.0,
        help="running 租约秒数；Worker 每个 Step 前续约（默认 30）",
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

    try:
        fail_rate = validate_fail_rate(args.fail_rate)
    except ValueError as exc:
        parser.error(str(exc))

    main_loop_forever(
        worker_id=args.worker_id,
        interval=args.interval,
        show_params=show_params,
        claim_lease_seconds=args.claim_lease_seconds,
        recover_claimed_interval=args.recover_claimed_interval,
        fail_rate=fail_rate,
        lease_seconds=args.lease_seconds,
    )


if __name__ == "__main__":
    main()
