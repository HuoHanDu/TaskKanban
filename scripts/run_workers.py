"""一次拉起多个 worker 进程的启动/管理脚本。

用法示例：
    # 启动 3 个 worker（前缀默认 local）
    python scripts/run_workers.py --workers 3

    # 自定义前缀与参数
    python scripts/run_workers.py --workers 5 --prefix server \
        --interval 0.2 --hide-params --claim-lease-seconds 60

    # 后台运行并写日志
    python scripts/run_workers.py --workers 4 --prefix server \
        --log-dir /tmp/taskkanban-worker-logs

设计说明（真实并发原理）：
- 每个 worker 都是操作系统级独立进程（multiprocessing.Process）；
- 每个进程内部创建自己独立的 MySQL 连接；
- 进程之间不共享内存、不共享连接、没有全局锁；
- 它们同时调用 repository.claim_next_task() 竞争数据库行锁；
- 因此这是真实的并发，不是线程/协程/async 的伪并发。

Windows/Linux 差异：
- Python multiprocessing 在 Windows 使用 spawn，会重新导入模块；
  因此被启动的目标必须可被 import，且不能把入口逻辑直接写在模块顶层。
  本脚本把 worker 主体放入 app.worker.main_loop_forever()，避免 spawn 问题。
- Linux 默认 fork，代价更小；两种方式都产生独立进程。
"""

from __future__ import annotations

import argparse
import logging
import multiprocessing as mp
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.worker import main_loop_forever  # noqa: E402


def _worker_process_entry(
    worker_id: str,
    interval: float,
    show_params: bool,
    claim_lease_seconds: float,
    recover_claimed_interval: float,
    fail_rate: float,
    log_path: str | None,
) -> None:
    """子进程入口：包装 worker 的主循环，方便 multiprocessing spawn。"""
    if log_path:
        root = logging.getLogger()
        root.handlers.clear()
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(process)d] %(levelname)s %(message)s"
            )
        )
        root.addHandler(handler)
        root.setLevel(logging.INFO)

    main_loop_forever(
        worker_id=worker_id,
        interval=interval,
        show_params=show_params,
        claim_lease_seconds=claim_lease_seconds,
        recover_claimed_interval=recover_claimed_interval,
        fail_rate=fail_rate,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="启动多个 worker 进程")
    parser.add_argument("--workers", type=int, default=3, help="worker 数量（默认 3）")
    parser.add_argument("--prefix", default="local", help="worker-id 前缀，如 server/local")
    parser.add_argument("--interval", type=float, default=1.0, help="空轮询间隔秒")
    parser.add_argument(
        "--claim-lease-seconds",
        type=float,
        default=300.0,
        help="claimed 租约秒数",
    )
    parser.add_argument(
        "--recover-claimed-interval",
        type=float,
        default=30.0,
        help="超时 claimed 回收真实间隔秒；启动时先回收一次；<=0 关闭",
    )
    parser.add_argument(
        "--fail-rate",
        type=float,
        default=0.0,
        help="模拟 Step 失败概率（测试/演示用），默认 0 不失败",
    )
    parser.add_argument(
        "--show-params",
        action="store_true",
        default=True,
        help="打印 Step 参数（默认开启）",
    )
    parser.add_argument(
        "--hide-params",
        action="store_true",
        help="关闭 Step 参数打印",
    )
    parser.add_argument(
        "--log-dir",
        default=None,
        help="若提供，每个 worker 日志写入 <log-dir>/<prefix>-worker-N.log",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="前台等待所有 worker 结束（Ctrl+C 时统一终止）",
    )
    args = parser.parse_args()

    if args.workers <= 0:
        parser.error("--workers must be positive")

    show_params = args.show_params and not args.hide_params
    ctx = mp.get_context("spawn")

    if args.log_dir:
        Path(args.log_dir).mkdir(parents=True, exist_ok=True)

    processes = []
    for i in range(1, args.workers + 1):
        worker_id = f"{args.prefix}-worker-{i}"
        log_path = None
        if args.log_dir:
            log_path = str(Path(args.log_dir) / f"{worker_id}.log")

        p = ctx.Process(
            target=_worker_process_entry,
            args=(
                worker_id,
                args.interval,
                show_params,
                args.claim_lease_seconds,
                args.recover_claimed_interval,
                args.fail_rate,
                log_path,
            ),
        )
        p.start()
        processes.append(p)
        print(f"started {worker_id} pid={p.pid}", flush=True)

    print(f"\n{args.workers} worker(s) started. Ctrl+C to stop all.\n", flush=True)

    def _stop_all(signum=None, frame=None):
        print("\nstopping workers...", flush=True)
        for p in processes:
            if p.is_alive():
                p.terminate()
        for p in processes:
            p.join(timeout=5)
        print("all workers stopped", flush=True)
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop_all)
    signal.signal(signal.SIGTERM, _stop_all)

    if args.wait:
        try:
            while True:
                alive = [p for p in processes if p.is_alive()]
                if not alive:
                    print("all workers exited", flush=True)
                    break
                time.sleep(1)
        except KeyboardInterrupt:
            _stop_all()
        return

    # 非 wait 模式：保持父进程存活以统一管理子进程
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        _stop_all()


if __name__ == "__main__":
    main()
