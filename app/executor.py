"""步骤模拟执行器。

笔试只要求任务调度/参数/并发/幂等，不要求真实发送消息。
这里根据 step.action 打印日志并模拟成功；可预留失败注入便于测试。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any, Dict, Mapping


@dataclass
class StepResult:
    success: bool
    message: str = ""


def execute_step(
    step: Mapping[str, Any],
    params: Mapping[str, Any],
    *,
    fail_rate: float = 0.0,
    sleep_seconds: float = 0.1,
) -> StepResult:
    """模拟执行一个 Step。

    参数：
      step: repository 返回的步骤 dict，含 action、override 等。
      params: 该 Step 执行时应看到的完整参数快照。
      fail_rate: 模拟失败概率，默认 0。
      sleep_seconds: 模拟耗时，便于观察 running 状态。
    """
    time.sleep(sleep_seconds)

    action = step.get("action", "mock")
    message = f"[{action}] params={dict(params)}"

    # 测试/演示时可用 fail_rate 注入失败
    if fail_rate > 0 and random.random() < fail_rate:
        return StepResult(success=False, message=message + " -> simulated failure")

    return StepResult(success=True, message=message)
