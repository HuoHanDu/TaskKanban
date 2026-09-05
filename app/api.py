"""FastAPI 接口层。

提供：
- GET  /                          极简看板页面
- GET  /tasks                     任务列表
- POST /tasks/{task_id}/claim     手动认领（可选演示用）
- POST /tasks/{task_id}/steps/{step_index}/report  幂等完成上报
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from app import repository

app = FastAPI(title="TaskKanban", version="0.1.0")

WEB_DIR = Path(__file__).resolve().parents[1] / "web"


class ReportBody(BaseModel):
    status: str = Field(default="success", pattern="^(success|failure)$")
    message: Optional[str] = None
    worker_id: Optional[str] = "api-report"


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/tasks")
def get_tasks():
    return {"tasks": repository.list_tasks()}


@app.get("/tasks/{task_id}")
def get_task_detail(task_id: int):
    """获取任务详情（含 steps），供前端展示/演示用。"""
    task = repository.get_task_with_steps(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return task


@app.get("/tasks/{task_id}/logs")
def get_task_logs(task_id: int):
    """返回任务全部 Step 执行日志，用于看板/答辩展示参数演变。"""
    task = repository.get_task_with_steps(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    logs = repository.list_step_logs(task_id)
    return {"task_id": task_id, "logs": logs}


@app.post("/tasks/{task_id}/claim")
def claim_task(task_id: int):
    """按指定任务手动认领（演示/测试用；真实 worker 使用数据库原子认领）。"""
    task = repository.get_task_with_steps(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if task["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"task status is {task['status']}")

    claimed = repository.claim_by_id(task_id, worker_id="manual-claim")
    return {"claimed": claimed, "status": "claimed" if claimed else task["status"]}


@app.post("/tasks/{task_id}/start")
def start_task(task_id: int):
    """手动把 manual-claim 认领的任务从 claimed 推进到 running（看板演示用）。"""
    task = repository.get_task_with_steps(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    if task["status"] != "claimed":
        raise HTTPException(
            status_code=409,
            detail=f"task status is {task['status']}, start only allowed while claimed",
        )
    if task["claimed_by"] != "manual-claim":
        raise HTTPException(
            status_code=403,
            detail="only tasks claimed by manual-claim can be started via API",
        )

    started = repository.mark_task_running(task_id, "manual-claim")
    if not started:
        raise HTTPException(status_code=409, detail="task could not be started")
    return {"task_id": task_id, "status": "running", "started": True}


@app.post("/tasks/{task_id}/steps/{step_index}/report")
def report_step(task_id: int, step_index: int, body: ReportBody):
    """重复上报 Step 完成结果；验证幂等写入。

    默认 worker_id 固定为 "api-report"，用于看板并发按钮演示。
    该接口只接受任务处于 claimed/running 且由同一 worker_id 持有；
    若任务不在执行中，返回 409，避免对已完成/未认领任务写入日志。
    """
    task = repository.get_task_with_steps(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")

    if task["status"] not in {"claimed", "running"}:
        raise HTTPException(
            status_code=409,
            detail=f"task status is {task['status']}, report only allowed while claimed/running",
        )

    worker_id = body.worker_id or "api-report"
    if task["claimed_by"] != worker_id:
        raise HTTPException(
            status_code=403,
            detail=f"task is claimed by {task['claimed_by']!r}, not {worker_id!r}",
        )

    try:
        outcome = repository.report_step_execution(
            task_id,
            step_index,
            body.status,
            worker_id,
            message=body.message or "api report",
            keep_success_on_conflict=True,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    if outcome["ignored_duplicate"]:
        message = "duplicate failure ignored; existing success preserved"
    elif outcome["log_inserted"]:
        message = "first write"
    else:
        message = "duplicate ignored (idempotent)"

    return {
        "task_id": task_id,
        "step_index": step_index,
        "status": body.status,
        "inserted": outcome["log_inserted"],
        "step_status": outcome["step_status"],
        "task_status": outcome["task_status"],
        "task_finished": outcome["task_finished"],
        "ignored_duplicate": outcome["ignored_duplicate"],
        "message": message,
    }


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):
    return JSONResponse(status_code=500, content={"detail": str(exc)})
