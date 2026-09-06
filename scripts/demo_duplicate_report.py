"""幂等日志演示：主动模拟同一个 Step 被“报告完成两次以上”。

题目场景：同一个 Step 可能因为 Worker 重启/网络重试等原因被报告两次以上。
本脚本主动模拟并打印：
1. 同一 Step 连续重复上报 5 次 success → 只有第一次写日志；
2. 同一 Step 先 success 后 failure → 后到 failure 被忽略，成功不被覆盖；
3. 同一 Step 并发 5 次上报（真实多线程+独立 DB 连接）→ 只有 1 条日志；
4. 输出 DB step_logs 最终结果作为证据。

用法：
    python scripts/demo_duplicate_report.py
    python scripts/demo_duplicate_report.py --worker-id demo-report

需要 MySQL 已初始化且 .env 配置正确；脚本会自动创建并清理演示任务。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import sys
from pathlib import Path

# Windows GBK 控制台直接打印中文会乱码，统一走 UTF-8 输出。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import repository  # noqa: E402


def _prepare_task() -> int:
    """创建一个 2-Step 任务并让它进入 running，便于模拟重复上报。"""
    tid = repository.create_task(
        base_params={"template_id": "T001", "channel": "email"},
        group_override={"channel": "sms", "priority": "high"},
        steps=[
            {"step_index": 1, "override": {"action": "mock"}, "action": "mock"},
            {"step_index": 2, "override": {"action": "mock"}, "action": "mock"},
        ],
    )
    assert repository.claim_by_id(tid, "demo-report") is True
    assert repository.mark_task_running(tid, "demo-report") is True
    return tid


def _show_logs(tid: int, title: str) -> None:
    print(f"\n--- {title} ---")
    logs = repository.list_step_logs(tid)
    if not logs:
        print("  (无日志)")
    for log in logs:
        print(f"  Step{log['step_index']} [{log['status']}] {log['message']}")


def _demo_sequential_success(tid: int, worker: str) -> None:
    print("\n" + "=" * 78)
    print("演示 1：同一 Step 连续重复上报 5 次 success")
    print("=" * 78)
    for i in range(1, 6):
        out = repository.report_step_execution(
            tid, 1, "success", worker, message=f"第 {i} 次上报"
        )
        print(f"  第 {i} 次: inserted={out['log_inserted']} "
              f"step={out['step_status']} task={out['task_status']}")
    _show_logs(tid, "Step1 连续 5 次 success 后 DB 日志")


def _demo_success_then_failure(tid: int, worker: str) -> None:
    print("\n" + "=" * 78)
    print("演示 2：Step1 先 success，后到 failure 不覆盖已有成功")
    print("=" * 78)
    # 演示 1 已经让 Step1 成功；此时 Step2 还是 pending，任务仍 running，
    # 正好可以验证“后到失败不能把已成功的 Step 改成 failed”。
    first = repository.report_step_execution(
        tid, 1, "success", worker, message="第一次成功"
    )
    print(f"  再次 success:   inserted={first['log_inserted']}（重复，不覆盖）")
    late = repository.report_step_execution(
        tid, 1, "failure", worker, message="后到的失败"
    )
    print(f"  后到 failure:   inserted={late['log_inserted']}, "
          f"ignored={late['ignored_duplicate']}, task={late['task_status']}")
    _show_logs(tid, "先 success 后 failure 后 DB 日志")


def _demo_concurrent(tid: int, worker: str) -> None:
    print("\n" + "=" * 78)
    print("演示 3：同一 Step 并发 5 次上报（线程池 + 每个调用独立 DB 连接）")
    print("=" * 78)

    # 并发演示用独立任务：必须让 Step1 不是“最后一个 Step”。
    # 如果只有一个 Step，第一个成功上报会把任务置 done，
    # 后续并发上报会因任务已终态被拒绝（这是状态机保护，不是幂等写入问题）。
    # 这里创建 2-Step 任务并并发上报 Step1：Step1 成功后任务仍 running，
    # 其余 4 个请求走正常幂等路径，能清晰看到 inserted=True/False。
    con_tid = repository.create_task(
        base_params={},
        group_override={},
        steps=[
            {"step_index": 1, "override": {}, "action": "mock"},
            {"step_index": 2, "override": {}, "action": "mock"},
        ],
    )
    try:
        assert repository.claim_by_id(con_tid, worker) is True
        assert repository.mark_task_running(con_tid, worker) is True

        def report(i: int):
            return repository.report_step_execution(
                con_tid, 1, "success", worker, message=f"并发第 {i} 次"
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(report, range(1, 6)))

        inserted_count = sum(1 for r in results if r["log_inserted"])
        for i, r in enumerate(results, start=1):
            print(f"  并发第 {i} 次: inserted={r['log_inserted']}")
        print(f"\n  并发结果：inserted 总次数 = {inserted_count}（应为 1）")
        _show_logs(con_tid, "并发 5 次上报后 DB 日志")
        assert inserted_count == 1, "并发幂等演示失败：插入了多条日志"
    finally:
        repository.delete_task_by_id(con_tid)


def main() -> None:
    parser = argparse.ArgumentParser(description="重复上报幂等演示")
    parser.add_argument("--worker-id", default="demo-report", help="worker 标识")
    args = parser.parse_args()
    worker = args.worker_id

    tid = _prepare_task()
    try:
        print(f"已创建演示任务 id={tid}，状态 running，worker={worker}")

        # 演示 1：Step1 重复 success
        _demo_sequential_success(tid, worker)

        # Step1 已 done，任务还剩 Step2 pending/running，继续演示 2
        _demo_success_then_failure(tid, worker)

        # 演示 3：并发独立任务
        _demo_concurrent(tid, worker)

        print("\n" + "=" * 78)
        print("幂等日志演示完成")
        print("结论：无论重复上报多少次，每个 (task_id, step_index) 只有一条日志；")
        print("      后到的 failure 不会覆盖已有的 success。")
        print("=" * 78)
    finally:
        repository.delete_task_by_id(tid)
        print("已清理演示任务。")


if __name__ == "__main__":
    main()
