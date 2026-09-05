"""数据库访问层：封装 tasks / steps / step_logs 的读写。

设计约定：
- 所有函数默认每次创建一个新连接，用后关闭。
- 事务型操作（创建任务、认领任务、写日志）内部显式 commit / rollback。
- JSON 字段在 Python 侧使用 dict/list，入库时序列化，出库时反序列化。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional, Sequence

from mysql.connector.abstracts import MySQLConnectionAbstract

from app.db import create_connection


# ---------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------

def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _json_loads(value: Any) -> Any:
    """MySQL JSON 列可能以 str/bytes/dict 形式返回，统一转成 Python 对象。"""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    return json.loads(value)


def _task_summary(row: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "status": row["status"],
        "claimed_by": row["claimed_by"],
        "claimed_at": row["claimed_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _task_detail(row: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        **_task_summary(row),
        "base_params": _json_loads(row["base_params"]),
        "group_override": _json_loads(row["group_override"]),
    }


def _step_dict(row: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "step_index": row["step_index"],
        "override": _json_loads(row["override"]),
        "action": row.get("action", "mock"),
        "status": row["status"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }


# ---------------------------------------------------------------
# 创建任务
# ---------------------------------------------------------------

def create_task(
    base_params: Mapping[str, Any],
    group_override: Mapping[str, Any],
    steps: Sequence[Mapping[str, Any]],
) -> int:
    """在一个事务中创建 Task 及其 Steps，返回新任务 id。

    steps 元素格式：
      {"step_index": 1, "override": {...}, "action": "mock", ...}
    其中 override 必填，action 可选（默认 'mock'）。
    """
    conn: MySQLConnectionAbstract = create_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO tasks (status, base_params, group_override)
            VALUES ('pending', %s, %s)
            """,
            (_json_dumps(dict(base_params)), _json_dumps(dict(group_override))),
        )
        task_id = cursor.lastrowid

        for step in steps:
            cursor.execute(
                """
                INSERT INTO steps (task_id, step_index, override, action)
                VALUES (%s, %s, %s, %s)
                """,
                (
                    task_id,
                    int(step["step_index"]),
                    _json_dumps(dict(step.get("override", {}))),
                    str(step.get("action", "mock")),
                ),
            )

        conn.commit()
        return int(task_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------
# 查询
# ---------------------------------------------------------------

def list_tasks() -> List[Dict[str, Any]]:
    """返回任务摘要列表（不含 params，避免看板数据过大）。"""
    conn = create_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT id, status, claimed_by, claimed_at, started_at,
                   finished_at, created_at, updated_at
            FROM tasks
            ORDER BY created_at DESC, id DESC
            """
        )
        return [_task_summary(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_task_with_steps(task_id: int) -> Optional[Dict[str, Any]]:
    """返回单个 Task 完整信息，含按 step_index 排序的 steps。"""
    conn = create_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT id, status, base_params, group_override, claimed_by,
                   claimed_at, started_at, finished_at, created_at, updated_at
            FROM tasks
            WHERE id = %s
            """,
            (task_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None

        cursor.execute(
            """
            SELECT id, task_id, step_index, override, action, status,
                   started_at, finished_at
            FROM steps
            WHERE task_id = %s
            ORDER BY step_index
            """,
            (task_id,),
        )
        steps = [_step_dict(r) for r in cursor.fetchall()]
        return {**_task_detail(row), "steps": steps}
    finally:
        conn.close()


# ---------------------------------------------------------------
# 认领任务（并发安全）
# ---------------------------------------------------------------

def claim_next_task(worker_id: str) -> Optional[Dict[str, Any]]:
    """原子认领一个 pending 任务，返回完整任务（含 steps）。

    实现：事务内 SELECT ... FOR UPDATE SKIP LOCKED 锁定一行，
    再用 UPDATE 将其置为 claimed。行锁保证同一任务不会被两个
    worker 同时认领。
    """
    conn = create_connection()
    task_id: Optional[int] = None
    try:
        conn.start_transaction()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT id
            FROM tasks
            WHERE status = 'pending'
            ORDER BY created_at, id
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """
        )
        row = cursor.fetchone()
        if row is None:
            conn.rollback()
            return None

        task_id = int(row["id"])
        cursor.execute(
            """
            UPDATE tasks
            SET status = 'claimed', claimed_by = %s, claimed_at = NOW()
            WHERE id = %s AND status = 'pending'
            """,
            (worker_id, task_id),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return None

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # 认领成功后另开连接读取完整详情
    return get_task_with_steps(task_id)


# ---------------------------------------------------------------
# 任务/步骤状态更新
# ---------------------------------------------------------------

def claim_by_id(task_id: int, worker_id: str) -> bool:
    """按指定任务 id 原子认领（API 手动演示用）。

    仅在任务仍为 pending 时成功，避免已认领/已完成任务被重复认领。
    """
    conn = create_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE tasks
            SET status = 'claimed', claimed_by = %s, claimed_at = NOW()
            WHERE id = %s AND status = 'pending'
            """,
            (worker_id, task_id),
        )
        success = cursor.rowcount == 1
        conn.commit()
        return success
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_task_by_id(task_id: int) -> None:
    """删除任务及其 steps / step_logs（外键级联删除）。测试清理用。"""
    conn = create_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM tasks WHERE id = %s", (task_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_step_logs(task_id: int) -> List[Dict[str, Any]]:
    """返回某任务的全部步骤日志，按 step_index 升序。"""
    conn = create_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT id, task_id, step_index, status, message, created_at, updated_at
            FROM step_logs
            WHERE task_id = %s
            ORDER BY step_index
            """,
            (task_id,),
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def mark_task_running(task_id: int) -> None:
    conn = create_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE tasks
            SET status = 'running', started_at = COALESCE(started_at, NOW())
            WHERE id = %s
            """,
            (task_id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_task_done(task_id: int) -> None:
    conn = create_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE tasks
            SET status = 'done', finished_at = NOW()
            WHERE id = %s
            """,
            (task_id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_task_failed(task_id: int) -> None:
    conn = create_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE tasks
            SET status = 'failed', finished_at = NOW()
            WHERE id = %s
            """,
            (task_id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_step_status(
    task_id: int,
    step_index: int,
    status: str,
    *,
    started: bool = False,
    finished: bool = False,
) -> None:
    """更新单个 Step 状态。started/finished 控制是否写入时间戳。"""
    conn = create_connection()
    try:
        cursor = conn.cursor()
        assignments = ["status = %s"]
        params: list[Any] = [status]

        if started:
            assignments.append("started_at = COALESCE(started_at, NOW())")
        if finished:
            assignments.append("finished_at = NOW()")

        params.extend([task_id, step_index])
        cursor.execute(
            f"""
            UPDATE steps
            SET {", ".join(assignments)}
            WHERE task_id = %s AND step_index = %s
            """,
            tuple(params),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------
# 幂等日志写入
# ---------------------------------------------------------------

def write_step_log(
    task_id: int,
    step_index: int,
    status: str,
    message: Optional[str] = None,
) -> bool:
    """幂等写入一条 Step 执行日志。

    依赖 step_logs 表的 UNIQUE(task_id, step_index)：
    - 第一次写入成功返回 True。
    - 重复写入被 INSERT IGNORE 忽略返回 False，且不会覆盖已有记录。
    """
    conn = create_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT IGNORE INTO step_logs (task_id, step_index, status, message)
            VALUES (%s, %s, %s, %s)
            """,
            (task_id, step_index, status, message),
        )
        inserted = cursor.rowcount == 1
        conn.commit()
        return inserted
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
