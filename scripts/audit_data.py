"""TaskKanban 数据体检/异常筛选脚本。

功能：
- 扫描 tasks / steps / step_logs，找出常见异常；
- 默认只打印统计和问题清单，不修改数据；
- 支持 --status、--limit、--stale-seconds 等筛选。

异常规则：
1. active_tasks：仍处于 pending/claimed/running 的任务；
2. stale_claimed：claimed 超过阈值仍未进入 running，疑似孤儿；
3. done_with_non_done_steps：任务 done 但存在未 done 的 Step；
4. done_without_logs / done_missing_logs：任务 done 但缺少 Step 日志；
5. failed_without_failure_log：任务 failed 但没有任何 failure 日志；
6. step_failed_with_success_log：Step 为 failed 但日志却是 success（矛盾）；
7. task_failed_but_no_failed_step：任务 failed 但没有任何 failed Step。

用法：
    # 统计全部
    python scripts/audit_data.py

    # 只看异常任务明细，最多 100 条
    python scripts/audit_data.py --limit 100

    # 只看 claimed/running/pending
    python scripts/audit_data.py --status active

    # 只看 done 状态
    python scripts/audit_data.py --status done

    # 将 claimed 超过 30 秒视为孤儿
    python scripts/audit_data.py --stale-seconds 30

    # 服务器虚拟环境
    .venv/bin/python scripts/audit_data.py --limit 200
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import create_connection  # noqa: E402


def _fetch_all(sql: str, params: tuple = ()) -> list[dict]:
    conn = create_connection()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(sql, params)
        return list(cur.fetchall())
    finally:
        conn.close()


def _summarize(rows: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        out[r["status"]] = out.get(r["status"], 0) + 1
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="TaskKanban 数据体检")
    parser.add_argument("--limit", type=int, default=50, help="每类异常最多显示条数（默认 50）")
    parser.add_argument(
        "--status",
        choices=["active", "pending", "claimed", "running", "done", "failed"],
        default=None,
        help="只检查某类状态",
    )
    parser.add_argument(
        "--stale-seconds",
        type=int,
        default=300,
        help="claimed 超过该秒数视为孤儿（默认 300）",
    )
    args = parser.parse_args()

    # 基础统计
    status_rows = _fetch_all(
        "SELECT status, COUNT(*) AS cnt FROM tasks GROUP BY status"
    )
    total = sum(r["cnt"] for r in status_rows)
    print(f"任务总数: {total}")
    if status_rows:
        for r in sorted(status_rows, key=lambda x: x["status"]):
            print(f"  {r['status']}: {r['cnt']}")
    else:
        print("  (空)")

    if total == 0:
        return

    problem_counts: dict[str, int] = {}
    problem_samples: dict[str, list[str]] = {}

    def _add(name: str, rows: list[dict]) -> None:
        problem_counts[name] = len(rows)
        problem_samples[name] = [str(r["id"]) for r in rows[: args.limit]]

    def _status_clause(column: str = "t.status") -> tuple[str, list]:
        if args.status in {"pending", "claimed", "running", "done", "failed"}:
            return f" AND {column} = %s", [args.status]
        if args.status == "active":
            return f" AND {column} IN ('pending','claimed','running')", []
        return "", []

    # 1. active tasks
    if args.status in {None, "active", "pending", "claimed", "running"}:
        clause, params = _status_clause()
        rows = _fetch_all(
            f"""
            SELECT t.id, t.status, t.claimed_by, t.claimed_at, t.created_at
            FROM tasks t
            WHERE 1=1 {clause}
            ORDER BY t.id
            """,
            tuple(params),
        )
        _add("active_tasks", rows)

    # 2. stale claimed
    if args.status in {None, "active", "claimed"}:
        rows = _fetch_all(
            """
            SELECT id, claimed_by, claimed_at, created_at
            FROM tasks
            WHERE status = 'claimed'
              AND claimed_at IS NOT NULL
              AND claimed_at < NOW() - INTERVAL %s SECOND
            ORDER BY id
            """,
            (args.stale_seconds,),
        )
        _add("stale_claimed", rows)

    # 3. done with non-done steps
    if args.status in {None, "done"}:
        rows = _fetch_all(
            """
            SELECT DISTINCT t.id
            FROM tasks t
            JOIN steps s ON s.task_id = t.id
            WHERE t.status = 'done'
              AND s.status <> 'done'
            ORDER BY t.id
            """
        )
        _add("done_with_non_done_steps", rows)

    # 4. done but no logs at all
    if args.status in {None, "done"}:
        rows = _fetch_all(
            """
            SELECT t.id
            FROM tasks t
            LEFT JOIN step_logs l ON l.task_id = t.id
            WHERE t.status = 'done'
            GROUP BY t.id
            HAVING COUNT(l.id) = 0
            ORDER BY t.id
            """
        )
        _add("done_without_logs", rows)

    # 5. done but some steps lack logs
    if args.status in {None, "done"}:
        rows = _fetch_all(
            """
            SELECT t.id
            FROM tasks t
            JOIN steps s ON s.task_id = t.id
            LEFT JOIN step_logs l
              ON l.task_id = t.id AND l.step_index = s.step_index
            WHERE t.status = 'done'
            GROUP BY t.id
            HAVING COUNT(s.id) <> COUNT(l.id)
            ORDER BY t.id
            """
        )
        _add("done_missing_logs", rows)

    # 6. failed without any failure log
    if args.status in {None, "failed"}:
        rows = _fetch_all(
            """
            SELECT t.id
            FROM tasks t
            LEFT JOIN step_logs l
              ON l.task_id = t.id AND l.status = 'failure'
            WHERE t.status = 'failed'
            GROUP BY t.id
            HAVING COUNT(l.id) = 0
            ORDER BY t.id
            """
        )
        _add("failed_without_failure_log", rows)

    # 7. step failed but log success
    if args.status in {None, "done", "failed"}:
        rows = _fetch_all(
            """
            SELECT s.task_id AS id
            FROM steps s
            JOIN step_logs l
              ON l.task_id = s.task_id AND l.step_index = s.step_index
            WHERE s.status = 'failed'
              AND l.status = 'success'
            ORDER BY s.task_id
            """
        )
        _add("step_failed_with_success_log", rows)

    # 8. task failed but no failed step
    if args.status in {None, "failed"}:
        rows = _fetch_all(
            """
            SELECT t.id
            FROM tasks t
            LEFT JOIN steps s
              ON s.task_id = t.id AND s.status = 'failed'
            WHERE t.status = 'failed'
            GROUP BY t.id
            HAVING COUNT(s.id) = 0
            ORDER BY t.id
            """
        )
        _add("task_failed_but_no_failed_step", rows)

    # 汇总
    print("\n=== 异常汇总 ===")
    total_problems = 0
    for name, count in sorted(problem_counts.items(), key=lambda x: -x[1]):
        if count == 0:
            continue
        total_problems += count
        print(f"{name}: {count}")
        samples = problem_samples[name]
        if samples:
            print(f"  示例 id: {', '.join(samples)}")
    if total_problems == 0:
        print("未发现异常")
    else:
        print(f"\n共发现 {total_problems} 条异常（可能同一任务命中多个规则）")

    # 提示
    print("\n提示：")
    print("  1. 卡住的 claimed 可先释放：")
    print("     mysql ... -e \"UPDATE tasks SET status='pending', claimed_by=NULL, claimed_at=NULL WHERE status='claimed';\"")
    print("  2. 删除历史数据：python scripts/cleanup_data.py --delete")


if __name__ == "__main__":
    main()
