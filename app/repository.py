"""数据库访问层：封装 tasks / steps / step_logs 的读写。

设计约定：
- 所有函数默认每次创建一个新连接，用后关闭。
- 事务型操作（创建任务、认领任务、写日志）内部显式 commit / rollback。
- JSON 字段在 Python 侧使用 dict/list，入库时序列化，出库时反序列化。
- 所有“推进状态”的函数都必须使用条件更新，并返回 bool 表示是否真正修改；
  只有当前持有者才能推进任务状态。
- 多步骤的“日志 + 状态 + 任务终态”提供原子事务函数，避免执行中断后
  出现 Step 状态与 Task 状态不一致的中间态。
"""

from __future__ import annotations

import functools
import json
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence

from mysql.connector.abstracts import MySQLConnectionAbstract

from app.db import create_connection

# 任务合法状态集
TASK_STATUSES = {"pending", "claimed", "running", "done", "failed"}
# 步骤合法状态集
STEP_STATUSES = {"pending", "running", "done", "failed"}
# 步骤日志合法状态集
LOG_STATUSES = {"success", "failure"}

# 认领后允许推进到终态的状态：支持“claim 后直接 done/failed”的简化和
# “claim -> running -> done/failed”两种路径。
TASK_ACTIVE_STATUSES = {"claimed", "running"}

# MySQL 死锁错误码：遇到 1213/40001 时通常可重试
DEADLOCK_ERROR_CODES = {1213, 40001}
# 死锁/锁等待重试次数
MAX_LOCK_RETRIES = 3
# 死锁重试退避基数（秒）
DEADLOCK_RETRY_BASE_SLEEP = 0.05

# 单次原子事务最多处理的步骤数，防御异常输入导致的超大事务。
MAX_ATOMIC_STEPS = 1000


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


def _validate_status(status: str, allowed: set[str]) -> None:
    if status not in allowed:
        raise ValueError(
            f"invalid status {status!r}; expected one of {sorted(allowed)}"
        )


def _is_deadlock(exc: Exception) -> bool:
    """判断异常是否为 MySQL 死锁或锁等待超时。"""
    errno = getattr(exc, "errno", None)
    if errno is not None:
        return int(errno) in DEADLOCK_ERROR_CODES
    # 部分驱动包装在 args 中
    for arg in getattr(exc, "args", ()):
        if isinstance(arg, Exception):
            nested = getattr(arg, "errno", None)
            if nested is not None and int(nested) in DEADLOCK_ERROR_CODES:
                return True
        if isinstance(arg, (int, str)):
            text = str(arg)
            if "Deadlock" in text or "deadlock" in text:
                return True
    return False


def _retry_on_deadlock(func):
    """把“每个尝试新建连接 + 死锁重试 + 关闭连接”抽成通用装饰器。

    被装饰函数必须接收一个已创建的 conn 作为第一个参数，并在函数内部
    自行 commit/rollback；装饰器负责遇到 1213/40001 时重试整个函数。

    用法：
        @_retry_on_deadlock
        def _tx(conn, task_id, worker_id):
            cursor = conn.cursor()
            cursor.execute(...)
            success = cursor.rowcount == 1
            conn.commit()
            return success
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        last_exc: Exception | None = None
        for attempt in range(MAX_LOCK_RETRIES):
            conn = create_connection()
            try:
                return func(conn, *args, **kwargs)
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                last_exc = exc
                if _is_deadlock(exc) and attempt + 1 < MAX_LOCK_RETRIES:
                    time.sleep(DEADLOCK_RETRY_BASE_SLEEP * (attempt + 1))
                    continue
                raise
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        # 理论上不会到这一步；若重试耗尽则抛出最后一次异常
        assert last_exc is not None
        raise last_exc

    return wrapper


def _normalize_create_task_input(
    base_params: Any,
    group_override: Any,
    steps: Any,
) -> tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]:
    """校验 create_task 的输入并返回 (base, group, normalized_steps)。

    复用 app.params._normalize_steps 的规则：
    - base/group 必须是 Mapping；
    - steps 必须是非空 Sequence，元素必须是 Mapping；
    - 每个 step 必须有合法 step_index（正整数、唯一）；
    - override 可选/None -> {}，否则必须是 Mapping；
    - action 必须可转换为非空字符串。
    """
    from app.params import _ensure_mapping, _normalize_steps

    base = dict(_ensure_mapping(base_params, "base_params"))
    group = dict(_ensure_mapping(group_override, "group_override"))
    if not isinstance(steps, Sequence) or isinstance(steps, (str, bytes)):
        raise TypeError(
            f"steps must be a non-empty sequence, got {type(steps).__name__}"
        )
    if len(steps) == 0:
        raise ValueError("task must contain at least one step")

    normalized_steps = _normalize_steps(steps)
    normalized: List[Dict[str, Any]] = []
    for step in normalized_steps:
        # 找到原 step 以保留 action 等额外字段
        source = None
        for raw in steps:
            if isinstance(raw, Mapping) and raw.get("step_index") == step["step_index"]:
                source = raw
                break
        action = source.get("action", "mock") if source is not None else "mock"
        if not isinstance(action, str) or not action.strip():
            raise ValueError(
                f"step_index {step['step_index']} action must be a non-empty string"
            )
        normalized.append({**step, "action": action.strip()})

    return base, group, normalized


def _load_task_for_update(conn: Any, task_id: int) -> Optional[Dict[str, Any]]:
    """在当前连接/事务内锁定并读取任务行；返回 None 表示任务不存在。"""
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT id, status, claimed_by, claimed_at, started_at, finished_at
        FROM tasks
        WHERE id = %s
        FOR UPDATE
        """,
        (task_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return dict(row)


def _require_owner(
    task_row: Dict[str, Any],
    worker_id: str,
    *,
    allow_states: Optional[set[str]] = None,
) -> None:
    """校验任务行由 worker_id 持有且处于允许的状态；不满足抛 RuntimeError。"""
    states = allow_states if allow_states is not None else TASK_ACTIVE_STATUSES
    if task_row["status"] not in states:
        raise RuntimeError(
            f"task {task_row['id']} status is {task_row['status']!r}, "
            f"expected one of {sorted(states)}"
        )
    if task_row["claimed_by"] != worker_id:
        raise RuntimeError(
            f"task {task_row['id']} is claimed by "
            f"{task_row['claimed_by']!r}, not {worker_id!r}"
        )


def _update_task_status(
    conn: Any,
    task_id: int,
    status: str,
    *,
    started: bool = False,
    finished: bool = False,
) -> None:
    """在当前连接/事务内更新任务状态（不提交，由调用方控制）。"""
    assignments = ["status = %s"]
    params: list[Any] = [status]
    if started:
        assignments.append("started_at = COALESCE(started_at, NOW())")
    if finished:
        assignments.append("finished_at = NOW()")
    params.append(task_id)
    cursor = conn.cursor()
    cursor.execute(
        f"UPDATE tasks SET {', '.join(assignments)} WHERE id = %s",
        tuple(params),
    )


def _update_step_status(
    conn: Any,
    task_id: int,
    step_index: int,
    status: str,
    *,
    started: bool = False,
    finished: bool = False,
) -> bool:
    """在当前连接/事务内更新单个 Step 状态（不提交）。

    返回 False 表示该 step 不存在（行锁已保证任务持有权在调用前校验）。
    """
    assignments = ["status = %s"]
    params: list[Any] = [status]
    if started:
        assignments.append("started_at = COALESCE(started_at, NOW())")
    if finished:
        assignments.append("finished_at = NOW()")
    params.extend([task_id, step_index])
    cursor = conn.cursor()
    cursor.execute(
        f"""
        UPDATE steps
        SET {', '.join(assignments)}
        WHERE task_id = %s AND step_index = %s
        """,
        tuple(params),
    )
    return cursor.rowcount == 1


def _insert_step_log(
    conn: Any,
    task_id: int,
    step_index: int,
    status: str,
    message: Optional[str],
) -> bool:
    """在当前连接/事务内幂等写入 Step 日志（不提交）。

    返回 True 表示首次插入，False 表示唯一键冲突被忽略。
    """
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT IGNORE INTO step_logs (task_id, step_index, status, message)
        VALUES (%s, %s, %s, %s)
        """,
        (task_id, step_index, status, message),
    )
    return cursor.rowcount == 1


def _step_has_success_log(conn: Any, task_id: int, step_index: int) -> bool:
    """在当前连接/事务内检查某 Step 是否已有成功日志。"""
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        """
        SELECT 1 FROM step_logs
        WHERE task_id = %s AND step_index = %s AND status = 'success'
        """,
        (task_id, step_index),
    )
    return cursor.fetchone() is not None


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

    入口校验：
    - base_params / group_override 必须是 Mapping；
    - steps 必须是非空 Sequence，不能创建空任务；
    - step_index 必须为正整数且任务内唯一；
    - override 必须是 Mapping（None/缺省视为 {}）；
    - action 必须是非空字符串（缺省为 "mock"）。
    """
    base, group, normalized_steps = _normalize_create_task_input(
        base_params, group_override, steps
    )

    conn: MySQLConnectionAbstract = create_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO tasks (status, base_params, group_override)
            VALUES ('pending', %s, %s)
            """,
            (_json_dumps(base), _json_dumps(group)),
        )
        task_id = cursor.lastrowid

        for step in normalized_steps:
            cursor.execute(
                """
                INSERT INTO steps (task_id, step_index, override, action)
                VALUES (%s, %s, %s, %s)
                """,
                (
                    task_id,
                    int(step["step_index"]),
                    _json_dumps(dict(step.get("override", {}))),
                    step["action"],
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

@_retry_on_deadlock
def _claim_next_task_tx(conn: Any, worker_id: str) -> Optional[int]:
    """在单个事务内原子认领一个 pending 任务并返回 task_id（不包含读取详情）。

    若没有可认领任务返回 None。死锁由 _retry_on_deadlock 自动重试。
    """
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
    return task_id


def claim_next_task(worker_id: str) -> Optional[Dict[str, Any]]:
    """原子认领一个 pending 任务，返回完整任务（含 steps）。

    实现：事务内 SELECT ... FOR UPDATE SKIP LOCKED 锁定一行，
    再用 UPDATE 将其置为 claimed。行锁保证同一任务不会被两个
    worker 同时认领。遇到 MySQL 死锁自动重试最多 3 次。

    注意：认领后立刻读取任务详情；如果 worker 需要跨多步执行，
    后续状态推进必须使用 mark_task_running/done/failed(..., claimed_by=worker_id)
    这类条件更新，防止非持有者越权推进。
    """
    task_id = _claim_next_task_tx(worker_id)
    if task_id is None:
        return None

    # 认领成功后另开连接读取完整详情
    return get_task_with_steps(task_id)


@_retry_on_deadlock
def _claim_by_id_tx(conn: Any, task_id: int, worker_id: str) -> bool:
    """在单个连接内按指定任务 id 原子认领（不包含死锁重试）。"""
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


def claim_by_id(task_id: int, worker_id: str) -> bool:
    """按指定任务 id 原子认领（API 手动演示用）。

    仅在任务仍为 pending 时成功，避免已认领/已完成任务被重复认领。
    返回 True 表示本次调用真正完成了认领。
    遇到 MySQL 死锁自动重试最多 3 次。
    """
    return _claim_by_id_tx(task_id, worker_id)


@_retry_on_deadlock
def _release_task_tx(conn: Any, task_id: int, worker_id: str) -> bool:
    """在单个连接内释放任务（不包含死锁重试）。"""
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE tasks
        SET status = 'pending',
            claimed_by = NULL,
            claimed_at = NULL,
            started_at = NULL,
            finished_at = NULL
        WHERE id = %s
          AND claimed_by = %s
          AND status IN ('claimed', 'running')
        """,
        (task_id, worker_id),
    )
    success = cursor.rowcount == 1
    conn.commit()
    return success


def release_task(task_id: int, worker_id: str) -> bool:
    """释放一个由 worker_id 持有的 claimed/running 任务。

    用于 worker 主动放弃/错误恢复；只有当前持有者能释放。
    返回 True 表示任务确实被释放并回到 pending。
    遇到 MySQL 死锁自动重试最多 3 次。
    """
    return _release_task_tx(task_id, worker_id)


@_retry_on_deadlock
def _recover_expired_claims_tx(conn: Any, max_claimed_seconds: float) -> int:
    """在单个连接内回收超时 claimed 任务（不包含死锁重试）。"""
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE tasks
        SET status = 'pending',
            claimed_by = NULL,
            claimed_at = NULL,
            started_at = NULL,
            finished_at = NULL
        WHERE status = 'claimed'
          AND claimed_at IS NOT NULL
          AND claimed_at < NOW() - INTERVAL %s SECOND
        """,
        (float(max_claimed_seconds),),
    )
    recovered = cursor.rowcount
    conn.commit()
    return recovered


def recover_expired_claims(max_claimed_seconds: float) -> int:
    """把超过 max_claimed_seconds 仍停留在 claimed 的任务重置为 pending。

    这是 worker 崩溃后的兜底回收机制：只有长期没有进入 running 的
    claimed 任务会被回收；已经 running 的任务不会被误回收。
    遇到 MySQL 死锁自动重试最多 3 次。
    """
    return _recover_expired_claims_tx(max_claimed_seconds)


# ---------------------------------------------------------------
# 单步状态更新（条件更新，返回是否实际变更）
# ---------------------------------------------------------------

def _mark_task_status(
    conn: Any,
    task_id: int,
    worker_id: str,
    status: str,
    finished: bool,
) -> bool:
    """在已创建的连接内推进任务状态；只允许当前持有者从活动状态到终态/运行态。"""
    assignments = ["status = %s"]
    params: list[Any] = [status]
    if finished:
        assignments.append("finished_at = NOW()")
    params.extend([task_id, worker_id])
    cursor = conn.cursor()
    cursor.execute(
        f"""
        UPDATE tasks
        SET {", ".join(assignments)}
        WHERE id = %s
          AND status IN ('claimed', 'running')
          AND claimed_by = %s
        """,
        tuple(params),
    )
    success = cursor.rowcount == 1
    conn.commit()
    return success


def _mark_task_running(
    conn: Any,
    task_id: int,
    worker_id: str,
) -> bool:
    """在已创建的连接内执行 claimed -> running（不包含死锁重试）。"""
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE tasks
        SET status = 'running', started_at = COALESCE(started_at, NOW())
        WHERE id = %s
          AND status = 'claimed'
          AND claimed_by = %s
        """,
        (task_id, worker_id),
    )
    success = cursor.rowcount == 1
    conn.commit()
    return success


@_retry_on_deadlock
def _mark_task_running_retryable(conn: Any, task_id: int, worker_id: str) -> bool:
    """带死锁重试的 claimed -> running 事务函数。"""
    return _mark_task_running(conn, task_id, worker_id)


def mark_task_running(task_id: int, worker_id: str) -> bool:
    """将任务从 claimed 推进到 running。

    仅当任务当前状态为 claimed 且 claimed_by == worker_id 时成功。
    遇到 MySQL 死锁自动重试最多 3 次。
    """
    return _mark_task_running_retryable(task_id, worker_id)


@_retry_on_deadlock
def _mark_task_done_retryable(conn: Any, task_id: int, worker_id: str) -> bool:
    """带死锁重试的任务 done 事务函数。"""
    return _mark_task_status(conn, task_id, worker_id, "done", finished=True)


def mark_task_done(task_id: int, worker_id: str) -> bool:
    """将任务推进到 done。

    仅当任务由 worker_id 持有且状态为 claimed/running 时成功。
    遇到 MySQL 死锁自动重试最多 3 次。
    """
    return _mark_task_done_retryable(task_id, worker_id)


@_retry_on_deadlock
def _mark_task_failed_retryable(conn: Any, task_id: int, worker_id: str) -> bool:
    """带死锁重试的任务 failed 事务函数。"""
    return _mark_task_status(conn, task_id, worker_id, "failed", finished=True)


def mark_task_failed(task_id: int, worker_id: str) -> bool:
    """将任务推进到 failed。

    仅当任务由 worker_id 持有且状态为 claimed/running 时成功。
    遇到 MySQL 死锁自动重试最多 3 次。
    """
    return _mark_task_failed_retryable(task_id, worker_id)


def _mark_step_status_tx(
    conn: Any,
    task_id: int,
    step_index: int,
    status: str,
    worker_id: str,
    started: bool,
    finished: bool,
) -> bool:
    """在已创建的连接内执行 Step 状态更新事务（不包含死锁重试）。"""
    assignments = ["status = %s"]
    params: list[Any] = [status]

    if started:
        assignments.append("started_at = COALESCE(started_at, NOW())")
    if finished:
        assignments.append("finished_at = NOW()")

    params.extend([task_id, step_index, worker_id])
    cursor = conn.cursor()
    cursor.execute(
        f"""
        UPDATE steps
        SET {", ".join(assignments)}
        WHERE task_id = %s
          AND step_index = %s
          AND EXISTS (
              SELECT 1 FROM tasks
              WHERE tasks.id = steps.task_id
                AND tasks.status IN ('claimed', 'running')
                AND tasks.claimed_by = %s
          )
        """,
        tuple(params),
    )
    success = cursor.rowcount == 1
    conn.commit()
    return success


@_retry_on_deadlock
def _mark_step_status_retryable(
    conn: Any,
    task_id: int,
    step_index: int,
    status: str,
    worker_id: str,
    started: bool,
    finished: bool,
) -> bool:
    """带死锁重试的 Step 状态更新事务函数。"""
    return _mark_step_status_tx(
        conn,
        task_id,
        step_index,
        status,
        worker_id,
        started=started,
        finished=finished,
    )


def mark_step_status(
    task_id: int,
    step_index: int,
    status: str,
    *,
    worker_id: str,
    started: bool = False,
    finished: bool = False,
) -> bool:
    """更新单个 Step 状态，要求任务当前由 worker_id 持有。

    started/finished 控制是否写入时间戳。
    返回 True 表示真正发生了更新。
    遇到 MySQL 死锁自动重试最多 3 次。
    """
    _validate_status(status, STEP_STATUSES)
    return _mark_step_status_retryable(
        task_id,
        step_index,
        status,
        worker_id,
        started=started,
        finished=finished,
    )


# ---------------------------------------------------------------
# 原子事务：一个 Step 的执行结果上报
# ---------------------------------------------------------------

def report_step_execution(
    task_id: int,
    step_index: int,
    status: str,
    worker_id: str,
    *,
    message: Optional[str] = None,
    keep_success_on_conflict: bool = True,
) -> Dict[str, Any]:
    """原子地完成一个 Step 的“日志 + Step 状态 + 任务状态”推进。

    事务语义：
    1. 锁定任务行，确认当前任务由 worker_id 持有且状态为 claimed/running；
    2. 校验 step_index 存在；
    3. 幂等写入 step_logs（若已存在则跳过，不覆盖）；
    4. 更新 Step 状态（running/failed/done + 时间戳）；
    5. 若 Step 失败或这是最后一个 Step，同步推进任务终态。

    返回：
      {
        "task_id": int,
        "step_index": int,
        "step_status": str,
        "task_status": str,
        "log_inserted": bool,
        "task_finished": bool,
        "ignored_duplicate": bool,
      }

    若任务不存在/不持有/状态非法/Step 不存在，抛 RuntimeError 并回滚，
    不产生任何部分写入。

    keep_success_on_conflict 语义：
    - 当该 Step 已有成功日志，而本次上报是 failure 时，若为 True，
      本次失败上报会被忽略，任务和 Step 维持原状态；
    - 满足题目硬性要求“后到的失败不能覆盖已有的成功记录”。
    """
    _validate_status(status, {"success", "failure"})

    conn: MySQLConnectionAbstract = create_connection()
    try:
        conn.start_transaction()
        task = _load_task_for_update(conn, task_id)
        if task is None:
            raise RuntimeError(f"task {task_id} not found")

        _require_owner(task, worker_id)

        # 校验 Step 存在（在事务内查询，避免对不存在 Step 写入日志）
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT status, step_index
            FROM steps
            WHERE task_id = %s AND step_index = %s
            FOR UPDATE
            """,
            (task_id, step_index),
        )
        step_row = cursor.fetchone()
        if step_row is None:
            raise RuntimeError(
                f"step_index {step_index} not found in task {task_id}"
            )

        # 严格按 step_index 升序推进：若存在比当前 Step 更小且仍未完成的前置 Step，
        # 拒绝本次上报。这样避免“先报 Step2 再报 Step1”绕过顺序语义。
        cursor.execute(
            """
            SELECT step_index, status
            FROM steps
            WHERE task_id = %s
            ORDER BY step_index
            FOR UPDATE
            """,
            (task_id,),
        )
        all_step_rows = cursor.fetchall()
        all_steps = {int(r["step_index"]): r["status"] for r in all_step_rows}
        if step_index not in all_steps:
            raise RuntimeError(
                f"step_index {step_index} not found in task {task_id}"
            )
        before_incomplete = [
            idx
            for idx, st in all_steps.items()
            if idx < step_index and st != "done"
        ]
        if before_incomplete:
            raise RuntimeError(
                f"step_index {step_index} cannot be reported before "
                f"previous steps complete: {before_incomplete}"
            )
        # 对已 done 的 Step 再次 success 属于幂等路径，放行；
        # 对已 done 的 Step 再次 failure 会在后面由 keep_success_on_conflict 忽略。
        step_status = "done" if status == "success" else "failed"
        was_done = all_steps[step_index] == "done"
        log_inserted = _insert_step_log(
            conn, task_id, step_index, status, message
        )

        # 后到失败不能覆盖已有成功日志/状态：若该 Step 已存在成功日志，
        # 本次 failure 上报在 keep_success_on_conflict=True 时整体忽略。
        existing_success = status == "failure" and _step_has_success_log(
            conn, task_id, step_index
        )

        if existing_success and keep_success_on_conflict:
            # 不改 Step、不改任务，也不覆盖日志；事务内没有任何数据变更。
            conn.commit()
            return {
                "task_id": task_id,
                "step_index": step_index,
                "step_status": all_steps[step_index],
                "task_status": task["status"],
                "log_inserted": False,
                "task_finished": False,
                "ignored_duplicate": True,
            }
        # 若本次上报的是已经 done 的 Step 且状态仍为 success，属于重复幂等，
        # 保持任务原状态，不再重复推进。
        if was_done and status == "success":
            conn.commit()
            return {
                "task_id": task_id,
                "step_index": step_index,
                "step_status": all_steps[step_index],
                "task_status": task["status"],
                "log_inserted": False,
                "task_finished": False,
                "ignored_duplicate": False,
            }

        # 更新 Step 为 done/failed
        step_updated = _update_step_status(
            conn,
            task_id,
            step_index,
            step_status,
            finished=True,
        )

        # 判断任务是否结束：失败即结束；成功且是最后一个 Step 则 done。
        task_finished = False
        if status == "failure":
            task_status = "failed"
            _update_task_status(conn, task_id, "failed", finished=True)
            task_finished = True
        else:
            total = len(all_steps)
            # 基于事务开始时锁定的 Step 状态计算：只有本次真正从非 done 变成 done
            # 才增加 done 计数；重复上报已 done Step 不会让任务误判为全部完成。
            done_count = sum(1 for st in all_steps.values() if st == "done")
            if step_updated and step_status == "done" and not was_done:
                done_count += 1
            if total == done_count:
                task_status = "done"
                _update_task_status(conn, task_id, "done", finished=True)
                task_finished = True
            else:
                task_status = task["status"]  # 保持 running/claimed

        conn.commit()
        return {
            "task_id": task_id,
            "step_index": step_index,
            "step_status": step_status,
            "task_status": task_status,
            "log_inserted": log_inserted,
            "task_finished": task_finished,
            "ignored_duplicate": False,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def complete_task_atomically(
    task_id: int,
    worker_id: str,
    *,
    results: Sequence[Mapping[str, Any]],
    keep_success_on_conflict: bool = True,
) -> Dict[str, Any]:
    """在单个事务内批量提交一个任务的全部 Step 执行结果。

    这是 run_worker_once 的原子版本：所有 Step 日志、Step 状态、任务终态
    要么全部生效，要么全部回滚，避免执行中断产生部分状态。

    results 元素格式：
      {"step_index": int, "success": bool, "message": str = "", ...}

    返回：
      {
        "task_id": int,
        "task_status": str,
        "log_inserted": int,
        "steps_updated": int,
        "ignored_failures": int,
      }

    若任务不持有/状态非法/Step 缺失/结果未覆盖任务全部 Step/结果数超过保护上限，
    抛 RuntimeError 并整体回滚。

    keep_success_on_conflict：若某 Step 已有成功日志而本次结果为失败，
    为 True 时该失败结果被忽略（不改变 Step/任务状态），满足
    “后到失败不能覆盖已有成功”的硬性要求。
    """
    if not isinstance(results, Sequence) or isinstance(results, (str, bytes)):
        raise TypeError(
            f"results must be a sequence, got {type(results).__name__}"
        )
    if len(results) > MAX_ATOMIC_STEPS:
        raise ValueError(
            f"too many step results: {len(results)} > {MAX_ATOMIC_STEPS}"
        )
    if len(results) == 0:
        raise ValueError("results must not be empty when completing a task")

    conn: MySQLConnectionAbstract = create_connection()
    try:
        conn.start_transaction()
        task = _load_task_for_update(conn, task_id)
        if task is None:
            raise RuntimeError(f"task {task_id} not found")
        _require_owner(task, worker_id)

        # 校验所有 step_index 都存在且没有重复；同时锁定步骤行。
        indexes = [int(item["step_index"]) for item in results]
        if len(set(indexes)) != len(indexes):
            raise ValueError(f"duplicate step_index in results: {indexes}")

        cursor = conn.cursor(dictionary=True)
        sql_placeholders = ", ".join(["%s"] * len(indexes))
        cursor.execute(
            f"""
            SELECT step_index
            FROM steps
            WHERE task_id = %s AND step_index IN ({sql_placeholders})
            FOR UPDATE
            """,
            [task_id, *indexes],
        )
        existing = {int(row["step_index"]) for row in cursor.fetchall()}
        missing = set(indexes) - existing
        if missing:
            raise RuntimeError(
                f"step_index not found in task {task_id}: {sorted(missing)}"
            )

        # 校验提交结果覆盖任务的全部 Step；不允许“只提交部分 Step 就把任务 done”。
        # 先锁全部 Step 行，避免提交过程中任务 Steps 被并发修改。
        cursor.execute(
            """
            SELECT step_index
            FROM steps
            WHERE task_id = %s
            ORDER BY step_index
            FOR UPDATE
            """,
            (task_id,),
        )
        all_steps = [int(row["step_index"]) for row in cursor.fetchall()]
        if len(all_steps) != len(existing) or set(all_steps) != set(indexes):
            raise RuntimeError(
                f"results must cover every step of task {task_id}; "
                f"task steps={all_steps}, result steps={sorted(indexes)}"
            )

        log_inserted = 0
        ignored_failures = 0
        effective_results: List[Mapping[str, Any]] = []
        for item in results:
            step_index = int(item["step_index"])
            success = bool(item["success"])
            message = item.get("message") or ""
            step_status = "done" if success else "failed"

            if not success and keep_success_on_conflict:
                # 已有成功日志时忽略本次失败结果
                if _step_has_success_log(conn, task_id, step_index):
                    ignored_failures += 1
                    continue

            if _insert_step_log(
                conn, task_id, step_index,
                "success" if success else "failure",
                message,
            ):
                log_inserted += 1
            _update_step_status(
                conn,
                task_id,
                step_index,
                step_status,
                started=True,
                finished=True,
            )
            effective_results.append(item)

        any_failed = any(not bool(item["success"]) for item in effective_results)
        if any_failed:
            task_status = "failed"
            _update_task_status(conn, task_id, "failed", finished=True)
        else:
            task_status = "done"
            _update_task_status(conn, task_id, "done", finished=True)

        conn.commit()
        return {
            "task_id": task_id,
            "task_status": task_status,
            "log_inserted": log_inserted,
            "steps_updated": len(effective_results),
            "ignored_failures": ignored_failures,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------
# 删除/查询/幂等日志（保留原接口）
# ---------------------------------------------------------------

@_retry_on_deadlock
def _delete_task_by_id_tx(conn: Any, task_id: int) -> None:
    """在单个连接内删除任务（不包含死锁重试）。"""
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tasks WHERE id = %s", (task_id,))
    conn.commit()


def delete_task_by_id(task_id: int) -> None:
    """删除任务及其 steps / step_logs（外键级联删除）。测试清理用。

    遇到 MySQL 死锁自动重试最多 3 次。
    """
    return _delete_task_by_id_tx(task_id)


def list_step_logs(task_id: int) -> List[Dict[str, Any]]:
    """返回某任务的全部步骤日志，按 step_index 升序。

    遇到 MySQL 死锁自动重试最多 3 次。
    """
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


@_retry_on_deadlock
def _write_step_log_tx(
    conn: Any,
    task_id: int,
    step_index: int,
    status: str,
    message: Optional[str],
) -> bool:
    """在单个连接内幂等写入 Step 日志（不包含死锁重试）。"""
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
    - 遇到 MySQL 死锁自动重试最多 3 次。
    """
    _validate_status(status, LOG_STATUSES)
    return _write_step_log_tx(task_id, step_index, status, message)
